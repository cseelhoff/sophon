#!/usr/bin/env python3
"""Hot filesystem/native-export Kopia snapshot actions and label parser."""
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
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
import uuid

PREFIX = "sophon.backup."
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IDENTITY = re.compile(r"[a-f0-9]{64}\Z")
INI_KEY = re.compile(r"[A-Za-z0-9_.-]+\.[A-Za-z0-9_]+\Z")
CHUNK = 65536


class EngineError(RuntimeError):
    def __init__(self, method, path, status, detail=""):
        self.status = status
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"Engine {method} {path} failed: HTTP {status}{suffix}")


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


def cleanup_stale_runs(root):
    runs_dir = Path(root) / "runs"
    if runs_dir.is_symlink():
        fail("Runs staging directory is a symlink")
    if not runs_dir.exists():
        return
    for entry in runs_dir.iterdir():
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)


def engine_api(url, timeout=900):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or timeout <= 0):
        fail("ENGINE_URL must be the internal HTTP dump-proxy origin")
    return {"host": parsed.hostname, "port": parsed.port or 2375, "timeout": timeout}


def _http_request(api, method, raw_path, body=None, stream=False):
    connection = http.client.HTTPConnection(api.get("host", "localhost"), api.get("port", 2375), timeout=api["timeout"])
    keep_open = False
    try:
        if "socket" in api:
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(api["timeout"])
            connection.sock.connect(api["socket"])
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, raw_path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300 and not (
                response.status == 304 and method == "POST" and raw_path.split("?", 1)[0].endswith(("/start", "/stop"))):
            detail = ""
            try:
                payload = json.loads(response.read(8192))
                if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                    detail = payload["error"]
            except (ValueError, OSError):
                pass
            raise EngineError(method, raw_path, response.status, detail)
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


def engine(api, method, path, body=None, stream=False):
    if api.get("transport"):
        return api["transport"](method, path, body, stream)
    return _http_request(api, method, "/v1.41" + path, body=body, stream=stream)


def proxy_inventory(api):
    if api.get("proxy"):
        return api["proxy"].inventory()
    if api.get("transport") or "socket" in api:
        import dump_proxy
        return dump_proxy.ProxyService(api).inventory()
    return _http_request(api, "GET", "/v1/inventory")


def proxy_dump_stream(api, container_id, dump_name):
    if api.get("proxy") or api.get("transport") or "socket" in api:
        import dump_proxy
        service = api["proxy"] if api.get("proxy") else dump_proxy.ProxyService(api)
        buffer = bytearray()
        try:
            service.stream_dump(container_id, dump_name, buffer.extend)
        except dump_proxy.ProxyError as error:
            raise EngineError("POST", f"/v1/dump/{container_id}/{dump_name}", error.status, str(error)) from error
        offset = 0

        def read(size):
            nonlocal offset
            chunk = bytes(buffer[offset:offset + size])
            offset += len(chunk)
            return chunk

        return {"read": read, "close": lambda: None}
    return _http_request(api, "POST", f"/v1/dump/{container_id}/{quote(dump_name, safe='')}", stream=True)


def _slug(value):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:96] or "item"


def _unique_name(used, preferred):
    candidate = preferred if NAME.fullmatch(preferred) else _slug(preferred)
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}-{index}" in used:
        index += 1
    chosen = f"{candidate}-{index}"
    used.add(chosen)
    return chosen


def _validate_relpath(path):
    if (not path or path.startswith("/") or "\\" in path
            or any(ord(char) < 32 for char in path) or ".." in path.split("/")
            or str(PurePosixPath(path)) != path):
        fail("Unsafe relative volume path: " + repr(path))
    return path


def _volume_mounts(container):
    result = []
    for mount in (container.get("Mounts") or []):
        if mount.get("Type") == "volume" and isinstance(mount.get("Name"), str) and NAME.fullmatch(mount["Name"]):
            dest = (mount.get("Destination") or "").rstrip("/")
            result.append({"Name": mount["Name"], "Destination": dest, "RW": mount.get("RW", True)})
    return result


def _resolve_volume_mount(container, ref):
    ref = ref.strip()
    if not ref:
        fail("Empty backup volume reference")
    mounts = _volume_mounts(container)
    project = ((container.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project", "")
    if ref.startswith("/"):
        normalized = ref.rstrip("/")
        if not normalized:
            fail("Root '/' cannot be backed up as a named volume")
        matches = [m for m in mounts if m["Destination"] == normalized]
    else:
        matches = [m for m in mounts if m["Name"] == ref]
        if not matches and project:
            matches = [m for m in mounts if m["Name"] == f"{project}_{ref}"]
    if len(matches) != 1:
        cname = (container.get("Name") or "").lstrip("/") or container.get("Id", "container")[:12]
        fail(f"Declared backup volume or mount {ref!r} is not an attached named volume on {cname}")
    return matches[0]


def _resolve_volume_ref(container, ref):
    return _resolve_volume_mount(container, ref)["Name"]


def _resolve_path_spec(container, raw_spec):
    raw_spec = raw_spec.strip()
    optional = False
    if "?" in raw_spec:
        raw_spec, query = raw_spec.split("?", 1)
        for token in query.split("&"):
            if token == "optional=true":
                optional = True
            elif token == "optional=false":
                optional = False
            else:
                fail("Unsupported path option: " + token)
    mounts = sorted(_volume_mounts(container), key=lambda m: len(m["Destination"]), reverse=True)
    if raw_spec.startswith("/"):
        for mount in mounts:
            prefix = mount["Destination"]
            if prefix and raw_spec.startswith(prefix + "/"):
                relpath = _validate_relpath(raw_spec[len(prefix) + 1:])
                return mount["Name"], relpath, optional
        cname = (container.get("Name") or "").lstrip("/") or container.get("Id", "container")[:12]
        fail(f"Declared backup path {raw_spec!r} is not inside any attached named volume on {cname}")
    if ":" not in raw_spec:
        fail("Path backup label must be /<mount>/<relpath> or <volume>:<relpath>")
    vol_ref, rel_raw = raw_spec.split(":", 1)
    return _resolve_volume_ref(container, vol_ref), _validate_relpath(rel_raw), optional


def _parse_excludes(raw):
    if not raw:
        return []
    patterns = []
    for chunk in re.split(r"[,\n]", raw):
        pattern = chunk.strip()
        if not pattern:
            continue
        if "\x00" in pattern or "\r" in pattern:
            fail("Invalid control character in sophon.backup.exclude")
        patterns.append(pattern)
    return patterns


def _parse_command(raw):
    text = raw.strip()
    if not text:
        fail("Backup dump command cannot be empty")
    if text.startswith("["):
        argv = json.loads(text)
        if (not isinstance(argv, list) or not argv or any(
                not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)):
            fail("Dump JSON command must be a non-empty string array")
        return argv
    if "\x00" in text:
        fail("Invalid null byte in backup dump command")
    return ["sh", "-ec", text]


def _default_dump_name(restore_kind):
    if restore_kind == "postgres-cluster":
        return "postgres-cluster.sql"
    if restore_kind == "postgres-custom":
        return "database.dump"
    if restore_kind == "ldap-subtree":
        return "directory.ldif"
    return "dump.out"


def _build_dump(raw_cmd, name=None, user=None, restore=None, base=None, used_names=None):
    if restore not in (None, "postgres-cluster", "postgres-custom", "ldap-subtree"):
        fail("Unsupported declared native restore type: " + repr(restore))
    if restore == "ldap-subtree" and not base:
        fail("LDAP restoration requires sophon.backup.dump.base")
    if base is not None and restore != "ldap-subtree":
        fail("sophon.backup.dump.base is only valid with restore=ldap-subtree")
    argv = _parse_command(raw_cmd)
    chosen_name = name if name else _default_dump_name(restore)
    if not NAME.fullmatch(chosen_name) or (used_names is not None and chosen_name in used_names):
        fail("Invalid or duplicate backup artifact name: " + repr(chosen_name))
    if used_names is not None:
        used_names.add(chosen_name)
    record = {"name": chosen_name, "argv": argv}
    if user is not None:
        if not user or any(ord(char) < 32 for char in user):
            fail("Invalid dump user")
        record["user"] = user
    if restore is not None:
        record["restore"] = restore
    if base is not None:
        record["base"] = base
    return record


def parse_spec(container):
    labels = (container.get("Config") or {}).get("Labels") or {}
    fields = {key[len(PREFIX):]: value for key, value in labels.items() if key.startswith(PREFIX)}
    if not fields:
        return None
    if any(not isinstance(value, str) for value in fields.values()):
        fail("Backup label values must be strings")
    if "version" in fields or "stop" in fields:
        fail("Legacy sophon.backup.version/stop labels are retired; set sophon.backup.enable=true")
    enable = fields.get("enable")
    if enable not in ("true", "false"):
        fail("sophon.backup.enable must be explicitly 'true' or 'false'")
    if enable == "false":
        unknown = set(fields) - {"enable", "group"}
        if unknown:
            fail("Disabled backup container has unexpected labels: " + ", ".join(sorted(unknown)))
        return None

    default_group = labels.get("com.docker.compose.project", "").strip() or container["Name"].lstrip("/")
    group = fields.get("group", default_group).strip()
    if not NAME.fullmatch(group):
        fail("Invalid sophon.backup.group name: " + repr(group))
    if group == "manifest.json":
        fail("The group name manifest.json is reserved")

    known_top = {
        "enable", "group", "volumes", "exclude", "paths",
        "ini", "ini.keys",
        "dump", "dump.name", "dump.user", "dump.restore", "dump.base",
    }
    named_dumps = {}
    for key, value in fields.items():
        if key in known_top:
            continue
        match = re.fullmatch(r"dumps\.([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\.(cmd|argv|name|user|restore|base)", key)
        if not match:
            fail("Unknown backup label: " + PREFIX + key)
        named_dumps.setdefault(match.group(1), {})[match.group(2)] = value

    if "ini.keys" in fields and "ini" not in fields:
        fail("sophon.backup.ini.keys requires sophon.backup.ini")
    if any(k.startswith("dump.") for k in fields) and "dump" not in fields:
        fail("sophon.backup.dump.* attributes require sophon.backup.dump")

    used_names = set()
    paths = []
    excludes = _parse_excludes(fields.get("exclude", ""))

    if "volumes" in fields:
        raw_vols = [entry.strip() for entry in fields["volumes"].split(",") if entry.strip()]
        if not raw_vols:
            fail("sophon.backup.volumes cannot be empty")
        seen_vols = set()
        for vol_ref in raw_vols:
            mount_info = _resolve_volume_mount(container, vol_ref)
            vol_name = mount_info["Name"]
            if vol_name in seen_vols:
                fail("Duplicate volume in sophon.backup.volumes: " + vol_name)
            seen_vols.add(vol_name)
            dest_slug = _slug(mount_info["Destination"].lstrip("/") or vol_name)
            paths.append({
                "name": _unique_name(used_names, "volume-" + dest_slug),
                "volume": vol_name,
                "path": ".",
                "optional": False,
                "exclude": list(excludes),
            })
    elif excludes:
        fail("sophon.backup.exclude requires sophon.backup.volumes")

    if "paths" in fields:
        raw_paths = [entry.strip() for entry in fields["paths"].split(",") if entry.strip()]
        if not raw_paths:
            fail("sophon.backup.paths cannot be empty")
        for raw_path in raw_paths:
            vol_name, relpath, optional = _resolve_path_spec(container, raw_path)
            paths.append({
                "name": _unique_name(used_names, "path-" + _slug(relpath)),
                "volume": vol_name,
                "path": relpath,
                "optional": optional,
            })

    if "ini" in fields:
        ini_raw = fields["ini"].strip()
        if "ini.keys" in fields:
            ini_target = ini_raw
            key_list_raw = fields["ini.keys"]
        else:
            if ":" not in ini_raw:
                fail("sophon.backup.ini must be <path>:<section.key>,... or paired with sophon.backup.ini.keys")
            ini_target, key_list_raw = ini_raw.rsplit(":", 1)
        vol_name, relpath, optional = _resolve_path_spec(container, ini_target)
        if optional:
            fail("INI identity file cannot be optional")
        keys = [k.strip() for k in key_list_raw.split(",") if k.strip()]
        if not keys or any(not INI_KEY.fullmatch(k) for k in keys):
            fail("INI selection must list explicit section.key names")
        paths.append({
            "name": _unique_name(used_names, "ini-" + _slug(PurePosixPath(relpath).name)),
            "volume": vol_name,
            "path": relpath,
            "optional": False,
            "ini": keys,
        })
        for entry in paths:
            if entry["volume"] == vol_name and entry["path"] == ".":
                auto_exclude = "/" + relpath
                if auto_exclude not in entry["exclude"] and relpath not in entry["exclude"]:
                    entry["exclude"].append(auto_exclude)

    dumps = []
    if "dump" in fields:
        dumps.append(_build_dump(
            fields["dump"],
            name=fields.get("dump.name"),
            user=fields.get("dump.user"),
            restore=fields.get("dump.restore"),
            base=fields.get("dump.base"),
            used_names=used_names,
        ))

    for dump_key in sorted(named_dumps):
        entry = named_dumps[dump_key]
        if ("cmd" in entry) == ("argv" in entry):
            fail(f"sophon.backup.dumps.{dump_key} requires cmd (or argv)")
        raw_cmd = entry["cmd"] if "cmd" in entry else entry["argv"]
        if "argv" in entry and not raw_cmd.lstrip().startswith("["):
            fail(f"sophon.backup.dumps.{dump_key}.argv must be a JSON array")
        dumps.append(_build_dump(
            raw_cmd,
            name=entry.get("name", dump_key),
            user=entry.get("user"),
            restore=entry.get("restore"),
            base=entry.get("base"),
            used_names=used_names,
        ))

    if not paths and not dumps:
        fail("sophon.backup.enable=true requires at least one volumes, paths, ini, or dump label")

    return {"group": group, "paths": paths, "dumps": dumps}


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
    containers = proxy_inventory(api)
    for current in containers:
        if not IDENTITY.fullmatch(current.get("Id", "")) or type((current.get("State") or {}).get("Running")) is not bool:
            fail("Invalid container identity/state in inventory")
        attached.update(m["Name"] for m in current.get("Mounts", []) if m.get("Type") == "volume")
        labels = (current.get("Config") or {}).get("Labels") or {}
        if not any(key.startswith(PREFIX) for key in labels):
            continue
        name = current["Name"].lstrip("/")
        if not NAME.fullmatch(name):
            fail("Unsafe container name")
        default_group = labels.get("com.docker.compose.project", "").strip() or name
        group_name = labels.get(PREFIX + "group", default_group).strip()
        if not NAME.fullmatch(group_name):
            group_name = "invalid-" + current["Id"][:12]
        try:
            spec = parse_spec(current)
        except (RuntimeError, ValueError) as error:
            group = groups.setdefault(group_name, {"members": [], "errors": []})
            group["errors"].append(f"{name}: {error}")
            continue
        if spec is None:
            continue
        group = groups.setdefault(spec["group"], {"members": [], "errors": []})
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


def output_stream(stream, output=None, expect_trailer=False):
    digest, size, diagnostic, stderr_buf = hashlib.sha256(), 0, bytearray(), bytearray()
    trailer = None
    try:
        while header := stream["read"](8):
            header += exact(stream, 8 - len(header))
            channel, remaining = struct.unpack(">BxxxI", header)
            if channel == 0 and expect_trailer:
                payload = exact(stream, remaining)
                trailer = json.loads(payload.decode("utf-8"))
                continue
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
                elif len(stderr_buf) < 16384:
                    stderr_buf.extend(chunk[:16384 - len(stderr_buf)])
    finally:
        stream["close"]()
    return {
        "sha256": digest.hexdigest(),
        "bytes": size,
        "output": bytes(diagnostic),
        "stderr": stderr_buf.decode("utf-8", errors="replace").strip(),
        "trailer": trailer,
    }


def control(api, member, argv, user=None):
    body = {"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": argv}
    if user:
        body["User"] = user
    execution = engine(api, "POST", f"/containers/{member}/exec", body)["Id"]
    result = output_stream(engine(api, "POST", f"/exec/{execution}/start",
                                  {"Detach": False, "Tty": False}, stream=True))
    state = engine(api, "GET", f"/exec/{execution}/json")
    if state["Running"] or state.get("ExitCode") != 0:
        detail = f": {result['stderr']}" if result["stderr"] else ""
        fail("Recovery control command failed" + detail)
    return result["output"]


def dump(api, member, record, target):
    if not member["running"]:
        fail("Native export requires a running container: " + member["name"])
    stream = proxy_dump_stream(api, member["id"], record["name"])
    with target.open("xb") as output:
        target.chmod(0o600)
        result = output_stream(stream, output=output, expect_trailer=True)
        output.flush()
        os.fsync(output.fileno())
    trailer = result["trailer"]
    if trailer is None:
        fail(f"{member['name']} native export {record['name']} truncated before completion status")
    if trailer.get("running") or trailer.get("exit_code") != 0:
        detail = f": {result['stderr']}" if result["stderr"] else ""
        fail(f"{member['name']} native export {record['name']} failed (exit {trailer.get('exit_code')}){detail}")
    if result["bytes"] == 0:
        fail(f"{member['name']} native export {record['name']} returned empty stdout")
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


def apply_volume_excludes(staged_dir, patterns):
    if not patterns:
        return
    staged_dir = Path(staged_dir)
    for raw in patterns:
        cleaned = raw.lstrip("/").rstrip("/")
        if not cleaned or any(ch in cleaned for ch in "*?[]!") or ".." in cleaned.split("/"):
            continue
        candidate = staged_dir
        safe = True
        for part in PurePosixPath(cleaned).parts:
            next_path = candidate / part
            if next_path != staged_dir / cleaned and next_path.is_symlink():
                safe = False
                break
            candidate = next_path
        if not safe:
            continue
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            shutil.rmtree(candidate)
    ignore_file = staged_dir / ".kopiaignore"
    if ignore_file.is_symlink():
        ignore_file.unlink()
    ignore_file.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    ignore_file.chmod(0o600)


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


def resolve_volume(volume_name, volumes=None, volumes_root="/volumes"):
    if not NAME.fullmatch(volume_name):
        fail("Invalid named volume: " + repr(volume_name))
    if isinstance(volumes, dict) and volume_name in volumes:
        explicit = Path(volumes[volume_name])
        if explicit.is_symlink() or not explicit.is_dir():
            fail(f"Configured volume mount for {volume_name} is missing or a symlink")
        return explicit.resolve(strict=True)
    base = Path(volumes_root)
    for candidate in (base / volume_name / "_data", base / volume_name):
        if not candidate.is_symlink() and candidate.is_dir():
            return candidate.resolve(strict=True)
    fail(f"Volume {volume_name} was not found under {volumes_root}")


def parse_explicit_volumes(environment):
    raw = environment.get("BACKUP_VOLUMES", "").strip()
    if not raw:
        return {}
    volumes = json.loads(raw)
    if not isinstance(volumes, dict) or any(
            not NAME.fullmatch(name) or not isinstance(path, str) or not Path(path).is_absolute()
            for name, path in volumes.items()):
        fail("BACKUP_VOLUMES must map named volumes to absolute read-only mount paths")
    return volumes


def collect(api, volumes, root, version, volumes_root="/volumes", run_id=None):
    root = Path(root)
    if (root / "restore.json").exists() or (root / "restore.json").is_symlink():
        fail("Operator restoration is pending; resume or explicitly clean up its recovery state before backing up")
    if not version.strip():
        fail("Sophon version is required")
    groups, undeclared, _ = inventory(api)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = run_id if (run_id and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id)) else uuid.uuid4().hex
    run = f"{stamp}-{suffix}"
    directory = private_dir(private_dir(root / "runs") / run)
    manifest = {
        "version": 3,
        "run": run,
        "sophon_version": version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "groups": [],
        "failures": [],
        "undeclared_volumes": undeclared,
    }
    for group_name, group in sorted(groups.items()):
        group_dir = private_dir(directory / group_name)
        record = {"name": group_name, "members": [], "paths": [], "dumps": []}
        try:
            if group["errors"]:
                fail("; ".join(sorted(group["errors"])))
            for member in sorted(group["members"], key=lambda m: m["name"]):
                record["members"].append({key: member[key] for key in ("name", "image", "image_id")})
                for item in member["spec"]["paths"]:
                    volume = item["volume"]
                    if volume not in member["volumes"]:
                        fail(f"Declared volume {volume} is not attached to {member['name']}")
                    resolve_volume(volume, volumes=volumes, volumes_root=volumes_root)
            for member in sorted(group["members"], key=lambda m: m["name"]):
                member_dir = private_dir(group_dir / member["name"])
                for item in member["spec"]["paths"]:
                    source_root = resolve_volume(item["volume"], volumes=volumes, volumes_root=volumes_root)
                    source = source_root if item["path"] == "." else source_root / item["path"]
                    if not source.resolve().is_relative_to(source_root):
                        fail("Declared path escapes its mounted volume")
                    present = source.exists() or source.is_symlink()
                    artifact = {
                        **item,
                        "member": member["name"],
                        "present": present,
                        "snapshot_path": f"{group_name}/{member['name']}/{item['name']}",
                    }
                    if not present:
                        if not item["optional"]:
                            fail("Required irreproducible path is missing: " + item["path"])
                    else:
                        if "ini" in item and (source.is_symlink() or not source.is_file()):
                            fail("INI identity selection requires a regular file, not a symlink or directory")
                        staged_target = member_dir / item["name"]
                        artifact["copy"] = clone(source, staged_target)
                        if item["path"] == "." and item.get("exclude"):
                            apply_volume_excludes(staged_target, item["exclude"])
                        if "ini" in item:
                            select_ini(staged_target, item["ini"])
                    record["paths"].append(artifact)
                for item in member["spec"]["dumps"]:
                    exported = dump(api, member, item, member_dir / item["name"])
                    exported["snapshot_path"] = f"{group_name}/{member['name']}/{item['name']}"
                    if "restore" in item:
                        exported["restore"] = item["restore"]
                    if "base" in item:
                        exported["base"] = item["base"]
                    record["dumps"].append(exported)
            manifest["groups"].append(record)
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            manifest["failures"].append({"group": group_name, "error": str(error)})
            shutil.rmtree(group_dir, ignore_errors=True)
    manifest["status"] = "failed" if manifest["failures"] else "complete"
    write_json(directory / "manifest.json", manifest)
    for failure in manifest["failures"]:
        print(f"ERROR group={failure['group']}: {failure['error']}", file=sys.stderr)
    print("Undeclared volumes (not copied): " + ", ".join(undeclared), file=sys.stderr)
    return {"path": str(directory), "manifest": manifest}


def prepare(api, volumes, root, version, volumes_root="/volumes"):
    with locked(root) as directory:
        return collect(api, volumes, directory, version, volumes_root=volumes_root)


def before_snapshot_action(environment):
    api = engine_api(environment["ENGINE_URL"], float(environment.get("COMMAND_TIMEOUT", "900")))
    explicit_volumes = parse_explicit_volumes(environment)
    volumes_root = environment.get("VOLUMES_ROOT", "/volumes")
    source_root = Path(environment.get("SOURCE_ROOT", "/source"))
    snapshot_id = environment.get("KOPIA_SNAPSHOT_ID", "")
    with locked(source_root) as root:
        cleanup_stale_runs(root)
        result = collect(
            api, explicit_volumes, root, environment["SOPHON_VERSION"],
            volumes_root=volumes_root, run_id=snapshot_id,
        )
        manifest = result["manifest"]
        staged_path = Path(result["path"])
        if manifest["failures"]:
            shutil.rmtree(staged_path, ignore_errors=True)
            write_json(root / "last-run.json", dict(manifest, upload_status="failed"))
            summary = "; ".join(f"{item['group']}: {item['error']}" for item in manifest["failures"])
            fail("Backup before-snapshot action failed: " + summary)
        write_json(root / "last-run.json", dict(manifest, staged_path=str(staged_path), upload_status="pending"))
        print("KOPIA_SNAPSHOT_PATH=" + str(staged_path), flush=True)


def after_snapshot_action(environment):
    source_root = Path(environment.get("SOURCE_ROOT", "/source"))
    staged_raw = environment.get("KOPIA_SNAPSHOT_PATH", "").strip()
    with locked(source_root) as root:
        runs_root = private_dir(root / "runs").resolve()
        if staged_raw:
            staged = Path(staged_raw)
            if not staged.is_symlink() and staged.exists() and staged.resolve().is_relative_to(runs_root):
                shutil.rmtree(staged, ignore_errors=True)
        last_run_path = root / "last-run.json"
        if last_run_path.exists() and not last_run_path.is_symlink():
            record = json.loads(last_run_path.read_text())
            record["upload_status"] = "complete"
            record["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record.pop("staged_path", None)
            write_json(last_run_path, record)


def run(environment):
    source_root = Path(environment.get("SOURCE_ROOT", "/source"))
    target = environment.get("KOPIA_SNAPSHOT_SOURCE", "/sophon-state")
    upload = subprocess.run(
        ["kopia", "snapshot", "create", target, "--json", "--fail-fast"],
        capture_output=True, text=True, env=environment, timeout=86400,
    )
    if upload.returncode:
        detail = upload.stderr
        for name in ("KOPIA_PASSWORD", "KOPIA_SERVER_PASSWORD", "KOPIA_SFTP_PRIVATE_KEY_B64"):
            if environment.get(name):
                detail = detail.replace(environment[name], "[redacted]")
        last_run_path = source_root / "last-run.json"
        if not last_run_path.exists() or json.loads(last_run_path.read_text()).get("upload_status") == "pending":
            with locked(source_root) as root:
                cleanup_stale_runs(root)
                existing = json.loads(last_run_path.read_text()) if last_run_path.exists() else {"failures": []}
                existing.update(status="failed", upload_status="failed", upload_error=detail.strip())
                write_json(root / "last-run.json", existing)
        fail("Kopia snapshot failed: " + detail.strip())
    snapshot = json.loads(upload.stdout)
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", snapshot["id"])
            or snapshot.get("rootEntry", {}).get("summ", {}).get("numFailed") != 0):
        fail("Kopia returned an invalid or incomplete snapshot result")
    last_run_path = source_root / "last-run.json"
    with locked(source_root) as root:
        record = json.loads(last_run_path.read_text()) if last_run_path.exists() else {"status": "complete", "failures": []}
        record["snapshot"] = snapshot
        record["upload_status"] = "complete"
        write_json(root / "last-run.json", record)
    import runtime
    runtime.sync_ui(environment)
    print(json.dumps({"status": record["status"], "failures": record.get("failures", []), "snapshot": snapshot}))
    return 0


def main():
    os.umask(0o077)
    kopia_action = os.environ.get("KOPIA_ACTION", "").strip()
    try:
        if kopia_action == "before-snapshot-root":
            before_snapshot_action(dict(os.environ))
            return 0
        if kopia_action == "after-snapshot-root":
            after_snapshot_action(dict(os.environ))
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("action", choices=("run", "before", "after"))
        args = parser.parse_args()
        if args.action == "before":
            before_snapshot_action(dict(os.environ))
            return 0
        if args.action == "after":
            after_snapshot_action(dict(os.environ))
            return 0
        return run(dict(os.environ))
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print("Backup failed: " + str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
