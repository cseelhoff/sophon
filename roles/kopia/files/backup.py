#!/usr/bin/env python3
"""Sophon v1/v2 backup-label collector. No restore command is ever executed here.

Public functions: portainer_api(url, key, endpoint_id=1, timeout=60),
prepare(api, root="/source", command_timeout=600, poll_interval=.25), and
verify(snapshot, output=None, restore_root="/restore"). API contexts are plain dicts;
tests may supply {"endpoint": 1, "transport": callable}, where the callable takes
(method, path, body, stream) and returns decoded JSON or {"read": fn, "close": fn}.
parse_spec(container) validates either label version into the same recipe;
encode_labels(spec) emits v2 labels from an already validated recipe.

prepare returns {"overrideRoot": "<root>/runs/<run>"} after workload recovery.
CLI stdout is wrapper input, NOT Kopia's native action result-file protocol;
the deployment wrapper must translate it using the verified Kopia protocol.
manifest.json version 1 contains run, created_at, endpoint, containers (complete
inspect records), stacks ({stack, file}), protection (volume name -> owners and
excludes), artifacts, restore, and final_containers. Artifact records contain
member, group, type, name, path, container_path, sha256, bytes, payload_bytes.
Restore records contain member, group, backup_dir, commands (native argv/user),
covers (destinations), volumes (destination -> volume name), and artifacts.
The private root/journal.json is outside every immutable snapshot run.
Fully recovered failed prepares delete only their own newly created staging
directory under the prepare lock. Success and unresolved-recovery staging stay.

verify checks all hashes/archives before extraction, emits restore-recipes.json,
and returns {"output": ..., "recipes": ..., "artifacts": ..., "bytes": ...}.
With output=None it performs read-only validation and returns the secret-bearing
manifest plus artifact/payload counts, without creating directories or extracting.
Local data-filter extraction intentionally does NOT preserve UID/GID; original
tar bytes and full secret-bearing deployment metadata remain in the snapshot.
"""

import argparse
import datetime
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import posixpath
import re
import signal
import shutil
import ssl
import sys
import tarfile
import time
from urllib.parse import quote, urlsplit
import uuid


VERSION_LABEL = "sophon.backup.version"
SPEC_LABEL = "sophon.backup.spec"
LABEL_PREFIX = "sophon.backup."
LABEL_COLLECTIONS = {
    "covers": "covers", "excludes": "excludes", "paths": "paths",
    "prepare": "pre", "archive-pre": "backup", "archive-post": "post",
    "restore": "restore",
}
GROUP = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
ID = re.compile(r"[0-9a-f]{64}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
VARIABLE = "${SOPHON_BACKUP_DIR}"
CHUNK = 1024 * 1024
JSON_LIMIT = 64 * CHUNK
SPEC_KEYS = {
    "group", "covers", "excludes", "paths", "pre", "backup", "post", "restore",
    "stop", "stop_timeout",
}


def fail(message):
    raise RuntimeError(message)


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        fail("private directory is a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    """Atomic, fsynced replacement: intent must survive before remote mutation."""
    path = Path(path)
    staging = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, separators=(",", ":"), ensure_ascii=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, path)
        sync_dir(path.parent)
    finally:
        if staging.exists():
            staging.unlink()


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        data = source.read(JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        fail("metadata exceeds size limit")
    try:
        return json.loads(data, object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError):
        fail("invalid JSON metadata")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate JSON key")
        result[key] = value
    return result


def portainer_api(url, key, endpoint_id=1, timeout=60):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        fail("PORTAINER_URL must be an HTTPS origin or base path")
    if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
        fail("missing or invalid PORTAINER_API_KEY")
    if not str(endpoint_id).isdigit() or int(endpoint_id) < 1:
        fail("invalid PORTAINER_ENDPOINT_ID")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        fail("invalid API timeout")
    return {
        "host": parsed.hostname, "port": parsed.port or 443,
        "base": parsed.path.rstrip("/"), "key": key,
        "endpoint": int(endpoint_id), "timeout": timeout,
    }


def close_response(response, connection):
    try:
        response.close()
    finally:
        connection.close()


def request(api, method, path, body=None, stream=False):
    adapter = api.get("transport")
    if adapter is not None:
        return adapter(method, path, body, stream)
    connection = http.client.HTTPSConnection(
        api["host"], api["port"], timeout=api["timeout"],
        context=ssl.create_default_context(),
    )
    try:
        payload = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        headers = {"X-API-Key": api["key"], "User-Agent": "curl/8.9.0"}
        if payload is not None:
            headers["Content-Type"] = "application/x-tar" if isinstance(body, bytes) else "application/json"
        connection.request(method, api["base"] + path, body=payload, headers=headers)
        response = connection.getresponse()
        # http.client never forwards the API key to a redirect destination.
        if not 200 <= response.status < 300:
            fail(f"API request failed (HTTP {response.status})")
        if stream:
            return {
                "read": response.read,
                "close": lambda: close_response(response, connection),
            }
        data = response.read(JSON_LIMIT + 1)
        if len(data) > JSON_LIMIT:
            fail("API metadata exceeds size limit")
        close_response(response, connection)
        return json.loads(data) if data.strip() else None
    except RuntimeError:
        connection.close()
        raise
    except (OSError, http.client.HTTPException, ValueError) as error:
        connection.close()
        fail("API transport or response failure: " + type(error).__name__)


def docker(api, method, path, body=None, stream=False):
    return request(
        api, method, f"/api/endpoints/{api['endpoint']}/docker" + path, body, stream,
    )


def container_path(value, backup_dir=None):
    if (not isinstance(value, str) or not value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        fail("invalid container path")
    if value.startswith(VARIABLE):
        suffix = value[len(VARIABLE):]
        if not re.fullmatch(r"(?:[-.][a-zA-Z0-9][a-zA-Z0-9._-]*)?(?:/.*)?", suffix):
            fail("unsafe backup-directory suffix")
        value = (backup_dir or "/sophon-placeholder") + suffix
    if ("$" in value or "\\" in value or not value.startswith("/")
            or value.startswith("//") or value == "/"
            or posixpath.normpath(value) != value):
        fail("unsafe container path")
    return value


def command_records(records):
    if not isinstance(records, list):
        fail("commands must be a list")
    for record in records:
        if not isinstance(record, dict) or set(record) - {"argv", "user"}:
            fail("invalid command record")
        argv = record.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg.strip() or "\x00" in arg
                       for arg in argv)):
            fail("invalid command argv")
        if ("user" in record and (not isinstance(record["user"], str)
                                 or not record["user"].strip()
                                 or any(c in record["user"] for c in "\x00\r\n"))):
            fail("invalid command user")
    return records


def decode_labels(labels):
    if not isinstance(labels, dict):
        fail("invalid container labels")
    fields = {
        key[len(LABEL_PREFIX):]: value for key, value in labels.items()
        if isinstance(key, str) and key.startswith(LABEL_PREFIX)
    }
    if not fields:
        return None
    if any(not isinstance(value, str) for value in fields.values()):
        fail("Sophon backup label values must be strings")
    if fields.get("version") == "1":
        if set(fields) != {"version", "spec"}:
            fail("incomplete or mixed v1/v2 Sophon backup labels")
        try:
            return json.loads(fields["spec"], object_pairs_hook=unique_keys)
        except (ValueError, UnicodeError):
            fail("malformed Sophon backup spec")
    required = {"version", "group", "stop-during-backup", "stop-timeout"}
    if fields.get("version") != "2" or not required <= fields.keys():
        fail("unsupported or incomplete Sophon backup labels")
    if fields["stop-during-backup"] not in ("true", "false"):
        fail("stop-during-backup must be true or false")
    if not re.fullmatch(r"[1-9][0-9]*", fields["stop-timeout"]):
        fail("stop-timeout must be positive integer seconds")
    try:
        timeout = int(fields["stop-timeout"])
    except ValueError:
        fail("invalid stop-timeout")
    spec = {
        "group": fields["group"], "stop": fields["stop-during-backup"] == "true",
        "stop_timeout": timeout, "covers": [], "excludes": {}, "paths": [],
        "pre": [], "backup": [], "post": [], "restore": [],
    }
    collections = {name: {} for name in LABEL_COLLECTIONS}
    for key, value in fields.items():
        if key in required:
            continue
        match = re.fullmatch(r"([a-z-]+)\.(0|[1-9][0-9]*)\.([a-z-]+)", key)
        if match is None or match[1] not in collections:
            fail("unknown or mixed v2 Sophon backup label")
        if len(match[2]) > len(str(len(fields))) or int(match[2]) >= len(fields):
            fail("noncontiguous backup label indexes")
        record = collections[match[1]].setdefault(int(match[2]), {})
        record[match[3]] = value
    for name, records in collections.items():
        if sorted(records) != list(range(len(records))):
            fail("noncontiguous backup label indexes")
        for index in range(len(records)):
            record = records[index]
            if name == "covers":
                if set(record) != {"path"}:
                    fail("cover label requires exactly path")
                spec["covers"].append(record["path"])
            elif name == "excludes":
                if set(record) != {"path", "reason"} or record["path"] in spec["excludes"]:
                    fail("exclusion labels require unique path and reason")
                spec["excludes"][record["path"]] = record["reason"]
            else:
                if name != "paths":
                    if "argv" not in record:
                        fail("command label requires argv")
                    try:
                        record["argv"] = json.loads(record["argv"], object_pairs_hook=unique_keys)
                    except (ValueError, UnicodeError):
                        fail("malformed command argv label")
                spec[LABEL_COLLECTIONS[name]].append(record)
    return spec


def encode_labels(spec):
    labels = {
        VERSION_LABEL: "2",
        LABEL_PREFIX + "group": spec["group"],
        LABEL_PREFIX + "stop-during-backup": "true" if spec["stop"] else "false",
        LABEL_PREFIX + "stop-timeout": str(spec["stop_timeout"]),
    }
    for name, field in LABEL_COLLECTIONS.items():
        records = spec[field]
        if name == "covers":
            records = [{"path": path} for path in records]
        elif name == "excludes":
            records = [{"path": path, "reason": reason} for path, reason in records.items()]
        for index, record in enumerate(records):
            for key, value in record.items():
                labels[f"{LABEL_PREFIX}{name}.{index}.{key}"] = (
                    json.dumps(value, separators=(",", ":")) if key == "argv" else value
                )
    return labels


def parse_spec(container):
    spec = decode_labels(container.get("Config", {}).get("Labels") or {})
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS:
        fail("Sophon backup recipe requires exactly the expected fields")
    if not isinstance(spec["group"], str) or not GROUP.fullmatch(spec["group"]):
        fail("unsafe backup group")
    covers, excludes = spec["covers"], spec["excludes"]
    if (not isinstance(covers, list) or any(not isinstance(v, str) for v in covers)
            or len(set(covers)) != len(covers) or not isinstance(excludes, dict)):
        fail("invalid covers or excludes")
    volumes = {
        mount["Destination"]: mount.get("Name")
        for mount in container.get("Mounts", []) if mount.get("Type") == "volume"
    }
    for destination in covers:
        container_path(destination)
        if not volumes.get(destination):
            fail("cover does not identify a persistent volume mount")
    for destination, reason in excludes.items():
        container_path(destination)
        if not isinstance(reason, str) or not reason.strip() or "\x00" in reason:
            fail("exclusion requires a reconstructible or duplicate reason")
        if not volumes.get(destination):
            fail("exclusion does not identify a persistent volume mount")
    if set(covers) & set(excludes):
        fail("overlapping covers and excludes")
    if type(spec["stop"]) is not bool:
        fail("stop must be a boolean")
    if type(spec["stop_timeout"]) is not int or spec["stop_timeout"] < 1:
        fail("stop_timeout must be positive integer seconds")
    for phase in ("pre", "backup", "post", "restore"):
        command_records(spec[phase])
    if spec["stop"] and spec["backup"]:
        fail("stopped members cannot execute backup commands; use pre hooks or live backup")
    if not isinstance(spec["paths"], list):
        fail("paths must be a list")
    names = set()
    for artifact in spec["paths"]:
        if not isinstance(artifact, dict):
            fail("invalid artifact record")
        match artifact.get("type"):
            case "archive":
                if set(artifact) != {"type", "path", "name"}:
                    fail("invalid archive record")
                container_path(artifact["path"])
            case "portainer-backup":
                if set(artifact) != {"type", "name"}:
                    fail("invalid Portainer backup record")
                if spec["stop"]:
                    fail("native Portainer backup must keep its API running")
            case _:
                fail("unknown artifact type")
        name = artifact.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            fail("unsafe or duplicate artifact name")
        names.add(name)
    if covers and not spec["paths"]:
        fail("data owner has no artifacts")
    return spec


def fingerprint(container):
    mounts = sorted(container.get("Mounts", []),
                    key=lambda mount: json.dumps(mount, sort_keys=True))
    data = {key: container.get(key) for key in ("Id", "Name", "Image")}
    data["config_image"] = container.get("Config", {}).get("Image")
    data["Mounts"] = mounts
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def inspect(api, member):
    container = docker(api, "GET", f"/containers/{member}/json")
    if not isinstance(container, dict) or container.get("Id") != member:
        fail("container identity mismatch")
    if not isinstance(container.get("State", {}).get("Running"), bool):
        fail("missing container running state")
    return container


def inventory(api, include_stacks=True):
    listing = docker(api, "GET", "/containers/json?all=true")
    if not isinstance(listing, list):
        fail("invalid container inventory")
    containers, members, protection = [], {}, {}
    for entry in listing:
        member = entry.get("Id") if isinstance(entry, dict) else None
        if not isinstance(member, str) or not ID.fullmatch(member):
            fail("invalid immutable container ID")
        container = inspect(api, member)
        containers.append(container)
        spec = parse_spec(container)
        if spec is not None:
            members[member] = {
                "id": member, "fingerprint": fingerprint(container), "spec": spec,
                "running": container["State"]["Running"],
                "volumes": {
                    mount["Destination"]: mount["Name"]
                    for mount in container.get("Mounts", [])
                    if mount.get("Type") == "volume"
                },
            }
        for mount in container.get("Mounts", []):
            if mount.get("Type") != "volume":
                continue
            volume, destination = mount.get("Name"), mount.get("Destination")
            if not isinstance(volume, str) or not volume:
                fail("persistent volume has no immutable name")
            container_path(destination)
            record = protection.setdefault(volume, {"owners": [], "excludes": []})
            if spec is None:
                continue
            if destination in spec["covers"] and member not in record["owners"]:
                record["owners"].append(member)
            if destination in spec["excludes"]:
                record["excludes"].append({
                    "member": member, "destination": destination,
                    "reason": spec["excludes"][destination],
                })
    if any(not record["owners"] and not record["excludes"]
           for record in protection.values()):
        fail("uncovered persistent volume in current or exited container")
    stacks = []
    if include_stacks:
        listing = request(api, "GET", "/api/stacks")
        if not isinstance(listing, list):
            fail("invalid stack inventory")
        for stack in listing:
            if stack.get("EndpointId") != api["endpoint"]:
                continue
            stack_id = stack.get("Id")
            if type(stack_id) is not int or stack_id < 1:
                fail("invalid stack ID")
            stacks.append({
                "stack": stack,
                "file": request(api, "GET", f"/api/stacks/{stack_id}/file"),
            })
    return {"containers": containers, "members": members,
            "protection": protection, "stacks": stacks}


def unchanged(api, member):
    container = inspect(api, member["id"])
    if fingerprint(container) != member["fingerprint"]:
        fail("container name/image/mount replacement detected")
    return container


def save_journal(root, journal):
    write_json(root / "journal.json", journal)


def log_command(phase, member, index, exit_code):
    print(f"command phase={phase} member={member} index={index} exit={exit_code}",
          file=sys.stderr, flush=True)


def run_command(api, root, journal, member, record, phase, index,
                command_timeout, poll_interval):
    container = unchanged(api, member)
    if not container["State"]["Running"]:
        fail(f"command requires running member={member['id']} phase={phase} index={index}")
    pidfile = "/tmp/sophon-exec-" + uuid.uuid4().hex
    argv = [arg.replace(VARIABLE, journal["backup_dir"]) for arg in record["argv"]]
    body = {
        "AttachStdout": False, "AttachStderr": False,
        "Env": [f"SOPHON_PIDFILE={pidfile}",
                f"SOPHON_BACKUP_DIR={journal['backup_dir']}"],
        # A background child cannot be its inherited process-group leader, so
        # setsid cannot fork away from the exec we inspect (BusyBox has no -w).
        "Cmd": [
            "/bin/sh", "-ceu",
            'setsid /bin/sh -ceu \'umask 077; echo $$ > "$SOPHON_PIDFILE"; '
            'exec "$@"\' sophon "$@" & child=$!; wait "$child"',
            "sophon", *argv,
        ],
    }
    if "user" in record:
        body["User"] = record["user"]
    created = docker(api, "POST", f"/containers/{member['id']}/exec", body)
    exec_id = created.get("Id") if isinstance(created, dict) else None
    if not isinstance(exec_id, str) or not ID.fullmatch(exec_id):
        fail("invalid exec ID")
    execution = {
        "member": member["id"], "id": exec_id, "pidfile": pidfile,
        "phase": phase, "index": index,
    }
    journal["execs"].append(execution)
    save_journal(root, journal)
    docker(api, "POST", f"/exec/{exec_id}/start", {"Detach": True, "Tty": False})
    deadline = time.monotonic() + command_timeout
    while True:
        state = docker(api, "GET", f"/exec/{exec_id}/json")
        if type(state.get("Running")) is not bool:
            fail("invalid exec inspection")
        if not state["Running"]:
            code = state.get("ExitCode")
            if type(code) is not int:
                fail("missing exec exit code")
            journal["execs"].remove(execution)
            save_journal(root, journal)
            log_command(phase, member["id"], index, code)
            if code != 0:
                fail(f"command failed member={member['id']} phase={phase} index={index} exit={code}")
            return
        if time.monotonic() >= deadline:
            fail(f"command timed out member={member['id']} phase={phase} index={index}")
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


def small_archive(api, member, path):
    stream = docker(api, "GET", f"/containers/{member}/archive?path={quote(path, safe='')}",
                    stream=True)
    data = bytearray()
    try:
        while chunk := stream["read"](8192):
            data.extend(chunk)
            if len(data) > 65536:
                fail("PID archive exceeds size limit")
    finally:
        stream["close"]()
    import io
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            if (len(members) != 1 or not members[0].isreg()
                    or not 1 <= members[0].size <= 32):
                fail("invalid PID archive")
            pid = archive.extractfile(members[0]).read().strip()
    except (tarfile.TarError, OSError, EOFError):
        fail("invalid PID archive")
    if not re.fullmatch(rb"[0-9]{1,10}", pid) or not 2 <= int(pid) <= 2147483647:
        fail("unsafe process-group PID")
    return int(pid)


def signal_group(api, member, pidfile, pid, sig, timeout, poll_interval):
    # PID comes only from the unique in-container wrapper file, never Exec.Pid.
    script = ('IFS= read -r pid < "$1"; [ "$pid" = "$2" ] || exit 1; '
              f'kill -{sig} "-$pid" 2>/dev/null || :')
    created = docker(api, "POST", f"/containers/{member}/exec", {
        "AttachStdout": False, "AttachStderr": False, "User": "0",
        "Cmd": ["/bin/sh", "-ceu", script, "sophon-cancel", pidfile, str(pid)],
    })
    exec_id = created.get("Id") if isinstance(created, dict) else None
    if not isinstance(exec_id, str) or not ID.fullmatch(exec_id):
        fail("invalid cancellation exec ID")
    docker(api, "POST", f"/exec/{exec_id}/start", {"Detach": True, "Tty": False})
    deadline = time.monotonic() + timeout
    while True:
        state = docker(api, "GET", f"/exec/{exec_id}/json")
        if state.get("Running") is False:
            if state.get("ExitCode") != 0:
                fail("process-group cancellation command failed")
            return
        if time.monotonic() >= deadline:
            fail("process-group cancellation command timed out")
        time.sleep(poll_interval)


def cancel_exec(api, execution, member, poll_interval):
    unchanged(api, member)
    state = docker(api, "GET", f"/exec/{execution['id']}/json")
    if state.get("Running") is False:
        return
    if state.get("Running") is not True:
        fail("unknown dump execution state")
    pid = small_archive(api, member["id"], execution["pidfile"])
    for sig, grace in (("TERM", 2), ("KILL", 10)):
        # Recheck just before signalling: do not kill a completed/reused PID.
        state = docker(api, "GET", f"/exec/{execution['id']}/json")
        if state.get("Running") is False:
            return
        signal_group(api, member["id"], execution["pidfile"], pid, sig, 10, poll_interval)
        deadline = time.monotonic() + grace
        while True:
            state = docker(api, "GET", f"/exec/{execution['id']}/json")
            if state.get("Running") is False:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)
    fail("dump process group remains running; writers not resumed")


def finalize(api, root, journal, command_timeout, poll_interval, groups=None):
    """Finish every safe member; retain any unresolved intents for next startup."""
    failures = []
    members = journal["members"]
    selected = sorted(groups or {member["spec"]["group"] for member in members.values()})
    for group in selected:
        ids = [key for key, member in members.items() if member["spec"]["group"] == group]
        cancellation_failed = False
        for execution in list(journal["execs"]):
            if execution["member"] not in ids:
                continue
            try:
                cancel_exec(api, execution, members[execution["member"]], poll_interval)
                journal["execs"].remove(execution)
                save_journal(root, journal)
            except BaseException:
                cancellation_failed = True
                failures.append(f"cancel failed member={execution['member']} phase={execution['phase']}")
        if cancellation_failed:
            failures.append(f"group={group} remains paused until dump cancellation succeeds")
            continue
        for member_id in ids:
            member = members[member_id]
            if not member["stop_pending"]:
                continue
            try:
                container = unchanged(api, member)
                if not container["State"]["Running"]:
                    try:
                        docker(api, "POST", f"/containers/{member_id}/start")
                    except Exception:
                        if not unchanged(api, member)["State"]["Running"]:
                            raise
                    if not unchanged(api, member)["State"]["Running"]:
                        fail("restart did not make member running")
                member["stop_pending"] = False
                save_journal(root, journal)
            except BaseException:
                failures.append(f"restart failed member={member_id}")
        for member_id in ids:
            member = members[member_id]
            if not member["pending_post"]:
                continue
            if member["stop_pending"]:
                failures.append(f"post deferred until restart member={member_id}")
                continue
            post_failed = False
            for index, record in enumerate(member["spec"]["post"]):
                try:
                    run_command(api, root, journal, member, record, "post", index,
                                command_timeout, poll_interval)
                except BaseException:
                    post_failed = True
                    failures.append(f"post failed member={member_id} index={index}")
                    # A timed-out post command must be cancelled before another.
                    for execution in list(journal["execs"]):
                        if execution["member"] == member_id:
                            try:
                                cancel_exec(api, execution, member, poll_interval)
                                journal["execs"].remove(execution)
                                save_journal(root, journal)
                            except BaseException:
                                failures.append(f"post cancellation failed member={member_id}")
                                break
                    if any(item["member"] == member_id for item in journal["execs"]):
                        break
            if not post_failed:
                member["pending_post"] = False
                try:
                    save_journal(root, journal)
                except BaseException:
                    member["pending_post"] = True
                    failures.append(f"post journal persistence failed member={member_id}")
    return failures


def validate_journal(journal):
    if (not isinstance(journal, dict) or journal.get("version") != 1
            or not isinstance(journal.get("members"), dict)
            or not isinstance(journal.get("execs"), list)):
        fail("invalid recovery journal; manual recovery required")
    container_path(journal.get("backup_dir"))
    for key, member in journal["members"].items():
        if (not ID.fullmatch(key) or member.get("id") != key
                or not HASH.fullmatch(member.get("fingerprint", ""))
                or type(member.get("running")) is not bool
                or type(member.get("stop_pending")) is not bool
                or type(member.get("pending_post")) is not bool):
            fail("invalid recovery member")
        if not member["running"] and (member["stop_pending"] or member["pending_post"]):
            fail("recovery must not start or post originally stopped members")
        parse_spec({
            "Config": {"Labels": {VERSION_LABEL: "1",
                                 SPEC_LABEL: json.dumps(member.get("spec"))}},
            "Mounts": [{"Type": "volume", "Destination": destination, "Name": name}
                       for destination, name in member.get("volumes", {}).items()],
        })
    for execution in journal["execs"]:
        if (not isinstance(execution, dict)
                or execution.get("member") not in journal["members"]
                or not ID.fullmatch(execution.get("id", ""))
                or not re.fullmatch(r"/tmp/sophon-exec-[0-9a-f]{32}",
                                    execution.get("pidfile", ""))
                or execution.get("phase") not in ("pre", "backup", "post")):
            fail("invalid recovery execution")


def recover(api, root, command_timeout, poll_interval):
    path = root / "journal.json"
    if not path.exists():
        return
    journal = read_json(path)
    validate_journal(journal)
    failures = finalize(api, root, journal, command_timeout, poll_interval)
    if failures:
        fail("startup recovery incomplete: " + "; ".join(failures))
    if journal["execs"] or any(member["stop_pending"] or member["pending_post"]
                               for member in journal["members"].values()):
        fail("startup recovery has unresolved intents")
    path.unlink()
    sync_dir(root)


def safe_tar_member(member, destination):
    # Podman's stopped-container archive root is an empty-name directory.
    if not member.name and member.isdir():
        member = member.replace(name=".")
    name = member.name
    if (not name or name.startswith("/") or "\\" in name or "\x00" in name
            or ".." in name.split("/")):
        fail("unsafe archive member path")
    if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
        fail("unsupported archive member type")
    if member.issym() or member.islnk():
        link = member.linkname
        origin = posixpath.dirname(name) if member.issym() else ""
        target = posixpath.normpath(posixpath.join(origin, link))
        if (not link or link.startswith("/") or "\\" in link or "\x00" in link
                or target == ".." or target.startswith("../")):
            fail("external archive link")
    return tarfile.data_filter(member, destination)


def archive_payload(path):
    count, payload = 0, 0
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                safe_tar_member(member, str(Path(path).parent / ".validation"))
                count += 1
                if member.isreg():
                    source = archive.extractfile(member)
                    read = 0
                    while chunk := source.read(CHUNK):
                        read += len(chunk)
                    if read != member.size:
                        fail("truncated archive file")
                    payload += read
    except (tarfile.TarError, OSError, EOFError, ValueError):
        fail("invalid or unsafe archive")
    if not count:
        fail("empty archive")
    return payload


def normalize_native_archive(path, source_path, portainer_backup=False):
    with tarfile.open(path, mode="r:*") as original:
        members = original.getmembers()
        if not members:
            return False
        if portainer_backup:
            if not any(member.name.startswith("/") for member in members):
                return False
        elif members[0].name or not members[0].isdir():
            return False
        # Podman's stopped mount archives use an empty root and /child names.
        # Convert that specific native layout to Docker's portable basename root.
        # Portainer's native gzip export instead contains /data-relative names.
        basename = "" if portainer_backup else posixpath.basename(source_path)
        prefix = basename + "/" if basename else ""
        temporary = path.with_name(path.name + ".canonical")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as staging:
                with tarfile.open(fileobj=staging, mode="w:gz" if portainer_backup else "w") as output:
                    for member in members:
                        if not member.name and not member.isdir():
                            fail("unsafe archive member path")
                        name = prefix + member.name.lstrip("/")
                        if not name and member.isdir():
                            name = basename or "."
                        normalized = member.replace(name=name)
                        normalized.pax_headers.pop("path", None)
                        normalized.pax_headers.pop("linkpath", None)
                        if member.islnk():
                            if not member.linkname:
                                fail("external archive link")
                            normalized.linkname = prefix + member.linkname.lstrip("/")
                        elif member.issym() and member.linkname.startswith(source_path + "/"):
                            target = prefix + member.linkname[len(source_path) + 1:]
                            normalized.linkname = posixpath.relpath(target, posixpath.dirname(name))
                        safe_tar_member(normalized, str(path.parent / ".validation"))
                        if member.isreg():
                            with original.extractfile(member) as data:
                                output.addfile(normalized, data)
                        else:
                            output.addfile(normalized)
                staging.flush()
                os.fsync(staging.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return True


def download(api, artifact, member, run_dir, backup_dir):
    parent = private_dir(private_dir(run_dir / "artifacts") / member["spec"]["group"])
    parent = private_dir(parent / member["id"])
    suffix = ".tar.gz" if artifact["type"] == "portainer-backup" else ".tar"
    path = parent / (artifact["name"] + suffix)
    container = unchanged(api, member)
    match artifact["type"]:
        case "archive":
            source_path = container_path(artifact["path"], backup_dir)
            stream = docker(
                api, "GET", f"/containers/{member['id']}/archive?path={quote(source_path, safe='')}",
                stream=True,
            )
        case "portainer-backup":
            if not container["State"]["Running"]:
                fail("native Portainer backup requires a running API")
            source_path = None
            stream = request(api, "POST", "/api/backup", {"password": ""}, stream=True)
        case _:
            fail("unknown artifact type")
    digest, size = hashlib.sha256(), 0
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            while chunk := stream["read"](CHUNK):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if path.exists():
            path.unlink()
        raise
    finally:
        stream["close"]()
    if not size:
        fail("empty artifact download")
    normalized = False
    if artifact["type"] == "portainer-backup":
        normalized = normalize_native_archive(path, "/data", portainer_backup=True)
    elif not container["State"]["Running"]:
        normalized = normalize_native_archive(path, source_path)
    if normalized:
        checksum, size = hash_file(path)
    else:
        checksum = digest.hexdigest()
    payload = archive_payload(path)
    sync_dir(parent)
    sync_dir(parent.parent)
    sync_dir(parent.parent.parent)
    return {
        "member": member["id"], "group": member["spec"]["group"],
        "type": artifact["type"], "name": artifact["name"],
        "path": path.relative_to(run_dir).as_posix(), "container_path": source_path,
        "sha256": checksum, "bytes": size, "payload_bytes": payload,
    }


def prepare(api, root="/source", command_timeout=600, poll_interval=.25):
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in (command_timeout, poll_interval)):
        fail("timeouts and poll interval must be positive")
    root = private_dir(Path(root).absolute())
    fd = os.open(root / ".collector.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    run_dir, run_identity, journal = None, None, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("another collector is preparing this source")
        recover(api, root, command_timeout, poll_interval)
        initial = inventory(api)
        if not initial["members"]:
            fail("no Sophon backup participants")
        for member in initial["members"].values():
            if not member["running"] and member["spec"]["backup"]:
                fail("originally stopped member cannot run backup commands")
        run = (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
               + "-" + uuid.uuid4().hex)
        runs = private_dir(root / "runs")
        run_dir = runs / run
        run_dir.mkdir(mode=0o700)
        created = run_dir.lstat()
        run_identity = (created.st_dev, created.st_ino)
        run_dir = run_dir.resolve(strict=True)
        if run_dir.parent != runs.resolve(strict=True) or run_dir.name != run:
            fail("new staging directory escaped the runs root")
        sync_dir(runs)
        sync_dir(root)
        backup_dir = "/tmp/sophon-backup-" + run
        journal = {
            "version": 1, "run": run, "backup_dir": backup_dir,
            "members": initial["members"], "execs": [],
        }
        for member in journal["members"].values():
            member["pending_post"] = False
            member["stop_pending"] = False
        save_journal(root, journal)
        artifacts = []
        failures = []
        try:
            groups = sorted({member["spec"]["group"]
                             for member in journal["members"].values()})
            for group in groups:
                members = [member for member in journal["members"].values()
                           if member["spec"]["group"] == group]
                try:
                    for member in members:
                        if not member["running"] or not member["spec"]["pre"]:
                            continue
                        member["pending_post"] = True
                        save_journal(root, journal)
                        for index, record in enumerate(member["spec"]["pre"]):
                            run_command(api, root, journal, member, record, "pre", index,
                                        command_timeout, poll_interval)
                    for member in members:
                        if not member["running"] or not member["spec"]["stop"]:
                            continue
                        if not unchanged(api, member)["State"]["Running"]:
                            fail("originally running member stopped externally")
                        member["stop_pending"] = True
                        save_journal(root, journal)
                        docker(api, "POST", f"/containers/{member['id']}/stop?t={member['spec']['stop_timeout']}")
                        state = unchanged(api, member)["State"]
                        if state["Running"]:
                            fail("stop did not stop member")
                        if state.get("OOMKilled") or state.get("ExitCode") == 137:
                            fail(f"member {member['id']} was forcibly killed; refusing an application-consistent backup")
                    for member in members:
                        for index, record in enumerate(member["spec"]["backup"]):
                            run_command(api, root, journal, member, record, "backup", index,
                                        command_timeout, poll_interval)
                    for member in members:
                        owned = []
                        for artifact in member["spec"]["paths"]:
                            try:
                                exported = download(api, artifact, member, run_dir, backup_dir)
                            except RuntimeError as error:
                                fail(f"group {group} artifact {artifact['name']}: {error}")
                            artifacts.append(exported)
                            owned.append(exported)
                        if member["spec"]["covers"] and not sum(
                                artifact["payload_bytes"] for artifact in owned):
                            fail("data owner exported no nonempty protected data")
                finally:
                    failures.extend(finalize(api, root, journal, command_timeout,
                                             poll_interval, [group]))
                if failures:
                    fail("group finalization incomplete")
            final = inventory(api, include_stacks=False)
            if set(initial["members"]) != set(final["members"]):
                fail("backup participant membership changed")
            for member_id, member in initial["members"].items():
                current = final["members"][member_id]
                if member["fingerprint"] != current["fingerprint"]:
                    fail("container name/image/mount replacement detected")
                if member["running"] != current["running"]:
                    fail("original container running state not restored")
            if not sum(artifact["payload_bytes"] for artifact in artifacts):
                fail("run contains no nonempty protected data")
            recipes = []
            for member in initial["members"].values():
                recipes.append({
                    "member": member["id"], "group": member["spec"]["group"],
                    "backup_dir": backup_dir, "commands": member["spec"]["restore"],
                    "covers": member["spec"]["covers"], "volumes": member["volumes"],
                    "artifacts": [artifact["path"] for artifact in artifacts
                                  if artifact["member"] == member["id"]],
                })
            write_json(run_dir / "manifest.json", {
                "version": 1, "run": run,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "endpoint": api["endpoint"], "containers": initial["containers"],
                "stacks": initial["stacks"], "protection": initial["protection"],
                "artifacts": artifacts, "restore": recipes,
                "final_containers": final["containers"],
            })
            sync_dir(run_dir)
        except BaseException as error:
            failures.insert(0, str(error) if isinstance(error, RuntimeError)
                            else "collector interrupted or export failed")
        finally:
            failures.extend(finalize(api, root, journal, command_timeout, poll_interval))
            pending = (journal["execs"] or any(member["stop_pending"] or member["pending_post"]
                                              for member in journal["members"].values()))
            if not pending:
                (root / "journal.json").unlink()
                sync_dir(root)
        if failures:
            # A failed run never exposes a snapshot root, even if bytes were exported.
            fail("backup preparation failed: " + "; ".join(dict.fromkeys(failures)))
        return {"overrideRoot": str(run_dir)}
    except BaseException as error:
        recovered = journal is None or (
            not journal["execs"] and not any(
                member.get("stop_pending", True) or member.get("pending_post", True)
                for member in journal["members"].values()))
        if (run_dir is not None and run_identity is not None and recovered
                and not os.path.lexists(root / "journal.json")):
            try:
                current = run_dir.lstat()
                if (run_dir.is_symlink() or (current.st_dev, current.st_ino) != run_identity
                        or run_dir.parent != runs.resolve(strict=True) or run_dir.name != run):
                    fail("failed-run staging identity changed; cleanup refused")
                # This exact run was never returned to Kopia; do not sweep other runs.
                shutil.rmtree(run_dir)
                sync_dir(runs)
            except BaseException:
                message = str(error) if isinstance(error, RuntimeError) else "collector failed"
                raise RuntimeError(message + "; failed-run staging cleanup failed") from error
        raise
    finally:
        os.close(fd)


def snapshot_file(snapshot, relative):
    if (not isinstance(relative, str) or not relative or relative.startswith("/")
            or "\\" in relative or posixpath.normpath(relative) != relative
            or ".." in relative.split("/")):
        fail("unsafe manifest artifact path")
    path = snapshot
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            fail("snapshot artifact path contains a symlink")
    if not path.is_file():
        fail("missing snapshot artifact")
    return path


def hash_file(path):
    digest, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify(snapshot, output=None, restore_root="/restore"):
    snapshot = Path(snapshot).absolute()
    if not snapshot.is_dir() or snapshot.is_symlink():
        fail("invalid restored snapshot directory")
    if output is not None:
        restore_root = private_dir(Path(restore_root).absolute())
        output = Path(output).absolute()
        try:
            relative = output.relative_to(restore_root)
        except ValueError:
            fail("verification output must be under the restore root")
        if not relative.parts or any(part == ".." for part in relative.parts):
            fail("invalid verification output directory")
        ancestor = restore_root
        for part in relative.parts:
            ancestor = ancestor / part
            if ancestor.is_symlink():
                fail("verification output contains a symlink")
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            fail("verification output must be empty")
        if snapshot == output or snapshot in output.parents or output in snapshot.parents:
            fail("verification output must not overlap snapshot data")
    manifest = read_json(snapshot_file(snapshot, "manifest.json"))
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest["version"] != 1):
        fail("unsupported manifest version")
    artifacts, recipes = manifest.get("artifacts"), manifest.get("restore")
    if not isinstance(artifacts, list) or not artifacts or not isinstance(recipes, list):
        fail("invalid manifest artifacts or recipes")
    seen, checked, total = set(), [], 0
    for artifact in artifacts:
        if (not isinstance(artifact, dict)
                or not ID.fullmatch(artifact.get("member", ""))
                or not GROUP.fullmatch(artifact.get("group", ""))
                or not NAME.fullmatch(artifact.get("name", ""))
                or artifact.get("type") not in ("archive", "portainer-backup")
                or not HASH.fullmatch(artifact.get("sha256", ""))
                or type(artifact.get("bytes")) is not int or artifact["bytes"] <= 0
                or type(artifact.get("payload_bytes")) is not int):
            fail("invalid manifest artifact record")
        suffix = ".tar.gz" if artifact["type"] == "portainer-backup" else ".tar"
        expected = (f"artifacts/{artifact['group']}/{artifact['member']}/"
                    f"{artifact['name']}{suffix}")
        if artifact.get("path") != expected or expected in seen:
            fail("invalid or duplicate manifest artifact location")
        seen.add(expected)
        path = snapshot_file(snapshot, expected)
        digest, size = hash_file(path)
        if digest != artifact["sha256"] or size != artifact["bytes"]:
            fail("snapshot artifact checksum or size mismatch")
        payload = archive_payload(path)
        if payload != artifact["payload_bytes"]:
            fail("snapshot artifact payload mismatch")
        total += payload
        checked.append((artifact, path))
    if not total:
        fail("snapshot has no nonempty protected data")
    participants = {}
    if not isinstance(manifest.get("containers"), list):
        fail("missing deployment inventory")
    for container in manifest["containers"]:
        member = container.get("Id") if isinstance(container, dict) else None
        if not isinstance(member, str) or not ID.fullmatch(member):
            fail("invalid deployment inventory member")
        definition = parse_spec(container)
        if definition is not None:
            if member in participants:
                fail("duplicate deployment inventory member")
            participants[member] = (definition, container)
    restored = set()
    for recipe in recipes:
        if (not isinstance(recipe, dict)
                or set(recipe) != {"member", "group", "backup_dir", "commands",
                                  "covers", "volumes", "artifacts"}
                or not isinstance(recipe.get("member"), str)
                or recipe["member"] not in participants or recipe["member"] in restored
                or not GROUP.fullmatch(recipe.get("group", ""))
                or not isinstance(recipe.get("artifacts"), list)
                or any(path not in seen for path in recipe["artifacts"])):
            fail("invalid restore recipe")
        container_path(recipe.get("backup_dir"))
        command_records(recipe.get("commands"))
        definition, container = participants[recipe["member"]]
        owned = [artifact for artifact in artifacts if artifact["member"] == recipe["member"]]
        volumes = {mount["Destination"]: mount["Name"]
                   for mount in container.get("Mounts", []) if mount.get("Type") == "volume"}
        if (recipe["group"] != definition["group"]
                or recipe["commands"] != definition["restore"]
                or recipe["covers"] != definition["covers"] or recipe["volumes"] != volumes
                or recipe["artifacts"] != [artifact["path"] for artifact in owned]
                or any(artifact["group"] != recipe["group"] for artifact in owned)
                or (definition["covers"] and not sum(item["payload_bytes"] for item in owned))):
            fail("restore recipe does not match deployment inventory and artifacts")
        restored.add(recipe["member"])
    if restored != set(participants) or any(
            artifact["member"] not in restored for artifact in artifacts):
        fail("incomplete restore recipes")
    if output is None:
        return {"manifest": manifest, "artifacts": len(checked), "bytes": total}
    output = private_dir(output)
    for artifact, source in checked:
        destination = private_dir(
            output / artifact["group"] / artifact["member"] / artifact["name"])
        try:
            with tarfile.open(source, mode="r:*") as archive:
                archive.extractall(destination, filter=safe_tar_member)
        except (tarfile.TarError, OSError, EOFError, ValueError):
            fail("safe archive extraction failed")
    recipe_path = output / "restore-recipes.json"
    write_json(recipe_path, {"version": 1, "run": manifest.get("run"), "restore": recipes})
    return {"output": str(output), "recipes": str(recipe_path),
            "artifacts": len(checked), "bytes": total}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("prepare")
    verification = commands.add_parser("verify")
    verification.add_argument("snapshot")
    verification.add_argument("--output", required=True)
    args = parser.parse_args()
    interrupted = False

    def terminate(signum, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError("collector termination requested")

    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.action == "prepare":
            api = portainer_api(
                os.environ.get("PORTAINER_URL", ""),
                os.environ.get("PORTAINER_API_KEY", ""),
                os.environ.get("PORTAINER_ENDPOINT_ID", "1"),
                float(os.environ.get("API_TIMEOUT", "60")),
            )
            result = prepare(
                api, os.environ.get("SOURCE_ROOT", "/source"),
                float(os.environ.get("COMMAND_TIMEOUT", "600")),
            )
        else:
            result = verify(args.snapshot, args.output)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except BaseException as error:
        message = str(error) if isinstance(error, RuntimeError) else "collector failed"
        print(message, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
