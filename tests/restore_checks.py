#!/usr/bin/env python3
"""Stdlib recovery checks: python3 tests/restore_checks.py."""

import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import traceback
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roles/kopia/files"))
import backup
import restore
import backup_checks as fixtures


def snapshot(root):
    database = fixtures.container(1, fixtures.spec(
        group="keycloak", covers=["/var/lib/postgresql"],
        paths=[{"type": "archive", "path": backup.VARIABLE + ".sql",
                "name": "postgres-cluster"}],
        restore=[{"argv": ["sh", "-ec",
                           'psql -X -U sophon_restore -d sophon_restore '
                           '-v ON_ERROR_STOP=1 -f "$SOPHON_BACKUP_DIR.sql"']}]))
    database["Name"] = "/keycloak-db"
    database["Image"] = "sha256:" + "a" * 64
    database["Mounts"][0]["Destination"] = "/var/lib/postgresql"
    database["Config"].update({
        "Env": ["POSTGRES_USER=keycloak", "POSTGRES_DB=keycloak", "POSTGRES_PASSWORD=private"],
        "Entrypoint": ["docker-entrypoint.sh"], "Cmd": ["postgres"],
    })
    database["Config"]["Labels"] = {
        **backup.encode_labels(backup.decode_labels(database["Config"]["Labels"])),
        "sophon.restore.version": "1", "sophon.restore.kind": "postgres-cluster",
        "sophon.restore.artifact": "postgres-cluster",
    }
    app = fixtures.container(2, fixtures.spec(
        group="keycloak", covers=[], paths=[],
        excludes={"/data": "Transient runtime state; PostgreSQL is authoritative"}))
    app["Name"] = "/keycloak"
    app["Image"] = "sha256:" + "b" * 64
    app["Config"].update({
        "Env": ["KC_DB=postgres", "KC_DB_URL_HOST=keycloak-db",
                "KC_DB_URL_DATABASE=keycloak", "KC_DB_USERNAME=keycloak",
                "KC_BOOTSTRAP_ADMIN_USERNAME=admin", "KC_BOOTSTRAP_ADMIN_PASSWORD=private"],
        "Entrypoint": ["/opt/keycloak/bin/kc.sh"], "Cmd": ["start", "--http-enabled=true"],
    })
    app["Config"]["Labels"] = {
        **backup.encode_labels(backup.decode_labels(app["Config"]["Labels"])),
        "sophon.restore.version": "1", "sophon.restore.kind": "keycloak",
        "sophon.restore.depends-on": "keycloak-db", "sophon.restore.verify-realm": "sophon",
    }
    api, state = fixtures.fake_api([database, app])

    def native_dump(method, path, body, data):
        if "/archive?" in path:
            name = Path(parse_qs(urlsplit(path).query)["path"][0]).name
            data["artifact"] = fixtures.tar_bytes(b"-- full cluster SQL\n", name=name)

    state["hook"] = native_dump
    return Path(backup.prepare(api, root / "source", poll_interval=.001)["overrideRoot"])


def test_plan_is_verified_read_only_and_redacted(root):
    source = snapshot(root)
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    plan = restore.plan(source, "keycloak")
    assert plan["version"] == 1
    assert plan["group"] == "keycloak"
    assert plan["order"] == ["keycloak-db", "keycloak"]
    assert plan["phases"] == [
        "provision-isolated", "bootstrap-postgres", "import-postgres",
        "restart-original-postgres", "start-keycloak", "verify-keycloak",
    ]
    assert "private" not in json.dumps(plan)
    assert before == sorted(p.relative_to(root) for p in root.rglob("*"))


def test_plan_rejects_missing_profiles_and_corrupt_artifacts(root):
    source = snapshot(root)
    path = source / "manifest.json"
    original = backup.read_json(path)
    changed = copy.deepcopy(original)
    del changed["containers"][0]["Config"]["Labels"]["sophon.restore.version"]
    backup.write_json(path, changed)
    try:
        restore.plan(source, "keycloak")
    except RuntimeError as error:
        assert "profile" in str(error)
    else:
        raise AssertionError("A manual-only snapshot was accepted for automation")
    backup.write_json(path, original)
    artifact = source / original["artifacts"][0]["path"]
    artifact.write_bytes(b"corrupt")
    try:
        restore.plan(source, "keycloak")
    except RuntimeError as error:
        assert "checksum" in str(error)
    else:
        raise AssertionError("Corrupt SQL was accepted")


def engine():
    state = {"containers": {}, "volumes": {}, "networks": {}, "execs": {},
             "events": [], "fail_import": False}

    def transport(method, path, body, stream):
        prefix = "/api/endpoints/1/docker"
        assert path.startswith(prefix)
        path = path[len(prefix):]
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        kind, name = parts[0], parts[1] if len(parts) > 1 else ""
        state["events"].append((method, path))
        if kind == "images":
            return {"Id": name, "Config": {"Volumes": {}}}
        if kind in ("networks", "volumes") and name == "create":
            name = body["Name"]
            state[kind][name] = copy.deepcopy(body)
            return {"Id": name, "Name": name}
        if kind in ("networks", "volumes"):
            record = state[kind].get(name)
            if record is None:
                raise RuntimeError("API request failed (HTTP 404)")
            if method == "DELETE":
                if kind == "volumes":
                    assert not any(name == mount["Name"] for container in state["containers"].values()
                                   for mount in container["Mounts"]), "Cannot remove an attached volume"
                del state[kind][name]
                return None
            return copy.deepcopy(record)
        if kind == "containers" and name == "create":
            name = parse_qs(parsed.query)["name"][0]
            config = copy.deepcopy(body)
            mounts = [{"Type": "volume", "Name": value.split(":")[0],
                       "Destination": value.split(":")[1], "RW": True}
                      for value in config["HostConfig"]["Binds"]]
            state["containers"][name] = {
                "Id": name, "Name": "/" + name, "Config": config,
                "Image": config["Image"], "Mounts": mounts,
                "HostConfig": config["HostConfig"], "State": {"Running": False},
                "NetworkSettings": {"Networks": {config["HostConfig"]["NetworkMode"]: {}}},
            }
            return {"Id": name}
        if kind == "containers":
            current = state["containers"].get(name)
            if current is None:
                raise RuntimeError("API request failed (HTTP 404)")
            action = parts[2] if len(parts) > 2 else ""
            if method == "DELETE":
                del state["containers"][name]
                return None
            if action == "json":
                return copy.deepcopy(current)
            if action == "start":
                current["State"]["Running"] = True
                current["Config"]["Env"].append("HOSTNAME=engine-generated")
                return None
            if action == "archive":
                assert method == "PUT" and isinstance(body, bytes)
                return None
            if action == "exec":
                ident = str(len(state["execs"]) + 1)
                state["execs"][ident] = body
                return {"Id": ident}
        if kind == "exec":
            command = state["execs"][name]["Cmd"]
            script = " ".join(command)
            failed = state["fail_import"] and "-f " in script
            if parts[2] == "json":
                return {"Running": False, "ExitCode": 1 if failed else 0}
            text = b""
            if "user_entity" in script:
                text = b"2\n2\n"
            if "rolcanlogin" in script:
                text = b"f|t|f|f|f|f|t\n"
            if "get realms" in script:
                text = b'[{"realm":"master"},{"realm":"sophon"}]'
            if "get clients" in script:
                text = b'[{"clientId":"nextcloud"}]'
            if failed:
                text = b"import failed; secret=private"
            payload = struct.pack(">BxxxI", 1, len(text)) + text
            return fixtures.stream_bytes(payload)
        raise AssertionError((method, path))

    return {"endpoint": 1, "transport": transport}, state


def test_run_requires_approval_before_engine_access(root):
    api, remote = engine()
    try:
        restore.run(api, root / "nonexistent", "keycloak", root / "state")
    except RuntimeError as error:
        assert "trust" in str(error)
    else:
        raise AssertionError("Execution without snapshot trust was allowed")
    assert not remote["events"]


def test_run_verifies_and_cleans_only_owned_fresh_resources(root):
    source = snapshot(root)
    api, remote = engine()
    result = restore.run(api, source, "keycloak", root / "state", trust_snapshot=True)
    assert result["stage"] == "verified"
    assert result["checks"]["database_users"] == 2
    assert result["checks"]["original_admin_login"] is True
    assert result["checks"]["bootstrap_role_disabled"] is True
    assert "private" not in json.dumps(result)
    assert len(remote["containers"]) == 2
    assert len(remote["volumes"]) == 2
    assert len(remote["networks"]) == 1
    network = next(iter(remote["networks"].values()))
    assert network["Internal"] is True
    for container in remote["containers"].values():
        assert not container["HostConfig"].get("PortBindings")
        assert not container["HostConfig"].get("Privileged")
        assert all(mount["Type"] == "volume" for mount in container["Mounts"])
        backup.parse_spec(container)
    before = len([event for event in remote["events"] if event[0] == "POST"
                  and "/containers/create?" in event[1]])
    resumed = restore.run(api, source, "keycloak", root / "state", trust_snapshot=True)
    assert resumed["stage"] == "verified"
    assert before == len([event for event in remote["events"] if event[0] == "POST"
                          and "/containers/create?" in event[1]])
    cleanup = restore.cleanup(api, root / "state")
    assert cleanup["stage"] == "cleaned"
    assert not remote["containers"] and not remote["volumes"] and not remote["networks"]


def test_failed_import_is_rebuilt_not_replayed_on_dirty_database(root):
    source = snapshot(root)
    api, remote = engine()
    remote["fail_import"] = True
    try:
        restore.run(api, source, "keycloak", root / "state", trust_snapshot=True)
    except RuntimeError as error:
        assert "private diagnostics" in str(error)
        assert "secret=private" not in str(error)
    else:
        raise AssertionError("A failed native import was accepted")
    assert backup.read_json(root / "state/state.json")["stage"] == "importing"
    remote["fail_import"] = False
    state = restore.run(api, source, "keycloak", root / "state", trust_snapshot=True)
    assert state["stage"] == "verified"
    deletes = [path for method, path in remote["events"] if method == "DELETE"]
    assert any("/volumes/" in path for path in deletes)
    restore.cleanup(api, root / "state")


def test_cleanup_refuses_replaced_resources_before_any_deletion(root):
    source = snapshot(root)
    api, remote = engine()
    restore.run(api, source, "keycloak", root / "state", trust_snapshot=True)
    value = next(iter(remote["containers"].values()))
    value["Config"]["Labels"][restore.OWNER] = "somebody-else"
    before = len(remote["events"])
    try:
        restore.cleanup(api, root / "state")
    except RuntimeError as error:
        assert "ownership" in str(error)
    else:
        raise AssertionError("Foreign resource was removed")
    assert not any(method == "DELETE" for method, path in remote["events"][before:])


def test_execution_refuses_state_inside_immutable_snapshot(root):
    source = snapshot(root)
    api, remote = engine()
    try:
        restore.run(api, source, "keycloak", source / "state", trust_snapshot=True)
    except RuntimeError as error:
        assert "overlap" in str(error)
    else:
        raise AssertionError("Recovery state was written into the immutable snapshot")
    assert not remote["events"]
    assert not (source / "state").exists()


def test_plan_refuses_external_mount_and_mismatched_dependency(root):
    source = snapshot(root)
    path = source / "manifest.json"
    original = backup.read_json(path)
    for mode in ("bind", "dependency", "unknown-version"):
        changed = copy.deepcopy(original)
        if mode == "bind":
            changed["containers"][1]["Mounts"].append({
                "Type": "bind", "Source": "/run/podman/podman.sock",
                "Destination": "/var/run/docker.sock",
            })
        else:
            labels = changed["containers"][1]["Config"]["Labels"]
            labels["sophon.restore." + ("depends-on" if mode == "dependency" else "version")] = (
                "wrong-db" if mode == "dependency" else "99")
        backup.write_json(path, changed)
        try:
            restore.plan(source, "keycloak")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Unsafe recovery configuration accepted: " + mode)


def run_tests():
    tests = sorted((name, value) for name, value in globals().items()
                   if name.startswith("test_") and callable(value))
    failures = 0
    for name, test in tests:
        with tempfile.TemporaryDirectory(prefix=".restore-tests-", dir=Path(__file__).parent) as root:
            try:
                test(Path(root))
                print("PASS " + name)
            except BaseException:
                failures += 1
                print("FAIL " + name)
                traceback.print_exc()
    print(f"{len(tests) - failures}/{len(tests)} recovery tests passed")
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(run_tests())
