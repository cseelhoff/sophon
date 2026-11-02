#!/usr/bin/env python3
"""Explicit, isolated recovery of version-1 PostgreSQL/Keycloak profiles."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
from urllib.parse import quote
import uuid

import backup

PREFIX = "sophon.restore."
OWNER = PREFIX + "owner"
PHASES = [
    "provision-isolated", "bootstrap-postgres", "import-postgres",
    "restart-original-postgres", "start-keycloak", "verify-keycloak",
]


def environment(container):
    values = container["Config"].get("Env", [])
    if (not isinstance(values, list) or any(
            not isinstance(value, str) or "=" not in value or "\x00" in value
            for value in values)):
        backup.fail("invalid source environment")
    result = dict(value.split("=", 1) for value in values)
    if len(result) != len(values):
        backup.fail("duplicate source environment variables")
    return result


def selection(snapshot, group):
    if not isinstance(group, str) or not backup.GROUP.fullmatch(group):
        backup.fail("invalid recovery group")
    manifest = backup.verify(snapshot)["manifest"]
    selected = []
    for container in manifest["containers"]:
        spec = backup.parse_spec(container)
        if spec is not None and spec["group"] == group:
            selected.append(container)
    if len(selected) != 2:
        backup.fail("this recovery version requires exactly one PostgreSQL and one Keycloak member")
    profiles = {}
    for container in selected:
        labels = container["Config"]["Labels"]
        profile = {key[len(PREFIX):]: value for key, value in labels.items()
                   if key.startswith(PREFIX)}
        kind = profile.get("kind")
        expected = {
            "postgres-cluster": {"version", "kind", "artifact"},
            "keycloak": {"version", "kind", "depends-on", "verify-realm"},
        }
        if (profile.get("version") != "1" or kind not in expected
                or set(profile) != expected[kind] or kind in profiles):
            backup.fail("missing, unsupported or incomplete recovery profile")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", container.get("Image", "")):
            backup.fail("recovery requires an immutable source image ID")
        name = container.get("Name", "").lstrip("/")
        if not backup.NAME.fullmatch(name):
            backup.fail("unsafe source container name")
        for mount in container.get("Mounts", []):
            if mount.get("Type") != "volume" or ":" in mount.get("Destination", ""):
                backup.fail("this recovery version refuses source bind/tmpfs mounts")
        env = environment(container)
        if any(key.endswith("_FILE") for key in env):
            backup.fail("file-backed secrets require an explicit recovery profile")
        profiles[kind] = {"container": container, "profile": profile, "env": env, "name": name}
    if set(profiles) != {"postgres-cluster", "keycloak"}:
        backup.fail("recovery dependency kinds are incomplete")
    database, app = profiles["postgres-cluster"], profiles["keycloak"]
    db_env, app_env = database["env"], app["env"]
    for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD"):
        if not db_env.get(key):
            backup.fail("PostgreSQL bootstrap conflicts with the original environment")
    if "sophon_restore" in (db_env["POSTGRES_USER"], db_env["POSTGRES_DB"]):
        backup.fail("PostgreSQL bootstrap conflicts with the original user/database")
    if (app["profile"]["depends-on"] != database["name"]
            or app_env.get("KC_DB") != "postgres"
            or app_env.get("KC_DB_URL_HOST") != database["name"]
            or app_env.get("KC_DB_URL_DATABASE") != db_env["POSTGRES_DB"]
            or app_env.get("KC_DB_USERNAME") != db_env["POSTGRES_USER"]
            or any(key in app_env for key in ("KC_DB_URL", "KC_DB_URL_PROPERTIES"))
            or app_env.get("KC_DB_URL_PORT", "5432") != "5432"
            or not app_env.get("KC_BOOTSTRAP_ADMIN_USERNAME")
            or not app_env.get("KC_BOOTSTRAP_ADMIN_PASSWORD")
            or not backup.GROUP.fullmatch(app["profile"]["verify-realm"])):
        backup.fail("Keycloak recovery dependency or authentication settings do not match")
    owned = [item for item in manifest["artifacts"]
             if item["member"] == database["container"]["Id"]]
    if (len(owned) != 1 or owned[0]["name"] != database["profile"]["artifact"]
            or owned[0]["type"] != "archive"):
        backup.fail("PostgreSQL recovery requires exactly one declared native archive")
    for item in (database, app):
        item["recipe"] = next(record for record in manifest["restore"]
                              if record["member"] == item["container"]["Id"])
    if (len(database["recipe"]["commands"]) != 1 or app["recipe"]["commands"]
            or len(database["recipe"]["covers"]) != 1 or app["recipe"]["covers"]
            or app["recipe"]["artifacts"]):
        backup.fail("unsupported recovery command or volume layout")
    database["artifact"] = owned[0]
    if (owned[0]["container_path"] != database["recipe"]["backup_dir"] + ".sql"
            or not owned[0]["container_path"].startswith("/tmp/sophon-backup-")
            or owned[0]["bytes"] > backup.JSON_LIMIT):
        backup.fail("unsupported SQL input location or upload size (maximum 64 MiB)")
    with backup.tarfile.open(backup.snapshot_file(Path(snapshot), owned[0]["path"])) as archive:
        members = archive.getmembers()
        if (len(members) != 1 or not members[0].isfile()
                or members[0].name != Path(owned[0]["container_path"]).name):
            backup.fail("native PostgreSQL archive must contain exactly its recorded SQL file")
    return manifest, database, app


def describe(snapshot, group, database, app):
    return {
        "version": 1, "group": group,
        "manifest_sha256": backup.hash_file(Path(snapshot) / "manifest.json")[0],
        "order": [database["name"], app["name"]], "phases": PHASES,
        "contexts": {"postgres-cluster": "fresh-database", "keycloak": "fresh-application"},
        "images": [item["container"]["Image"] for item in (database, app)],
        "artifact_sha256": database["artifact"]["sha256"],
        "verify_realm": app["profile"]["verify-realm"],
        "production_cutover": False,
    }


def plan(snapshot, group):
    _, database, app = selection(snapshot, group)
    return describe(snapshot, group, database, app)


def target(api):
    return {key: api.get(key) for key in ("host", "port", "base", "endpoint")}


@contextlib.contextmanager
def locked(directory):
    directory = backup.private_dir(directory)
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            backup.fail("another recovery operation holds the state lock")
        yield directory
    finally:
        os.close(fd)


def load_state(api, directory):
    state = backup.read_json(directory / "state.json")
    if (state.get("version") != 1 or state.get("target") != target(api)
            or not re.fullmatch(r"[0-9a-f]{32}", state.get("owner", ""))
            or state.get("prefix") != "sophon-restore-" + state["owner"][:12]
            or not isinstance(state.get("resources"), list)):
        backup.fail("recovery state does not match the target or ownership namespace")
    for record in state["resources"]:
        if (record.get("kind") not in ("containers", "volumes", "networks")
                or not record.get("name", "").startswith(state["prefix"] + "-")
                or not backup.NAME.fullmatch(record["name"])):
            backup.fail("unsafe resource in recovery state")
    return state


def save(directory, state):
    backup.write_json(directory / "state.json", state)


def resource_path(kind, name):
    return "/" + kind + "/" + quote(name, safe="")


def lookup(api, kind, name):
    path = resource_path(kind, name) + ("/json" if kind == "containers" else "")
    try:
        return backup.docker(api, "GET", path)
    except RuntimeError as error:
        if str(error) != "API request failed (HTTP 404)":
            raise
        return None


def owned(state, record, value):
    labels = value.get("Config", {}).get("Labels", {}) if record["kind"] == "containers" else value.get("Labels", {})
    if (labels.get(OWNER) != state["owner"]
            or labels.get(PREFIX + "manifest") != state["plan"]["manifest_sha256"]
            or (record.get("id") and record["id"] != value.get("Id", value.get("Name")))):
        backup.fail("resource ownership/replacement check failed: " + record["name"])
    if record["kind"] == "containers":
        mounts = value.get("Mounts", [])
        if (any(mount.get("Type") != "volume" for mount in mounts)
                or {mount["Destination"]: mount["Name"] for mount in mounts} != record["mounts"]
                or value.get("Image") != record["image"]
                or value.get("HostConfig", {}).get("Privileged")
                or value.get("HostConfig", {}).get("PortBindings")
                or value.get("HostConfig", {}).get("Devices")
                or set(value.get("NetworkSettings", {}).get("Networks", {})) != {state["network"]}):
            backup.fail("unsafe or changed restoration container: " + record["name"])
    elif record["kind"] == "networks" and value.get("Internal") is not True:
        backup.fail("recovery network is no longer internal")
    return value


def ensure(api, directory, state, kind, name, body, mounts=None):
    record = next((r for r in state["resources"] if r["kind"] == kind and r["name"] == name), None)
    if record is None:
        record = {"kind": kind, "name": name, "id": None}
        if kind == "containers":
            record.update({"mounts": mounts, "image": body["Image"]})
        state["resources"].append(record)
        save(directory, state)
    value = lookup(api, kind, name)
    if value is None:
        if record["id"] is not None:
            backup.fail("a tracked recovery resource disappeared: " + name)
        path = "/" + kind + "/create"
        if kind == "containers":
            path += "?name=" + quote(name, safe="")
        backup.docker(api, "POST", path, body)
        value = lookup(api, kind, name)
        if value is None:
            backup.fail("created recovery resource could not be inspected: " + name)
    owned(state, record, value)
    if kind == "containers":
        config = value["Config"]
        actual_env = environment(value)
        expected_env = environment({"Config": body})
        actual_env.pop("HOSTNAME", None)
        expected_env.pop("HOSTNAME", None)
        if (actual_env != expected_env
                or (config.get("Entrypoint") or []) != (body.get("Entrypoint") or [])
                or (config.get("Cmd") or []) != (body.get("Cmd") or [])
                or config.get("User", "") != body.get("User", "")):
            backup.fail("recovery container configuration changed: " + name)
    record["id"] = value.get("Id", value.get("Name"))
    save(directory, state)
    return record


def remove(api, directory, state, record):
    value = lookup(api, record["kind"], record["name"])
    if value is not None:
        owned(state, record, value)
        suffix = "?force=true" if record["kind"] == "containers" else ""
        backup.docker(api, "DELETE", resource_path(record["kind"], record["name"]) + suffix)
        if lookup(api, record["kind"], record["name"]) is not None:
            backup.fail("recovery resource still exists after removal: " + record["name"])
    state["resources"].remove(record)
    save(directory, state)


def resource_labels(state, mounts=()):
    return {
        OWNER: state["owner"], PREFIX + "manifest": state["plan"]["manifest_sha256"],
        "traefik.enable": "false",
        **backup.encode_labels({
            "group": "restore-test", "stop": False, "stop_timeout": 30,
            "covers": [], "excludes": {
                path: "Disposable isolated recovery target; original snapshot retained"
                for path in mounts
            }, "paths": [], "pre": [], "backup": [], "post": [], "restore": [],
        }),
    }


def create_container(api, directory, state, item, role, bootstrap=False):
    source = item["container"]
    config = source["Config"]
    mounts = {}
    for index, mount in enumerate(source["Mounts"]):
        name = state["prefix"] + "-" + role + "-data-" + str(index)
        ensure(api, directory, state, "volumes", name, {
            "Name": name, "Labels": resource_labels(state),
        })
        mounts[mount["Destination"]] = name
    image = backup.docker(api, "GET", "/images/" + quote(source["Image"], safe="") + "/json")
    if set(image.get("Config", {}).get("Volumes") or {}) - mounts.keys():
        backup.fail("image would create an unexpected anonymous volume")
    env = dict(item["env"])
    env.pop("HOSTNAME", None)
    if bootstrap:
        env["POSTGRES_USER"] = env["POSTGRES_DB"] = "sophon_restore"
    body = {
        "Image": source["Image"], "Env": [key + "=" + value for key, value in env.items()],
        "Entrypoint": config.get("Entrypoint"), "Cmd": config.get("Cmd"),
        "User": config.get("User", ""), "WorkingDir": config.get("WorkingDir", ""),
        "Labels": resource_labels(state, mounts),
        "HostConfig": {
            "Binds": [name + ":" + path for path, name in mounts.items()],
            "NetworkMode": state["network"], "RestartPolicy": {"Name": "no"},
            "SecurityOpt": ["label=disable", "no-new-privileges"],
        },
        "NetworkingConfig": {"EndpointsConfig": {
            state["network"]: {"Aliases": [item["name"]]},
        }},
    }
    return ensure(api, directory, state, "containers", state["prefix"] + "-" + role, body, mounts)


def start(api, state, record):
    value = lookup(api, "containers", record["name"])
    if value is None:
        backup.fail("recovery container is missing")
    owned(state, record, value)
    if not value["State"]["Running"]:
        backup.docker(api, "POST", resource_path("containers", record["name"]) + "/start")


def execute(api, directory, state, record, argv, env=None, user=None):
    value = lookup(api, "containers", record["name"])
    if value is None:
        backup.fail("recovery command target is missing")
    owned(state, record, value)
    body = {"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": argv}
    if env:
        body["Env"] = [key + "=" + value for key, value in env.items()]
    if user:
        body["User"] = user
    execution = backup.docker(api, "POST", resource_path("containers", record["name"]) + "/exec", body)["Id"]
    response = backup.docker(api, "POST", "/exec/" + execution + "/start",
                             {"Detach": False, "Tty": False}, stream=True)
    data = bytearray()
    try:
        while chunk := response["read"](backup.CHUNK):
            data.extend(chunk)
            if len(data) > backup.CHUNK:
                backup.fail("recovery command output exceeds the 1 MiB diagnostic limit")
    finally:
        response["close"]()
    output = {1: bytearray(), 2: bytearray()}
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            backup.fail("truncated recovery command output")
        channel, size = struct.unpack(">BxxxI", data[offset:offset + 8])
        offset += 8
        if channel not in output or offset + size > len(data):
            backup.fail("invalid recovery command output framing")
        output[channel].extend(data[offset:offset + size])
        offset += size
    status = backup.docker(api, "GET", "/exec/" + execution + "/json")
    result = {"exit_code": status.get("ExitCode"),
              "stdout": output[1].decode(errors="replace"),
              "stderr": output[2].decode(errors="replace")}
    if status.get("Running") or type(result["exit_code"]) is not int or result["exit_code"] != 0:
        backup.write_json(directory / "last-command.private.json", result)
        backup.fail("recovery command failed; private diagnostics: " + str(directory / "last-command.private.json"))
    return result["stdout"]


def ready(api, directory, state, record, user, database):
    execute(api, directory, state, record, [
        "sh", "-ec",
        'i=0; until pg_isready -U "$RESTORE_USER" -d "$RESTORE_DB"; do '
        'i=$((i+1)); test "$i" -lt 60; sleep 1; done',
    ], {"RESTORE_USER": user, "RESTORE_DB": database})


def validate(api, directory, state, database, app, db_record, app_record):
    system_role = "sophon_recovery_system_" + state["owner"][:12]
    flags = execute(api, directory, state, db_record, [
        "psql", "-X", "-U", database["env"]["POSTGRES_USER"],
        "-d", database["env"]["POSTGRES_DB"], "-v", "ON_ERROR_STOP=1",
        "-Atc", "select rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
        "rolreplication, rolbypassrls, rolpassword is null from pg_authid "
        "where rolname = '" + system_role + "';",
    ])
    if flags.strip() != "f|t|f|f|f|f|t":
        backup.fail("bootstrap system role is not passwordless and login-disabled")
    text = execute(api, directory, state, db_record, [
        "psql", "-X", "-U", database["env"]["POSTGRES_USER"],
        "-d", database["env"]["POSTGRES_DB"], "-v", "ON_ERROR_STOP=1",
        "-Atc", "select count(*) from user_entity; select count(*) from realm;",
    ])
    counts = text.strip().splitlines()
    if len(counts) != 2 or any(not count.isdigit() or int(count) < 1 for count in counts):
        backup.fail("recovered Keycloak database content check failed")
    realm = app["profile"]["verify-realm"]
    text = execute(api, directory, state, app_record, [
        "sh", "-ec",
        'config=/tmp/sophon-restore-kcadm; trap \'rm -f "$config"\' EXIT; '
        'i=0; until /opt/keycloak/bin/kcadm.sh config credentials '
        '--config "$config" --server http://127.0.0.1:8080 --realm master '
        '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" '
        '>/dev/null 2>&1; do i=$((i+1)); test "$i" -lt 45; sleep 2; done; '
        '/opt/keycloak/bin/kcadm.sh get realms --fields realm --config "$config"',
    ])
    realms = [entry["realm"] for entry in json.loads(text)]
    if "master" not in realms or realm not in realms:
        backup.fail("recovered Keycloak realms do not match the declared verification realm")
    state["checks"] = {
        "database_users": int(counts[0]), "database_realms": int(counts[1]),
        "original_admin_login": True, "realms": realms, "bootstrap_role_disabled": True,
        "bootstrap_system_role": system_role, "system_superuser_attribute_required_by_postgres": True,
    }
    save(directory, state)


def run(api, snapshot, group, state_dir, trust_snapshot=False):
    if not trust_snapshot:
        backup.fail("execution requires --trust-snapshot approval for SQL/commands/secrets")
    snapshot = Path(snapshot).absolute()
    state_dir = Path(state_dir).absolute()
    if (snapshot.resolve() == state_dir.resolve()
            or snapshot.resolve() in state_dir.resolve().parents
            or state_dir.resolve() in snapshot.resolve().parents):
        backup.fail("recovery state must not overlap immutable snapshot data")
    _, database, app = selection(snapshot, group)
    current_plan = describe(snapshot, group, database, app)
    with locked(state_dir) as directory:
        if (directory / "state.json").exists():
            state = load_state(api, directory)
            if state["plan"] != current_plan or state["stage"] == "cleaned":
                backup.fail("state belongs to another snapshot/plan or was already cleaned")
        else:
            owner = uuid.uuid4().hex
            prefix = "sophon-restore-" + owner[:12]
            state = {"version": 1, "owner": owner, "prefix": prefix,
                     "target": target(api), "plan": current_plan,
                     "network": prefix + "-internal", "resources": [],
                     "stage": "provisioning", "checks": {}}
            save(directory, state)
        if state["stage"] == "importing":
            # SQL imports are not restartable. Only this run's disposable DB is rebuilt.
            for kind in ("containers", "volumes"):
                for record in list(state["resources"]):
                    if record["kind"] == kind:
                        remove(api, directory, state, record)
            state["stage"] = "provisioning"
            save(directory, state)
        ensure(api, directory, state, "networks", state["network"], {
            "Name": state["network"], "Internal": True, "Labels": resource_labels(state),
        })
        if state["stage"] == "provisioning":
            record = create_container(api, directory, state, database, "database", bootstrap=True)
            start(api, state, record)
            ready(api, directory, state, record, "sophon_restore", "sophon_restore")
            state["stage"] = "importing"
            save(directory, state)
            artifact = database["artifact"]
            path = backup.snapshot_file(snapshot, artifact["path"])
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                payload = source.read(backup.JSON_LIMIT + 1)
            if (len(payload) != artifact["bytes"]
                    or hashlib.sha256(payload).hexdigest() != artifact["sha256"]):
                backup.fail("native SQL archive changed after snapshot validation")
            backup.docker(api, "PUT", resource_path("containers", record["name"]) + "/archive?path=/tmp",
                          payload)
            command = database["recipe"]["commands"][0]
            execute(api, directory, state, record, [
                value.replace(backup.VARIABLE, database["recipe"]["backup_dir"])
                for value in command["argv"]
            ], {"SOPHON_BACKUP_DIR": database["recipe"]["backup_dir"]}, command.get("user"))
            state["stage"] = "imported"
            save(directory, state)
        if state["stage"] == "imported":
            record = next(r for r in state["resources"] if r["kind"] == "containers")
            start(api, state, record)
            ready(api, directory, state, record, database["env"]["POSTGRES_USER"],
                  database["env"]["POSTGRES_DB"])
            system_role = "sophon_recovery_system_" + state["owner"][:12]
            execute(api, directory, state, record, [
                "psql", "-X", "-U", database["env"]["POSTGRES_USER"],
                "-d", database["env"]["POSTGRES_DB"], "-v", "ON_ERROR_STOP=1",
                "-c", "DROP DATABASE IF EXISTS sophon_restore",
                # PostgreSQL requires OID 10 to remain a superuser. Disable login and free the bootstrap name.
                "-c", "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='sophon_restore') "
                "THEN ALTER ROLE sophon_restore NOLOGIN NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD NULL; "
                "ALTER ROLE sophon_restore RENAME TO " + system_role + "; "
                "ELSIF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='" + system_role + "') "
                "THEN RAISE EXCEPTION 'Missing bootstrap system role'; END IF; END $$",
            ])
            state["stage"] = "restarting"
            save(directory, state)
        if state["stage"] == "restarting":
            old = next((r for r in state["resources"] if r["kind"] == "containers"), None)
            if old:
                remove(api, directory, state, old)
            state["stage"] = "starting"
            save(directory, state)
        if state["stage"] not in ("starting", "verified"):
            backup.fail("unsupported or interrupted recovery state")
        db_record = create_container(api, directory, state, database, "database")
        start(api, state, db_record)
        ready(api, directory, state, db_record, database["env"]["POSTGRES_USER"],
              database["env"]["POSTGRES_DB"])
        app_record = create_container(api, directory, state, app, "keycloak")
        start(api, state, app_record)
        validate(api, directory, state, database, app, db_record, app_record)
        state["stage"] = "verified"
        save(directory, state)
        return state


def cleanup(api, state_dir):
    with locked(state_dir) as directory:
        state = load_state(api, directory)
        # Refuse the entire cleanup before mutation if any extant resource was replaced.
        for record in state["resources"]:
            value = lookup(api, record["kind"], record["name"])
            if value is not None:
                owned(state, record, value)
        for kind in ("containers", "volumes", "networks"):
            for record in list(state["resources"]):
                if record["kind"] == kind:
                    remove(api, directory, state, record)
        state["stage"] = "cleaned"
        save(directory, state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "run", "cleanup"])
    parser.add_argument("snapshot", nargs="?")
    parser.add_argument("--group")
    parser.add_argument("--state")
    parser.add_argument("--trust-snapshot", action="store_true")
    args = parser.parse_args()
    try:
        if args.action != "cleanup" and (not args.snapshot or not args.group):
            parser.error("plan/run require a snapshot and --group")
        if args.action == "plan":
            result = plan(args.snapshot, args.group)
        else:
            if not args.state:
                parser.error("run/cleanup require --state")
            api = backup.portainer_api(
                os.environ.get("PORTAINER_URL", ""), os.environ.get("PORTAINER_API_KEY", ""),
                os.environ.get("PORTAINER_ENDPOINT_ID", "1"), timeout=180)
            result = cleanup(api, args.state) if args.action == "cleanup" else run(
                api, args.snapshot, args.group, args.state, args.trust_snapshot)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except RuntimeError as error:
        print("Recovery failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Recovery failed: invalid metadata, output, or local I/O (" +
              type(error).__name__ + "); no success recorded.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
