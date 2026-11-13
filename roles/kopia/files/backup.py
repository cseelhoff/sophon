#!/usr/bin/env python3
"""V3 filesystem/native-export collector. No Portainer access or restore operations."""
import argparse
import contextlib
import configparser
import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

PREFIX = "sophon.backup."
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IDENTITY = re.compile(r"[a-f0-9]{64}\Z")
CHUNK = 65536


class EngineError(RuntimeError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f"Engine {method} {path} failed: HTTP {status}")


def fail(message):
    raise RuntimeError(message)


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        fail("Private state directory is a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as output:
            temporary.chmod(0o600)
            json.dump(value, output, separators=(",", ":"), ensure_ascii=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(root):
    root = private_dir(root)
    fd = os.open(root / ".collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another backup or recovery holds the collector lock")
        yield root
    finally:
        os.close(fd)


def engine_api(url, timeout=900):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or timeout <= 0):
        fail("ENGINE_URL must be the internal HTTP socket-proxy origin")
    return {"host": parsed.hostname, "port": parsed.port or 2375, "timeout": timeout}


def engine(api, method, path, body=None, stream=False):
    if api.get("transport"):
        return api["transport"](method, path, body, stream)
    connection = http.client.HTTPConnection(api.get("host", "localhost"), api.get("port", 2375), timeout=api["timeout"])
    keep_open = False
    try:
        if "socket" in api:
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(api["timeout"])
            connection.sock.connect(api["socket"])
        connection.request(method, "/v1.41" + path, body=None if body is None else json.dumps(body),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if not 200 <= response.status < 300 and not (
                response.status == 304 and method == "POST" and path.split("?", 1)[0].endswith(("/start", "/stop"))):
            raise EngineError(method, path, response.status)
        if stream:
            deadline = time.monotonic() + api["timeout"]

            def read(size):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Engine command exceeded its deadline")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                return response.read(size)

            keep_open = True
            return {"read": read, "close": lambda: (response.close(), connection.close())}
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            fail("Engine response exceeded the metadata limit")
        return json.loads(data) if data else None
    finally:
        if not keep_open:
            connection.close()


def parse_spec(container):
    fields = {key[len(PREFIX):]: value for key, value in (container["Config"].get("Labels") or {}).items()
              if key.startswith(PREFIX)}
    if not fields:
        return None
    if fields.get("version") != "3":
        fail("Backup labels require version 3; migrate this workload before backing it up")
    if (not NAME.fullmatch(fields.get("group", "")) or fields.get("stop") not in ("true", "false")
            or any(not isinstance(value, str) for value in fields.values())):
        fail("Invalid v3 group or stop declaration")
    if fields["group"] == "manifest.json":
        fail("The group name manifest.json is reserved")
    spec = {"group": fields["group"], "stop": fields["stop"] == "true", "paths": [], "dumps": []}
    collections = {"paths": {}, "dumps": {}}
    for key, value in fields.items():
        if key in ("version", "group", "stop"):
            continue
        match = re.fullmatch(r"(paths|dumps)\.(0|[1-9][0-9]*)\.([a-z]+)", key)
        if not match or len(match[2]) > 6:
            fail("Unknown v3 label: " + key)
        collections[match[1]].setdefault(int(match[2]), {})[match[3]] = value
    names = set()
    for collection, entries in collections.items():
        if sorted(entries) != list(range(len(entries))):
            fail("Noncontiguous v3 label indexes")
        for index in range(len(entries)):
            record = entries[index]
            if not NAME.fullmatch(record.get("name", "")) or record["name"] in names:
                fail("Invalid or duplicate v3 artifact name")
            names.add(record["name"])
            if collection == "paths":
                if (set(record) - {"name", "volume", "path", "optional", "ini"}
                        or not {"name", "volume", "path"} <= record.keys()
                        or not NAME.fullmatch(record["volume"])):
                    fail("Path labels require name, volume and relative path")
                path = record["path"]
                if (not path or path.startswith("/") or "\\" in path
                        or any(ord(char) < 32 for char in path) or ".." in path.split("/")
                        or str(PurePosixPath(path)) != path or record.get("optional", "false") not in ("true", "false")):
                    fail("Unsafe relative volume path or invalid optional flag")
                record["optional"] = record.get("optional", "false") == "true"
                if "ini" in record:
                    keys = json.loads(record["ini"])
                    if (not isinstance(keys, list) or not keys or any(
                            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.[A-Za-z0-9_]+", key)
                            for key in keys)):
                        fail("INI selection must list explicit section.key names")
                    record["ini"] = keys
            else:
                if set(record) - {"name", "argv", "user", "restore", "base"} or "argv" not in record:
                    fail("Dump labels require name and argv")
                if record.get("restore") not in (None, "postgres-cluster", "ldap-subtree"):
                    fail("Unsupported declared native restore type")
                if record.get("restore") == "ldap-subtree" and not record.get("base"):
                    fail("LDAP restoration requires the configured base DN")
                argv = json.loads(record["argv"])
                if (not isinstance(argv, list) or not argv or any(
                        not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)):
                    fail("Dump argv must be a nonempty JSON string array")
                if "user" in record and (not record["user"] or any(
                        ord(char) < 32 for char in record["user"])):
                    fail("Invalid dump user")
                record["argv"] = argv
            spec[collection].append(record)
    return spec


def signature(container):
    facts = {"id": container["Id"], "name": container["Name"], "image": container["Image"],
             "mounts": sorted([{key: mount.get(key) for key in ("Type", "Name", "Destination", "RW")}
                               for mount in container.get("Mounts", [])],
                              key=lambda mount: json.dumps(mount, sort_keys=True))}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def inspect(api, member):
    current = engine(api, "GET", f"/containers/{member['id']}/json")
    if current["Id"] != member["id"] or signature(current) != member["signature"]:
        fail("Container image/name/mount identity changed: " + member["name"])
    return current


def inventory(api):
    groups, attached, declared = {}, set(), set()
    containers = []
    for item in engine(api, "GET", "/containers/json?all=true"):
        if not IDENTITY.fullmatch(item["Id"]):
            fail("Invalid container ID in engine inventory")
        current = engine(api, "GET", f"/containers/{item['Id']}/json")
        if current["Id"] != item["Id"] or type(current["State"]["Running"]) is not bool:
            fail("Invalid container identity/state")
        containers.append(current)
        attached.update(m["Name"] for m in current.get("Mounts", []) if m.get("Type") == "volume")
        labels = current["Config"].get("Labels") or {}
        if not any(key.startswith(PREFIX) for key in labels):
            continue
        name = current["Name"].lstrip("/")
        if not NAME.fullmatch(name):
            fail("Unsafe container name")
        group_name = labels.get(PREFIX + "group", "invalid-" + current["Id"][:12])
        if not NAME.fullmatch(group_name):
            group_name = "invalid-" + current["Id"][:12]
        group = groups.setdefault(group_name, {"members": [], "errors": []})
        try:
            spec = parse_spec(current)
        except (RuntimeError, ValueError) as error:
            group["errors"].append(f"{name}: {error}")
            continue
        member = {"id": current["Id"], "name": name, "signature": signature(current),
                  "running": current["State"]["Running"], "spec": spec,
                  "image": current["Config"]["Image"], "image_id": current["Image"],
                  "volumes": {m["Name"] for m in current.get("Mounts", []) if m.get("Type") == "volume"}}
        group["members"].append(member)
        declared.update(path["volume"] for path in spec["paths"])
    return groups, sorted(attached - declared), containers


def exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream["read"](size - len(data))
        if not chunk:
            fail("Truncated engine exec frame")
        data.extend(chunk)
    return bytes(data)


def output_stream(stream, output=None):
    digest, size, diagnostic = hashlib.sha256(), 0, bytearray()
    try:
        while header := stream["read"](8):
            header += exact(stream, 8 - len(header))
            channel, remaining = struct.unpack(">BxxxI", header)
            if channel not in (1, 2):
                fail("Invalid engine exec channel")
            while remaining:
                chunk = exact(stream, min(CHUNK, remaining))
                remaining -= len(chunk)
                if channel == 1:
                    if output is not None:
                        output.write(chunk)
                    else:
                        if len(diagnostic) + len(chunk) > CHUNK:
                            fail("Control-command output exceeds limit")
                        diagnostic.extend(chunk)
                    digest.update(chunk)
                    size += len(chunk)
    finally:
        stream["close"]()
    return {"sha256": digest.hexdigest(), "bytes": size, "output": bytes(diagnostic)}


def control(api, member, argv, user=None):
    body = {"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": argv}
    if user:
        body["User"] = user
    execution = engine(api, "POST", f"/containers/{member}/exec", body)["Id"]
    result = output_stream(engine(api, "POST", f"/exec/{execution}/start",
                                  {"Detach": False, "Tty": False}, stream=True))
    state = engine(api, "GET", f"/exec/{execution}/json")
    if state["Running"] or state.get("ExitCode") != 0:
        fail("Recovery control command failed")
    return result["output"]


def recover(api, root):
    path = root / "journal.json"
    if not path.exists():
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        journal = json.loads(source.read(16 * 1024 * 1024))
    if journal.get("version") != 3 or set(journal) != {"version", "run", "members", "execs"}:
        fail("Unsupported recovery journal; use the matching tool, never discard pending recovery")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}", journal.get("run", "")):
        fail("Invalid recovery staging identity")
    if not isinstance(journal["members"], dict) or not isinstance(journal["execs"], list):
        fail("Invalid recovery journal collections")
    for key, member in journal["members"].items():
        if (not IDENTITY.fullmatch(key) or member.get("id") != key
                or not IDENTITY.fullmatch(member.get("signature", ""))
                or not NAME.fullmatch(member.get("name", "")) or not NAME.fullmatch(member.get("group", ""))
                or type(member.get("stop_pending")) is not bool):
            fail("Invalid recovery member identity")
    for execution in journal["execs"]:
        if (execution.get("member") not in journal["members"]
                or not IDENTITY.fullmatch(execution.get("id", ""))
                or not re.fullmatch(r"/tmp/sophon-dump-[0-9a-f]{32}", execution.get("pidfile", ""))):
            fail("Invalid pending native export identity")
    errors = finalize(api, root, journal)
    if errors:
        fail("Interrupted-stop recovery incomplete: " + "; ".join(errors))
    path.unlink()
    staging = private_dir(root / "runs") / journal["run"]
    if staging.is_symlink():
        fail("Interrupted staging path is a symlink")
    if staging.exists():
        shutil.rmtree(staging)


def finalize(api, root, journal, groups=None):
    errors = []
    for execution in list(journal["execs"]):
        member = journal["members"][execution["member"]]
        if groups is not None and member["group"] not in groups:
            continue
        try:
            inspect(api, member)
            state = engine(api, "GET", f"/exec/{execution['id']}/json")
            if state["Running"]:
                pid = control(api, member["id"], ["cat", execution["pidfile"]], execution.get("user")).strip()
                if not re.fullmatch(rb"[0-9]{1,10}", pid) or not 1 < int(pid) < 2147483647:
                    fail("Invalid dump process-group identity")
                for sig, grace in (("TERM", 2), ("KILL", 10)):
                    if not engine(api, "GET", f"/exec/{execution['id']}/json")["Running"]:
                        break
                    control(api, member["id"], [
                        "sh", "-ceu", 'IFS= read -r current < "$1"; test "$current" = "$2"; '
                        f'kill -{sig} "-$current"', "sophon-cancel", execution["pidfile"], pid.decode(),
                    ], execution.get("user"))
                    deadline = time.monotonic() + grace
                    while engine(api, "GET", f"/exec/{execution['id']}/json")["Running"]:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(.1)
                if engine(api, "GET", f"/exec/{execution['id']}/json")["Running"]:
                    fail("Dump remains running")
            journal["execs"].remove(execution)
            write_json(root / "journal.json", journal)
        except (RuntimeError, OSError, KeyError) as error:
            errors.append(f"{member['name']}: dump recovery failed: {error}")
    pending = {execution["member"] for execution in journal["execs"]}
    pending_groups = {journal["members"][member]["group"] for member in pending}
    for member in reversed(list(journal["members"].values())):
        if (not member["stop_pending"] or member["group"] in pending_groups
                or (groups is not None and member["group"] not in groups)):
            continue
        try:
            if inspect(api, member)["State"]["Running"]:
                # Settle a stop whose response was lost before issuing its compensating start.
                engine(api, "POST", f"/containers/{member['id']}/stop?t=120")
                if inspect(api, member)["State"]["Running"]:
                    fail("Pending stop has not completed")
            engine(api, "POST", f"/containers/{member['id']}/start")
            if not inspect(api, member)["State"]["Running"]:
                fail("Writer did not restart")
            member["stop_pending"] = False
            write_json(root / "journal.json", journal)
        except (RuntimeError, OSError, KeyError) as error:
            errors.append(f"{member['name']}: restart failed: {error}")
    return errors


def dump(api, root, journal, member, record, target):
    if not inspect(api, member)["State"]["Running"]:
        fail("Native export requires a running container: " + member["name"])
    pidfile = "/tmp/sophon-dump-" + uuid.uuid4().hex
    body = {
        "AttachStdout": True, "AttachStderr": True, "Tty": False,
        "Cmd": ["sh", "-ceu",
                'setsid sh -ceu \'umask 077; printf "%s\\n" "$$" > "$1"; shift; exec "$@"\' '
                'sophon "$@" & child=$!; wait "$child"', "sophon", pidfile, *record["argv"]],
    }
    if "user" in record:
        body["User"] = record["user"]
    execution = engine(api, "POST", f"/containers/{member['id']}/exec", body)["Id"]
    if not IDENTITY.fullmatch(execution):
        fail("Invalid dump exec identity")
    pending = {"id": execution, "member": member["id"], "pidfile": pidfile}
    if "user" in record:
        pending["user"] = record["user"]
    journal["execs"].append(pending)
    write_json(root / "journal.json", journal)
    with target.open("xb") as output:
        target.chmod(0o600)
        result = output_stream(engine(api, "POST", f"/exec/{execution}/start",
                                      {"Detach": False, "Tty": False}, stream=True), output)
        output.flush()
        os.fsync(output.fileno())
    state = engine(api, "GET", f"/exec/{execution}/json")
    if state["Running"] or state.get("ExitCode") != 0:
        fail(f"{member['name']} native export {record['name']} failed (exit {state.get('ExitCode')})")
    if result["bytes"] == 0:
        fail(f"{member['name']} native export {record['name']} returned empty stdout")
    journal["execs"].remove(pending)
    write_json(root / "journal.json", journal)
    return {"name": record["name"], "member": member["name"],
            "sha256": result["sha256"], "bytes": result["bytes"]}


def clone(source, target):
    result = subprocess.run(["cp", "--archive", "--reflink=always", "--", str(source), str(target)],
                            capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"), timeout=900)
    mode = "reflink"
    if result.returncode and any(reason in result.stderr for reason in (
            "Operation not supported", "Invalid cross-device link", "Invalid argument")):
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        result = subprocess.run(["cp", "--archive", "--reflink=never", "--", str(source), str(target)],
                                capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"), timeout=900)
        mode = "copy"
    if result.returncode:
        fail("Staging clone failed: " + result.stderr.strip())
    return mode


def select_ini(path, keys):
    parser = configparser.ConfigParser(interpolation=None, allow_unnamed_section=True, strict=False)
    try:
        parser.read_string(path.read_text())
    except (configparser.Error, UnicodeError):
        fail("INI identity selection could not parse the configured file")
    selected = configparser.ConfigParser(interpolation=None)
    selected.optionxform = str
    for selector in keys:
        section, key = selector.rsplit(".", 1)
        if parser.has_option(section, key):
            if not selected.has_section(section):
                selected.add_section(section)
            selected.set(section, key, parser.get(section, key, raw=True))
    attributes = path.stat()
    with path.open("w") as output:
        selected.write(output)
    os.chmod(path, attributes.st_mode & 0o7777)
    os.chown(path, attributes.st_uid, attributes.st_gid)
    os.utime(path, ns=(attributes.st_atime_ns, attributes.st_mtime_ns))


def collect(api, volumes, root, version):
    recover(api, root)
    if not version.strip():
        fail("Sophon version is required")
    groups, undeclared, containers = inventory(api)
    run = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    directory = private_dir(private_dir(root / "runs") / run)
    manifest = {"version": 3, "run": run, "sophon_version": version, "created_at":
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "groups": [], "failures": [], "undeclared_volumes": undeclared}
    journal = {"version": 3, "run": run, "members": {}, "execs": []}
    for group_name, group in sorted(groups.items()):
        group_dir = private_dir(directory / group_name)
        record = {"name": group_name, "members": [], "paths": [], "dumps": []}
        captured = False
        try:
            if group["errors"]:
                fail("; ".join(group["errors"]))
            for member in group["members"]:
                record["members"].append({key: member[key] for key in ("name", "image", "image_id")})
                for item in member["spec"]["paths"]:
                    volume = item["volume"]
                    if volume not in volumes:
                        fail(f"Volume {volume} is not mounted into Kopia. Add \"{volume}\" to "
                             "SOPHON_KOPIA_EXTRA_VOLUMES and redeploy the Kopia stack")
                    if volume not in member["volumes"]:
                        fail(f"Declared volume {volume} is not attached to {member['name']}")
                    for writer in containers:
                        writes = writer["State"]["Running"] and any(
                            m.get("Type") == "volume" and m.get("Name") == volume and m.get("RW", True)
                            for m in writer.get("Mounts", []))
                        if writes and not any(m["id"] == writer["Id"] and m["spec"]["stop"]
                                              for m in group["members"]):
                            fail(f"Volume {volume} has an unpaused writer {writer['Name']}; declare it "
                                 f"in group {group_name} with stop=true")
            for member in group["members"]:
                journal["members"][member["id"]] = {
                    key: member[key] for key in ("id", "name", "signature")}
                journal["members"][member["id"]].update(group=group_name, stop_pending=False)
            for member in group["members"]:
                if member["running"] and member["spec"]["stop"]:
                    if not inspect(api, member)["State"]["Running"]:
                        fail("Writer stopped externally: " + member["name"])
                    journal["members"][member["id"]]["stop_pending"] = True
                    persisted = False
                    try:
                        write_json(root / "journal.json", journal)
                        persisted = True
                    finally:
                        if not persisted:
                            journal["members"][member["id"]]["stop_pending"] = False
                    engine(api, "POST", f"/containers/{member['id']}/stop?t=120")
                    state = inspect(api, member)["State"]
                    if state["Running"] or state.get("ExitCode") == 137 or state.get("OOMKilled"):
                        fail("Writer did not stop gracefully: " + member["name"])
            for member in group["members"]:
                member_dir = private_dir(group_dir / member["name"])
                for item in member["spec"]["paths"]:
                    source_root = Path(volumes[item["volume"]]).resolve(strict=True)
                    source = source_root / item["path"]
                    if not source.resolve().is_relative_to(source_root):
                        fail("Declared path escapes its mounted volume")
                    present = source.exists() or source.is_symlink()
                    artifact = {**item, "member": member["name"], "present": present,
                                "snapshot_path": f"{group_name}/{member['name']}/{item['name']}"}
                    if not present:
                        if not item["optional"]:
                            fail("Required irreproducible path is missing: " + item["path"])
                    else:
                        if "ini" in item and (source.is_symlink() or not source.is_file()):
                            fail("INI identity selection requires a regular file, not a symlink or directory")
                        artifact["copy"] = clone(source, member_dir / item["name"])
                        if "ini" in item:
                            select_ini(member_dir / item["name"], item["ini"])
                    record["paths"].append(artifact)
                for item in member["spec"]["dumps"]:
                    exported = dump(api, root, journal, member, item, member_dir / item["name"])
                    exported["snapshot_path"] = f"{group_name}/{member['name']}/{item['name']}"
                    record["dumps"].append(exported)
            for member in group["members"]:
                inspect(api, member)
            protected = {item["volume"] for member in group["members"] for item in member["spec"]["paths"]}
            if protected:
                for entry in engine(api, "GET", "/containers/json?all=true"):
                    current = engine(api, "GET", f"/containers/{entry['Id']}/json")
                    if current["State"]["Running"] and any(
                            mount.get("Type") == "volume" and mount.get("Name") in protected
                            and mount.get("RW", True) for mount in current.get("Mounts", [])):
                        fail("A volume writer restarted or appeared during collection: " + current["Name"])
            captured = True
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            manifest["failures"].append({"group": group_name, "error": str(error)})
            shutil.rmtree(group_dir)
        finally:
            errors = finalize(api, root, journal, {group_name})
            if errors:
                manifest["failures"].append({"group": group_name, "error": "; ".join(errors)})
                if group_dir.exists():
                    shutil.rmtree(group_dir)
        if captured and not errors:
            manifest["groups"].append(record)
    if not journal["execs"] and not any(m["stop_pending"] for m in journal["members"].values()):
        (root / "journal.json").unlink(missing_ok=True)
    manifest["status"] = "partial" if manifest["failures"] else "complete"
    write_json(directory / "manifest.json", manifest)
    write_json(root / "last-run.json", dict(manifest, upload_status="pending"))
    for failure in manifest["failures"]:
        print(f"ERROR group={failure['group']}: {failure['error']}", file=sys.stderr)
    print("Undeclared volumes (not copied): " + ", ".join(undeclared), file=sys.stderr)
    return {"path": str(directory), "manifest": manifest}


def prepare(api, volumes, root, version):
    with locked(root) as directory:
        return collect(api, volumes, directory, version)


def run(environment):
    api = engine_api(environment["ENGINE_URL"], float(environment.get("COMMAND_TIMEOUT", "900")))
    volumes = json.loads(environment["BACKUP_VOLUMES"])
    if not isinstance(volumes, dict) or any(
            not NAME.fullmatch(name) or not isinstance(path, str) or not Path(path).is_absolute()
            for name, path in volumes.items()):
        fail("BACKUP_VOLUMES must map named volumes to absolute read-only mount paths")
    with locked(environment.get("SOURCE_ROOT", "/source")) as root:
        result = collect(api, volumes, root, environment["SOPHON_VERSION"])
        manifest = result["manifest"]
        identity = environment["KOPIA_CLIENT_USERNAME"] + "@" + environment["KOPIA_CLIENT_HOSTNAME"] + ":/sophon-state"
        upload = subprocess.run(
            ["kopia", "snapshot", "create", result["path"], "--override-source=" + identity, "--json", "--fail-fast"],
            capture_output=True, text=True, env=environment, timeout=86400)
        if upload.returncode:
            detail = upload.stderr
            for name in ("KOPIA_PASSWORD", "KOPIA_SERVER_PASSWORD", "KOPIA_SFTP_PRIVATE_KEY_B64"):
                if environment.get(name):
                    detail = detail.replace(environment[name], "[redacted]")
            manifest["upload_error"] = detail.strip()
            manifest["upload_status"] = "failed"
            write_json(root / "last-run.json", manifest)
            fail("Kopia upload failed; immutable staging retained: " + detail.strip())
        try:
            snapshot = json.loads(upload.stdout)
            valid = (isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str)
                     and re.fullmatch(r"[a-f0-9]{32}", snapshot["id"])
                     and snapshot["rootEntry"]["summ"]["numFailed"] == 0)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            manifest["upload_status"] = "failed"
            manifest["upload_error"] = "Kopia returned an invalid or incomplete snapshot result"
            write_json(root / "last-run.json", manifest)
            fail(manifest["upload_error"] + "; immutable staging retained")
        manifest["snapshot"] = snapshot
        manifest["upload_status"] = "complete"
        write_json(root / "last-run.json", manifest)
        shutil.rmtree(result["path"])
        print(json.dumps({"status": manifest["status"], "failures": manifest["failures"],
                          "snapshot": manifest["snapshot"]}))
        return 1 if manifest["failures"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "recover"))
    args = parser.parse_args()
    os.umask(0o077)
    def terminate(signum, frame):
        raise KeyboardInterrupt("Backup termination requested")

    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.action == "run":
            return run(dict(os.environ))
        with locked(os.environ.get("SOURCE_ROOT", "/source")) as root:
            recover(engine_api(os.environ["ENGINE_URL"]), root)
        return 0
    except KeyboardInterrupt:
        print("Backup interrupted; pending stops/exports remain journaled for recovery.", file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print("Backup failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
