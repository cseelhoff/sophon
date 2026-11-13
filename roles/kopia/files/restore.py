#!/usr/bin/env python3
"""Explicit v3 group recovery. Configure never invokes this command."""
import argparse
import base64
import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

import backup
import runtime

OWNER = "sophon.restore.owner"


def image_id(value):
    value = value.removeprefix("sha256:")
    if not backup.IDENTITY.fullmatch(value):
        backup.fail("Recovery requires immutable image IDs")
    return value


def artifact(root, record, group):
    expected = f"{group}/{record['member']}/{record['name']}"
    if (not backup.NAME.fullmatch(record["member"]) or not backup.NAME.fullmatch(record["name"])
            or record.get("snapshot_path") != expected):
        backup.fail("Unsafe snapshot artifact location")
    path = root
    for part in PurePosixPath(expected).parts:
        path /= part
        if path != root / expected and path.is_symlink():
            backup.fail("Snapshot artifact parent is a symlink")
    return path


def checksum(path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(backup.CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def tree_hash(root):
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"))]:
        attributes = path.lstat()
        digest.update(os.fsencode(str(path.relative_to(root))) + b"\0")
        digest.update(f"{attributes.st_mode}:{attributes.st_uid}:{attributes.st_gid}:{attributes.st_mtime_ns}".encode())
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(attributes.st_mode):
            digest.update(bytes.fromhex(checksum(path)[0]))
        elif not stat.S_ISDIR(attributes.st_mode):
            backup.fail("Unsupported special file in restored snapshot")
    return digest.hexdigest()


def make_plan(snapshot, targets, group):
    snapshot = Path(snapshot)
    if snapshot.is_symlink() or not snapshot.is_dir():
        backup.fail("Snapshot staging must be a real private directory")
    if not backup.NAME.fullmatch(group):
        backup.fail("Invalid restore group")
    manifest_path = snapshot / "manifest.json"
    if manifest_path.is_symlink():
        backup.fail("Snapshot manifest is a symlink")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != 3:
        backup.fail("Only v3 snapshots can be restored by this tool")
    if any(item["group"] == group for item in manifest["failures"]):
        backup.fail("The selected snapshot failed this group; choose an explicit successful snapshot")
    matching = [item for item in manifest["groups"] if item["name"] == group]
    if len(matching) != 1:
        backup.fail("Snapshot does not contain exactly one selected group")
    source = matching[0]
    if not source["paths"] and not source["dumps"]:
        backup.fail("This group is reconstructible; rerun Configure instead of restoring it")
    current = {}
    for target in targets:
        if (target["Config"].get("Labels") or {}).get(backup.PREFIX + "group") != group:
            continue
        spec = backup.parse_spec(target)
        if spec is not None and spec["group"] == group:
            name = target["Name"].lstrip("/")
            if not backup.NAME.fullmatch(name) or name in current:
                backup.fail("Ambiguous target member name")
            current[name] = (target, spec)
    if set(current) != {member["name"] for member in source["members"]}:
        backup.fail("Freshly configured group membership does not match the snapshot")
    plan = {"version": 3, "group": group, "manifest_sha256": checksum(manifest_path)[0],
            "sophon_version": manifest["sophon_version"], "snapshot_sha256": tree_hash(snapshot),
            "members": [], "paths": [], "dumps": []}
    for member in source["members"]:
        target, spec = current[member["name"]]
        if image_id(member["image_id"]) != image_id(target["Image"]):
            backup.fail("Target image differs from the snapshot: " + member["name"])
        mounts = [{key: mount.get(key) for key in ("Type", "Name", "Destination", "RW")}
                  for mount in target.get("Mounts", [])]
        plan["members"].append({"id": target["Id"], "name": member["name"],
                                "image_id": target["Image"], "signature": backup.signature(target),
                                "running": target["State"]["Running"], "mounts": mounts})
        paths = {item["name"]: item for item in spec["paths"]}
        dumps = {item["name"]: item for item in spec["dumps"]}
        source_paths = [item for item in source["paths"] if item["member"] == member["name"]]
        source_dumps = [item for item in source["dumps"] if item["member"] == member["name"]]
        if set(paths) != {item["name"] for item in source_paths} or set(dumps) != {item["name"] for item in source_dumps}:
            backup.fail("Current path/export declarations differ from the snapshot")
        for item in source_paths:
            path = artifact(snapshot, item, group)
            declared = paths[item["name"]]
            if item["path"] != declared["path"] or item.get("ini") != declared.get("ini"):
                backup.fail("Current path/identity selection differs from the snapshot")
            if item["present"]:
                if not path.exists() and not path.is_symlink():
                    backup.fail("Snapshot is missing a declared path")
                if declared["path"] == "." and (path.is_symlink() or not path.is_dir()):
                    backup.fail("Whole-volume recovery requires a real snapshot directory")
                if "ini" in declared and (path.is_symlink() or not path.is_file()):
                    backup.fail("INI identity recovery requires a regular snapshot file")
            elif not declared["optional"]:
                backup.fail("Snapshot lacks a currently required path")
            if declared["volume"] not in {m["Name"] for m in mounts if m["Type"] == "volume"}:
                backup.fail("Target does not have the declared named volume")
            plan["paths"].append(dict(item, volume=declared["volume"]))
        for item in source_dumps:
            path = artifact(snapshot, item, group)
            if path.is_symlink() or not path.is_file() or checksum(path) != (item["sha256"], item["bytes"]):
                backup.fail("Native dump checksum/size mismatch")
            declared = dumps[item["name"]]
            kind = declared.get("restore")
            if kind not in ("postgres-cluster", "ldap-subtree"):
                backup.fail("Native export needs an explicit supported restore declaration in current IaC")
            if kind == "ldap-subtree":
                validate_ldif(path.read_bytes(), declared["base"], subtree_only=True)
            member_plan = next(entry for entry in plan["members"] if entry["name"] == member["name"])
            native_mounts(member_plan, "validation-state", "/var/lib/postgresql" if kind == "postgres-cluster" else "/var/lib/ldap")
            if kind == "postgres-cluster":
                values = dict(entry.split("=", 1) for entry in target["Config"]["Env"])
                if (not values.get("POSTGRES_USER") or not values.get("PGDATA", "").startswith("/var/lib/postgresql/")
                        or ".." in values["PGDATA"].split("/")):
                    backup.fail("Unsupported fresh PostgreSQL configuration")
            plan["dumps"].append(dict(item, restore=kind, base=declared.get("base")))
    return plan


def bounded_target(root, relative):
    if (not relative or relative.startswith("/") or ".." in relative.split("/")
            or str(PurePosixPath(relative)) != relative):
        backup.fail("Unsafe target relative path")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        backup.fail("Target mount must be a real named-volume directory")
    path = root
    for part in PurePosixPath(relative).parts[:-1]:
        path /= part
        if path.is_symlink() or not path.is_dir():
            backup.fail("Target parent must already exist in fresh configuration and cannot be a symlink")
    return root / relative


def remove_path(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def merge_ini(source, target, selectors):
    if target.is_symlink() or not target.is_file() or source.is_symlink():
        backup.fail("INI identity merge requires real freshly configured files")
    original = configparser.ConfigParser(interpolation=None, allow_unnamed_section=True, strict=False)
    identity = configparser.ConfigParser(interpolation=None, strict=False)
    original.optionxform = identity.optionxform = str
    try:
        original.read_string(target.read_text())
        identity.read_string(source.read_text())
    except (configparser.Error, UnicodeError):
        backup.fail("Cannot parse identity/configuration for a safe INI merge")
    for selector in selectors:
        section, key = selector.rsplit(".", 1)
        if not identity.has_section(section):
            continue
        matches = [name for name in identity[section] if name.casefold() == key.casefold()]
        if not matches:
            continue
        if not original.has_section(section):
            original.add_section(section)
        old = [name for name in original[section] if name.casefold() == key.casefold()]
        original.set(section, old[0] if old else key, identity[section][matches[0]])
    attributes = source.stat()
    temporary = target.with_name(".sophon-restore-" + uuid.uuid4().hex)
    try:
        with temporary.open("x") as output:
            original.write(output)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, attributes.st_uid, attributes.st_gid)
        os.chmod(temporary, attributes.st_mode & 0o7777)
        os.utime(temporary, ns=(attributes.st_atime_ns, attributes.st_mtime_ns))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def replace_paths(plan, snapshot, mounts):
    snapshot = Path(snapshot)
    for item in plan["paths"]:
        target = bounded_target(mounts[item["volume"]], item["path"])
        source = artifact(snapshot, item, plan["group"])
        if not item["present"]:
            remove_path(target)
        elif "ini" in item:
            merge_ini(source, target, item["ini"])
        elif item["path"] == ".":
            for entry in target.iterdir():
                remove_path(entry)
            result = subprocess.run(["cp", "--archive", "--reflink=auto", "--", str(source) + "/.", str(target) + "/"],
                                    capture_output=True, text=True, timeout=3600)
            if result.returncode:
                backup.fail("Whole-volume state replacement failed: " + result.stderr)
        else:
            remove_path(target)
            backup.clone(source, target)


def ldif_records(data):
    records = []
    for paragraph in re.split(rb"\r?\n\r?\n", data.strip()):
        lines = []
        for line in paragraph.splitlines():
            if line.startswith(b" "):
                if not lines:
                    backup.fail("Invalid folded LDIF")
                lines[-1] += line[1:]
            elif line and not line.startswith(b"#"):
                lines.append(line)
        if not lines:
            continue
        dn = next((line for line in lines if line.lower().startswith(b"dn:")), None)
        if dn is None:
            backup.fail("LDIF entry has no DN")
        value = (base64.b64decode(dn[4:], validate=True) if dn.startswith(b"dn:: ")
                 else dn[4:] if dn.startswith(b"dn: ") else None)
        if value is None or any(re.match(rb"[^:]+:<", line) for line in lines):
            backup.fail("Unsupported LDIF DN or external value URL")
        records.append((value.decode("utf-8"), b"\n".join(lines) + b"\n\n"))
    return records


def validate_ldif(data, base, subtree_only=False):
    if not re.fullmatch(r"dc=[A-Za-z0-9-]+(?:,dc=[A-Za-z0-9-]+)*", base, re.IGNORECASE):
        backup.fail("LDAP restore requires the explicit configured dc-style base DN")
    records = ldif_records(data)
    if not records:
        backup.fail("LDAP export contains no entries")
    if subtree_only and any(not (dn.casefold() == base.casefold() or
                                dn.casefold().endswith("," + base.casefold())) for dn, _ in records):
        backup.fail("LDAP snapshot contains entries outside the configured subtree")
    return records


def targets(api):
    return [backup.engine(api, "GET", f"/containers/{item['Id']}/json")
            for item in backup.engine(api, "GET", "/containers/json?all=true")]


def current(api, member):
    return backup.inspect(api, member)


def save(root, state):
    backup.write_json(root / "state.json", state)


def state_file(root):
    path = root / "state.json"
    if path.is_symlink():
        backup.fail("Recovery state is a symlink")
    state = json.loads(path.read_text())
    if state.get("version") != 3 or not re.fullmatch(r"[a-f0-9]{32}", state.get("owner", "")):
        backup.fail("Invalid v3 recovery state")
    return state


def helper(api, root, state, image, binds, command, entrypoint, env=(), database=False):
    name = "sophon-restore-" + state["owner"][:12] + "-" + str(len(state["resources"]))
    record = {"name": name, "id": None, "image": image, "binds": binds}
    state["resources"].append(record)
    save(root, state)
    caps = ["DAC_OVERRIDE", "CHOWN", "FOWNER"] + (["SETUID", "SETGID"] if database else [])
    body = {"Image": image, "User": "0:0", "Entrypoint": entrypoint, "Cmd": command, "Env": list(env),
            "Labels": {OWNER: state["owner"]}, "HostConfig": {
                "Binds": binds, "NetworkMode": "none", "ReadonlyRootfs": True,
                "CapDrop": ["ALL"], "CapAdd": caps, "SecurityOpt": ["label=disable", "no-new-privileges"],
                "Tmpfs": {"/tmp": "rw,mode=1777", "/var/run/postgresql": "rw,mode=3775",
                          "/var/run/slapd": "rw,mode=0755"}}}
    record["id"] = backup.engine(api, "POST", "/containers/create?name=" + name, body)["Id"]
    save(root, state)
    backup.engine(api, "POST", f"/containers/{record['id']}/start")
    return record


def owned_helper(api, state, record):
    try:
        value = backup.engine(api, "GET", f"/containers/{record['id'] or record['name']}/json")
    except backup.EngineError as error:
        if error.status == 404:
            return None
        raise
    actual = sorted(m["Name"] + ":" + m["Destination"] + ("" if m["RW"] else ":ro")
                    for m in value.get("Mounts", []) if m["Type"] == "volume")
    if (value["Config"].get("Labels", {}).get(OWNER) != state["owner"]
            or image_id(value["Image"]) != image_id(record["image"])
            or sorted(record["binds"]) != actual or value["HostConfig"].get("Privileged")
            or any(m["Type"] not in ("volume", "tmpfs") for m in value.get("Mounts", []))):
        backup.fail("Restore helper ownership/mount/image changed; cleanup refused")
    return value


def remove_helpers(api, root, state):
    for record in list(state["resources"]):
        value = owned_helper(api, state, record)
        if value is not None:
            backup.engine(api, "DELETE", f"/containers/{value['Id']}?force=true")
        state["resources"].remove(record)
        save(root, state)


def run_helper(api, record):
    deadline = time.monotonic() + 86400
    while time.monotonic() < deadline:
        value = backup.engine(api, "GET", f"/containers/{record['id']}/json")
        if not value["State"]["Running"]:
            if value["State"]["ExitCode"]:
                backup.fail("Restore helper failed; inspect its logs before retrying: " + record["name"])
            return
        time.sleep(.2)
    backup.fail("Restore helper timed out")


def native_mounts(member, state_volume, writable):
    result = [state_volume + ":/restore:ro"]
    for mount in member["mounts"]:
        if mount["Type"] != "volume" or not backup.NAME.fullmatch(mount["Name"]):
            backup.fail("Native restore requires named volumes, not host binds")
        destination = mount["Destination"]
        if destination == "/" or ":" in destination or ".." in destination.split("/"):
            backup.fail("Unsafe native restore mount")
        result.append(mount["Name"] + ":" + destination + ("" if destination == writable else ":ro"))
    if not any(m["Destination"] == writable for m in member["mounts"]):
        backup.fail("Fresh native database volume is missing")
    return result


POSTGRES_IMPORT = r'''
set -eu
temp=$1 database=$2 system=$3 dump=$4
i=0; until pg_isready -q; do i=$((i+1)); test "$i" -lt 90; sleep 1; done
if ! psql -X -U "$temp" -d "$database" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    psql -X -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -v temp="$temp" -v database="$database" <<'SQL'
SELECT format('CREATE ROLE %I SUPERUSER LOGIN', :'temp') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname=:'temp') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'database', :'temp') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname=:'database') \gexec
SQL
fi
psql -X -U "$temp" -d "$database" -v ON_ERROR_STOP=1 -v system="$system" <<'SQL'
SELECT format('ALTER ROLE %I RENAME TO %I', rolname, :'system') FROM pg_roles WHERE oid=10 AND rolname<>:'system' \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', datname, :'system') FROM pg_database WHERE datname<>current_database() \gexec
SQL
for target in postgres template1; do
    psql -X -U "$temp" -d "$target" -v ON_ERROR_STOP=1 -v temp="$temp" -v system="$system" <<'SQL'
-- pg_dumpall leaves these databases in place; settle dependencies from prior imports.
SELECT format('REASSIGN OWNED BY %I TO %I', rolname, :'system') FROM pg_roles WHERE rolname<>:'system' AND rolname<>:'temp' AND rolname !~ '^pg_' \gexec
SELECT format('DROP OWNED BY %I', rolname) FROM pg_roles WHERE rolname<>:'system' AND rolname<>:'temp' AND rolname !~ '^pg_' \gexec
SQL
done
psql -X -q -U "$temp" -d "$database" -v ON_ERROR_STOP=1 -f "$dump" > /tmp/sophon-import.log 2>&1
psql -X -q -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -v temp="$temp" -v database="$database" -v system="$system" <<'SQL'
ALTER ROLE :"system" NOLOGIN NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL;
DROP DATABASE :"database";
DROP ROLE :"temp";
SQL
'''


def restore_dump(api, root, state, item, image, volume):
    plan = state["plan"]
    member = next(m for m in plan["members"] if m["name"] == item["member"])
    live = current(api, member)
    source = "/restore/snapshot/" + item["snapshot_path"]
    if item["restore"] == "postgres-cluster":
        values = dict(entry.split("=", 1) for entry in live["Config"]["Env"])
        pgdata = values.get("PGDATA", "")
        if not pgdata.startswith("/var/lib/postgresql/") or ".." in pgdata.split("/"):
            backup.fail("Unsupported PostgreSQL data layout")
        record = helper(api, root, state, member["image_id"], native_mounts(
            member, volume, "/var/lib/postgresql"), ["postgres", "-c", "listen_addresses="], ["docker-entrypoint.sh"],
            ["POSTGRES_USER=" + values["POSTGRES_USER"], "PGDATA=" + pgdata], database=True)
        backup.control(api, record["id"], [
            "sh", "-ceu", POSTGRES_IMPORT, "restore", "sophon_temp_" + state["owner"][:12],
            "sophon_temp_" + state["owner"][:12], "sophon_system_" + state["owner"][:12], source])
    else:
        record = helper(api, root, state, member["image_id"], native_mounts(
            member, volume, "/var/lib/ldap"), ["sleep 86400"], ["/bin/sh", "-c"])
        data_mount = next(m for m in member["mounts"] if m["Destination"] == "/var/lib/ldap")
        owner = backup.control(api, record["id"], ["stat", "-c", "%u:%g", "/var/lib/ldap"]).decode().strip()
        if not re.fullmatch(r"[0-9]+:[0-9]+", owner):
            backup.fail("Invalid LDAP data ownership")
        imports = backup.private_dir(root / "imports")
        key = hashlib.sha256((item["member"] + "/" + item["name"]).encode()).hexdigest()
        destination = imports / (key + ".ldif")
        saved = state.setdefault("imports", {}).get(key)
        if saved is None:
            config = backup.control(api, record["id"], ["slapcat", "-n", "0", "-a", "(olcDatabase={1}mdb)"])
            suffixes = [line.removeprefix(b"olcSuffix: ").decode().casefold()
                        for line in config.splitlines() if line.startswith(b"olcSuffix: ")]
            if suffixes != [item["base"].casefold()]:
                backup.fail("LDAP restore requires the declared base to equal the configured MDB suffix")
            destination.write_bytes(artifact(root / "snapshot", item, plan["group"]).read_bytes())
            state["imports"][key] = list(checksum(destination))
            save(root, state)
        elif destination.is_symlink() or list(checksum(destination)) != saved:
            backup.fail("Prepared LDAP import changed; targets were not cleared")
        state["clear_volume"] = data_mount["Name"]
        save(root, state)
        cleaner = helper(api, root, state, image, [volume + ":/restore:ro",
                         data_mount["Name"] + ":/targets/" + data_mount["Name"]],
                         ["files", "--clear-volume", data_mount["Name"]], ["python3", "/app/restore.py"])
        run_helper(api, cleaner)
        backup.control(api, record["id"], ["slapadd", "-n", "1", "-w", "-F", "/etc/ldap/slapd.d",
                                          "-l", "/restore/imports/" + destination.name])
        backup.control(api, record["id"], ["chown", "-R", owner, "/var/lib/ldap"])
        backup.control(api, record["id"], ["slaptest", "-u", "-F", "/etc/ldap/slapd.d"])
    remove_helpers(api, root, state)


def apply(api, root, approve=False, image=None, state_volume=None):
    if not approve:
        backup.fail("Restore requires explicit approval (--approve); native dumps are trusted executable input")
    if not image or not state_volume or not backup.NAME.fullmatch(state_volume):
        backup.fail("Restore needs an immutable helper image and an owned state volume")
    image_id(image)
    state = state_file(root)
    if state["phase"] == "complete":
        backup.fail("This restore already completed; inspect it or clean up its state")
    plan = state["plan"]
    if tree_hash(root / "snapshot") != plan["snapshot_sha256"]:
        backup.fail("Staged snapshot changed after planning; no restore targets were modified")
    for member in plan["members"]:
        current(api, member)
    remove_helpers(api, root, state)
    state["approved"] = True
    state["phase"] = "stopping"
    save(root, state)
    producers = {item["member"] for item in plan["dumps"]}
    stop_order = sorted(plan["members"], key=lambda member: (member["name"] in producers, member["name"]))
    try:
        for member in stop_order:
            if current(api, member)["State"]["Running"]:
                backup.engine(api, "POST", f"/containers/{member['id']}/stop?t=120")
            if current(api, member)["State"]["Running"]:
                backup.fail("Group member did not stop")
        mutated = {item["volume"] for item in plan["paths"]}
        for item in plan["dumps"]:
            member = next(m for m in plan["members"] if m["name"] == item["member"])
            destination = "/var/lib/postgresql" if item["restore"] == "postgres-cluster" else "/var/lib/ldap"
            mutated.add(next(m["Name"] for m in member["mounts"] if m["Destination"] == destination))
        for value in targets(api):
            if value["State"]["Running"] and any(m.get("Type") == "volume" and m.get("Name") in mutated
                                                and m.get("RW", True) for m in value.get("Mounts", [])):
                backup.fail("An unrelated writer is using a restore target volume")
        state["phase"] = "files"
        save(root, state)
        binds = [state_volume + ":/restore:ro"] + [
            volume + ":/targets/" + volume for volume in sorted({item["volume"] for item in plan["paths"]})]
        if plan["paths"]:
            record = helper(api, root, state, image, binds, ["files"], ["python3", "/app/restore.py"])
            run_helper(api, record)
            remove_helpers(api, root, state)
        state["phase"] = "importing"
        save(root, state)
        for item in plan["dumps"]:
            restore_dump(api, root, state, item, image, state_volume)
        state["phase"] = "starting"
        save(root, state)
        for member in sorted(plan["members"], key=lambda m: (m["name"] not in producers, m["name"])):
            if member["running"]:
                backup.engine(api, "POST", f"/containers/{member['id']}/start")
                if not current(api, member)["State"]["Running"]:
                    backup.fail("Restored group member did not start")
        state["phase"] = "complete"
        save(root, state)
    except BaseException:
        state["phase"] = "failed"
        try:
            save(root, state)
        except OSError as error:
            print("ERROR recovery journal could not be persisted: " + str(error), file=sys.stderr)
        for member in stop_order:
            try:
                current(api, member)
                backup.engine(api, "POST", f"/containers/{member['id']}/stop?t=120")
            except (RuntimeError, OSError) as error:
                print("ERROR group stop recovery remains pending: " + str(error), file=sys.stderr)
        for record in state["resources"]:
            try:
                value = owned_helper(api, state, record)
                if value is not None and value["State"]["Running"]:
                    backup.engine(api, "POST", f"/containers/{value['Id']}/stop?t=120")
            except (RuntimeError, OSError) as error:
                print("ERROR helper stop recovery remains pending: " + str(error), file=sys.stderr)
        raise
    return state


def connection_environment(stdin=False):
    environment = dict(os.environ)
    if stdin:
        for entry in json.load(sys.stdin):
            key, value = entry.split("=", 1)
            if key.startswith("KOPIA_"):
                environment[key] = value
    for key, value in os.environ.items():
        if key.startswith("SOPHON_KOPIA_"):
            environment[key.removeprefix("SOPHON_")] = value
    environment.update(KOPIA_CONFIG_PATH="/tmp/restore.config", KOPIA_CACHE_DIRECTORY="/tmp/cache",
                       KOPIA_LOG_DIR="/tmp/logs")
    return environment


def plan(api, root, environment, snapshot_id, group):
    runtime.connect(environment)
    if snapshot_id == "latest":
        source = environment["KOPIA_CLIENT_USERNAME"] + "@" + environment["KOPIA_CLIENT_HOSTNAME"] + ":/sophon-state"
        snapshots = json.loads(runtime.kopia(environment, "snapshot", "list", source, "--json"))
        if not snapshots:
            backup.fail("No snapshots exist for the configured source")
        snapshot_id = max(snapshots, key=lambda value: value["endTime"])["id"]
    if not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
        backup.fail("Snapshot must be latest or an exact snapshot ID")
    existing = state_file(root) if (root / "state.json").exists() else None
    if existing:
        if "plan" not in existing:
            backup.fail("Planning was interrupted; run cleanup --approve before planning again")
        if existing["snapshot"] != snapshot_id or existing["plan"]["group"] != group:
            backup.fail("State is bound to another snapshot/group; choose another state volume")
        for member in existing["plan"]["members"]:
            current(api, member)
        return existing
    directory = root / "snapshot"
    if directory.exists():
        backup.fail("Unowned snapshot staging already exists")
    state = {"version": 3, "owner": uuid.uuid4().hex, "snapshot": snapshot_id,
             "phase": "downloading", "approved": False, "resources": []}
    save(root, state)
    runtime.kopia(environment, "snapshot", "restore", snapshot_id, str(directory),
                  "--no-skip-owners", "--no-skip-permissions", "--no-ignore-permission-errors", timeout=86400)
    prepared = make_plan(directory, targets(api), group)
    state.update(phase="planned", plan=prepared)
    save(root, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "status", "cleanup", "files"))
    parser.add_argument("--group")
    parser.add_argument("--snapshot", default="latest")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--connection-stdin", action="store_true")
    parser.add_argument("--state-volume")
    parser.add_argument("--image")
    parser.add_argument("--clear-volume")
    args = parser.parse_args()
    if args.action in ("plan", "apply") and not args.group:
        parser.error("--group is required")
    os.umask(0o077)
    def interrupted(signum, frame):
        raise RuntimeError("Restore interrupted; retry the same state to settle helpers and stopped workloads")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    root = Path("/restore")
    try:
        if args.action == "files":
            state = state_file(root)
            if not state["approved"] or state["phase"] not in ("files", "importing"):
                backup.fail("File helper has no approved active restore")
            if args.clear_volume:
                if args.clear_volume != state.get("clear_volume"):
                    backup.fail("Unapproved native data volume")
                target = Path("/targets") / args.clear_volume
                if target.is_symlink() or not target.is_dir():
                    backup.fail("Invalid native data mount")
                for entry in target.iterdir():
                    remove_path(entry)
            else:
                replace_paths(state["plan"], root / "snapshot",
                              {item["volume"]: "/targets/" + item["volume"] for item in state["plan"]["paths"]})
            return 0
        if args.action == "status":
            print(json.dumps(state_file(root)))
            return 0
        api = {"socket": "/socket/podman.sock", "timeout": 3600}
        environment = connection_environment(args.connection_stdin)
        with backup.locked(root):
            if args.action == "cleanup":
                if not args.approve:
                    backup.fail("Cleanup requires explicit approval (--approve)")
                state = state_file(root)
                remove_helpers(api, root, state)
                for name in ("snapshot", "imports", "state.json"):
                    remove_path(root / name)
                print("Recovery helpers and staged secrets removed; workload running state was not changed")
                return 0
            state = plan(api, root, environment, args.snapshot, args.group)
            if args.action == "apply":
                if not args.approve:
                    backup.fail("Restore requires explicit approval (--approve)")
                marker = Path("/source/.collector.lock")
                if marker.is_symlink() or not marker.is_file():
                    backup.fail("The source volume is not an initialized backup coordination volume")
                with backup.locked("/source") as source:
                    if (source / "journal.json").exists():
                        backup.fail("Finish pending backup stop recovery before restoring")
                    state = apply(api, root, args.approve, args.image, args.state_volume)
            print(json.dumps({"snapshot": state["snapshot"], "phase": state["phase"], "plan": state["plan"]}))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print("Restore failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
