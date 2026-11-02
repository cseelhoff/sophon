#!/usr/bin/env python3
"""Runnable stdlib checks: python3 tests/backup_checks.py (no network or workload access)."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roles/kopia/files"))
import backup


def ident(number):
    return f"{number:064x}"


def spec(**changes):
    value = {
        "group": "app", "covers": ["/data"], "excludes": {},
        "paths": [{"type": "archive", "path": "/data", "name": "data"}],
        "pre": [], "backup": [], "post": [], "restore": [],
        "stop": False, "stop_timeout": 10,
    }
    value.update(changes)
    return value


def command(name, user=None):
    value = {"argv": [name]}
    if user is not None:
        value["user"] = user
    return value


def container(number=1, definition=None, running=True, volume=None):
    return {
        "Id": ident(number), "Name": f"/app-{number}", "Image": "sha256:image",
        "Config": {
            "Image": "test:1", "Env": ["SECRET=deployment-secret"],
            "Labels": {} if definition is None else {
                backup.VERSION_LABEL: "1", backup.SPEC_LABEL: json.dumps(definition),
            },
        },
        "State": {"Running": running},
        "Mounts": [{"Type": "volume", "Name": volume or f"volume-{number}",
                    "Destination": "/data", "RW": True}],
    }


def tar_bytes(contents=b"database-data", name="data/file", link=None, mode="w"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=mode) as archive:
        record = tarfile.TarInfo(name)
        record.uid, record.gid, record.mode = 1000, 1000, 0o600
        if link is not None:
            record.type, record.linkname = tarfile.SYMTYPE, link
            archive.addfile(record)
        else:
            record.size = len(contents)
            archive.addfile(record, io.BytesIO(contents))
    return output.getvalue()


def stream_bytes(data, broken=False):
    source = io.BytesIO(data)
    state = {"reads": 0, "closed": False}

    def read(size):
        assert size <= backup.CHUNK
        state["reads"] += 1
        if broken and state["reads"] > 1:
            raise OSError("transport includes a SECRET")
        return source.read(min(size, 1024) if broken else size)

    def close():
        state["closed"] = True
        source.close()

    return {"read": read, "close": close}


def fake_api(containers):
    state = {
        "containers": {item["Id"]: copy.deepcopy(item) for item in containers},
        "events": [], "execs": {}, "next_exec": 100,
        "command_fail": {}, "hang": set(), "hook": None,
        "artifact": tar_bytes(), "portainer_artifact": tar_bytes(mode="w:gz"),
        "stream_broken": False,
        "stacks": [{"Id": 1, "EndpointId": 1,
                    "Env": [{"name": "DB_PASSWORD", "value": "stack-secret"}]}],
        "pid": b"1234\n", "pid_missing": False, "term_ignored": False,
        "stop_failure": None, "start_failures": set(), "listing_count": 0,
    }

    def transport(method, path, body, stream):
        if state["hook"]:
            state["hook"](method, path, body, state)
        prefix = "/api/endpoints/1/docker"
        if path.startswith(prefix):
            path = path[len(prefix):]
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if path == "/containers/json?all=true":
            state["events"].append(("inventory",))
            state["listing_count"] += 1
            return [{"Id": key} for key in state["containers"]]
        if path == "/api/stacks":
            return copy.deepcopy(state["stacks"])
        if path == "/api/stacks/1/file":
            return {"StackFileContent": "services:\n  app:\n    secret: stack-secret"}
        if path == "/api/backup":
            assert method == "POST" and body == {"password": ""} and stream
            state["events"].append(("portainer-backup",))
            return stream_bytes(state["portainer_artifact"])
        if parts[0] == "containers":
            member = parts[1]
            current = state["containers"].get(member)
            if current is None:
                raise RuntimeError("container gone")
            match parts[2]:
                case "json":
                    return copy.deepcopy(current)
                case "stop":
                    state["events"].append(("stop", member))
                    assert current["State"]["Running"]
                    if state["stop_failure"] != "before":
                        current["State"]["Running"] = False
                    if state["stop_failure"]:
                        raise RuntimeError("stop request failed")
                    return None
                case "start":
                    state["events"].append(("start", member))
                    if member in state["start_failures"]:
                        raise RuntimeError("restart request failed")
                    current["State"]["Running"] = True
                    return None
                case "archive":
                    assert stream
                    source_path = parse_qs(parsed.query)["path"][0]
                    if source_path.startswith("/tmp/sophon-exec-"):
                        state["events"].append(("pid-read", member))
                        if state["pid_missing"]:
                            raise RuntimeError("PID file missing")
                        return stream_bytes(tar_bytes(state["pid"], name="pid"))
                    state["events"].append(("archive", member, source_path))
                    return stream_bytes(state["artifact"], state["stream_broken"])
                case "exec":
                    assert method == "POST"
                    assert body["AttachStdout"] is False and body["AttachStderr"] is False
                    assert current["State"]["Running"]
                    value = ident(state["next_exec"])
                    state["next_exec"] += 1
                    state["execs"][value] = {
                        "body": copy.deepcopy(body), "member": member,
                        "Running": False, "ExitCode": 0,
                    }
                    state["events"].append(("exec-create", member, value))
                    return {"Id": value}
        if parts[0] == "exec":
            value = state["execs"][parts[1]]
            if parts[2] == "json":
                return {"Running": value["Running"], "ExitCode": value["ExitCode"],
                        "Pid": 999999}  # Deliberately wrong host PID: never use it.
            if parts[2] == "start":
                assert body == {"Detach": True, "Tty": False}
                original = value["body"]
                if "Env" in original:
                    argv = original["Cmd"][original["Cmd"].index("sophon") + 1:]
                    name = argv[0]
                    state["events"].append(("cmd", value["member"], name))
                    value["Running"] = name in state["hang"]
                    value["ExitCode"] = state["command_fail"].get(name, 0)
                else:
                    script = original["Cmd"][2]
                    sig = "KILL" if "kill -KILL" in script else "TERM"
                    assert original["User"] == "0"
                    assert original["Cmd"][-1] == "1234"
                    assert "999999" not in script
                    state["events"].append(("cancel", value["member"], sig))
                    if sig == "KILL" or not state["term_ignored"]:
                        for execution in state["execs"].values():
                            if execution["member"] == value["member"]:
                                execution["Running"] = False
                return None
        raise AssertionError((method, path))

    return {"endpoint": 1, "transport": transport}, state


def prepare(api, root, **options):
    return backup.prepare(api, root, command_timeout=options.get("command_timeout", .03),
                          poll_interval=.001)


def expect_failure(fn, text=None):
    try:
        fn()
    except RuntimeError as error:
        if text is not None:
            assert text in str(error), str(error)
        assert "deployment-secret" not in str(error)
        assert "stack-secret" not in str(error)
        return str(error)
    raise AssertionError("expected failure")


def meaningful(state):
    return [event for event in state["events"]
            if event[0] not in ("inventory", "exec-create", "pid-read")]


def test_group_order_and_private_inventory(root):
    definition = spec(stop=True, pre=[command("pre")], post=[command("post")],
                      restore=[{"argv": ["restore-native", backup.VARIABLE + "-postgres"],
                                "user": "postgres"}])
    api, state = fake_api([container(definition=definition)])
    result = prepare(api, root)
    assert meaningful(state) == [
        ("cmd", ident(1), "pre"), ("stop", ident(1)),
        ("archive", ident(1), "/data"), ("start", ident(1)),
        ("cmd", ident(1), "post"),
    ]
    run = Path(result["overrideRoot"])
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["containers"][0]["Config"]["Env"] == ["SECRET=deployment-secret"]
    assert manifest["containers"][0]["State"]["Running"] is True
    assert manifest["stacks"][0]["stack"]["Env"][0]["value"] == "stack-secret"
    assert manifest["restore"][0]["commands"] == definition["restore"]
    assert manifest["protection"]["volume-1"]["owners"] == [ident(1)]
    assert manifest["artifacts"][0]["payload_bytes"] == len(b"database-data")
    assert not (root / "journal.json").exists()
    assert root.stat().st_mode & 0o777 == 0o700
    assert run.stat().st_mode & 0o777 == 0o700
    assert (run / "manifest.json").stat().st_mode & 0o777 == 0o600
    raw = run / manifest["artifacts"][0]["path"]
    assert raw.stat().st_mode & 0o777 == 0o600
    with tarfile.open(raw) as archive:
        assert archive.getmembers()[0].uid == 1000


def test_live_dump_expansion_and_user(root):
    definition = spec(
        paths=[{"type": "archive", "path": backup.VARIABLE + "-postgres/dump",
                "name": "dump"}],
        backup=[{"argv": ["dump", backup.VARIABLE + "-postgres"], "user": "postgres"}],
    )
    api, state = fake_api([container(definition=definition)])
    result = prepare(api, root)
    command_body = next(value["body"] for value in state["execs"].values()
                        if "Env" in value["body"])
    assert command_body["User"] == "postgres"
    assert command_body["Cmd"][-1].endswith("-postgres")
    assert "setsid /bin/sh" in command_body["Cmd"][2]
    assert 'wait "$child"' in command_body["Cmd"][2]
    assert any(value.startswith("SOPHON_PIDFILE=/tmp/sophon-exec-")
               for value in command_body["Env"])
    manifest = json.loads((Path(result["overrideRoot"]) / "manifest.json").read_text())
    assert manifest["artifacts"][0]["container_path"].endswith("-postgres/dump")
    assert not any(event[0] in ("stop", "start") for event in state["events"])


def test_partial_pre_failure_posts_all_attempted(root):
    first = spec(pre=[command("pre-ok")], post=[command("post-ok")])
    second = spec(pre=[command("pre-fail"), command("not-attempted")],
                  post=[command("post-partial")])
    api, state = fake_api([container(1, first), container(2, second)])
    state["command_fail"]["pre-fail"] = 17
    expect_failure(lambda: prepare(api, root), "exit=17")
    assert meaningful(state) == [
        ("cmd", ident(1), "pre-ok"), ("cmd", ident(2), "pre-fail"),
        ("cmd", ident(1), "post-ok"), ("cmd", ident(2), "post-partial"),
    ]
    assert not (root / "journal.json").exists()
    assert all(item["State"]["Running"] for item in state["containers"].values())


def test_stop_timeout_after_stop_recovers(root):
    api, state = fake_api([container(definition=spec(stop=True, pre=[command("pre")],
                                                   post=[command("post")]))])
    state["stop_failure"] = "after"
    expect_failure(lambda: prepare(api, root), "stop request failed")
    assert ("start", ident(1)) in meaningful(state)
    assert meaningful(state)[-1] == ("cmd", ident(1), "post")
    assert state["containers"][ident(1)]["State"]["Running"]
    assert not (root / "journal.json").exists()


def test_stop_failure_before_stop_does_not_restart(root):
    api, state = fake_api([container(definition=spec(stop=True, pre=[command("pre")],
                                                   post=[command("post")]))])
    state["stop_failure"] = "before"
    expect_failure(lambda: prepare(api, root))
    assert not any(event[0] == "start" for event in meaningful(state))
    assert meaningful(state)[-1] == ("cmd", ident(1), "post")


def test_native_stopped_archive_root_directory_is_safe(root):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo("")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        data = tarfile.TarInfo("database.bin")
        data.size = 4
        archive.addfile(data, io.BytesIO(b"data"))
    path = root / "native-stopped.tar"
    path.write_bytes(output.getvalue())
    assert backup.archive_payload(path) == 4
    destination = root / "isolated"
    destination.mkdir()
    with tarfile.open(path) as archive:
        archive.extractall(destination, filter=backup.safe_tar_member)
    assert (destination / "database.bin").read_bytes() == b"data"
    expect_failure(lambda: backup.safe_tar_member(tarfile.TarInfo(""), str(destination)))
    expect_failure(lambda: backup.safe_tar_member(tarfile.TarInfo("../escape"), str(destination)))


def test_bodyless_native_api_response_accepts_whitespace(root):
    response = Mock(status=204)
    response.read.return_value = b"\n"
    connection = Mock()
    connection.getresponse.return_value = response
    with patch.object(backup.http.client, "HTTPSConnection", return_value=connection):
        assert backup.request(backup.portainer_api("https://portainer.example", "secret"),
                              "POST", "/api/native-bodyless") is None
    connection.close.assert_called_once()


def test_native_stopped_absolute_children_are_portable(root):
    path = root / "stopped.tar"
    with tarfile.open(path, "w") as archive:
        directory = tarfile.TarInfo("")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("/configuration/database.bin")
        member.size, member.uid, member.gid, member.mode = 4, 1000, 1000, 0o600
        member.pax_headers["path"] = member.name
        archive.addfile(member, io.BytesIO(b"data"))
        link = tarfile.TarInfo("/configuration/database-link")
        link.type, link.linkname = tarfile.LNKTYPE, member.name
        archive.addfile(link)
    assert backup.normalize_native_archive(path, "/appsmith-stacks")
    assert path.stat().st_mode & 0o777 == 0o600
    assert backup.archive_payload(path) == 4
    with tarfile.open(path) as archive:
        member = archive.getmember("appsmith-stacks/configuration/database.bin")
        assert (member.uid, member.gid, member.mode) == (1000, 1000, 0o600)
        destination = root / "restore"
        destination.mkdir()
        archive.extractall(destination, filter=backup.safe_tar_member)
    assert (destination / "appsmith-stacks/configuration/database-link").read_bytes() == b"data"


def test_native_stopped_normalization_rejects_traversal_and_external_links(root):
    for name, kind, target in [
        ("/../../escape", tarfile.REGTYPE, ""),
        ("", tarfile.REGTYPE, ""),
        ("/link", tarfile.SYMTYPE, "/etc/shadow"),
        ("/link", tarfile.LNKTYPE, "../../escape"),
        ("/link", tarfile.LNKTYPE, ""),
    ]:
        path = root / "bad-native.tar"
        with tarfile.open(path, "w") as archive:
            directory = tarfile.TarInfo("")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, target
            archive.addfile(member)
        original = path.read_bytes()
        expect_failure(lambda: backup.normalize_native_archive(path, "/data"))
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".canonical").exists()


def test_native_stopped_artifact_manifest_verifies_after_normalization(root):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo("")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("/database.bin")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"data"))
    api, state = fake_api([container(definition=spec(stop=True))])
    state["artifact"] = output.getvalue()
    snapshot = Path(prepare(api, root)["overrideRoot"])
    restore_root = root / "verification"
    destination = restore_root / "artifacts"
    assert backup.verify(snapshot, destination, restore_root)["bytes"] == 4
    assert (destination / "app" / ident(1) / "data" / "data" / "database.bin").read_bytes() == b"data"


def test_native_portainer_absolute_export_verifies(root):
    definition = spec(paths=[{"type": "portainer-backup", "name": "control-plane"}])
    api, state = fake_api([container(definition=definition)])
    state["portainer_artifact"] = tar_bytes(b"data", "/portainer.db", mode="w:gz")
    snapshot = Path(prepare(api, root)["overrideRoot"])
    restore_root = root / "verification"
    destination = restore_root / "artifacts"
    assert backup.verify(snapshot, destination, restore_root)["bytes"] == 4
    assert (destination / "app" / ident(1) / "control-plane" / "portainer.db").read_bytes() == b"data"
    for name in ("/../escape", ""):
        path = root / "bad-portainer.tar.gz"
        path.write_bytes(tar_bytes(b"data", name, mode="w:gz"))
        expect_failure(lambda: backup.normalize_native_archive(path, "/data", portainer_backup=True)
                       if name else backup.archive_payload(path))


def test_native_dump_filename_suffixes_are_safe(root):
    for suffix in (".sql", ".archive.gz", "-config.ldif"):
        assert backup.container_path("${SOPHON_BACKUP_DIR}" + suffix, "/tmp/sophon-run") == (
            "/tmp/sophon-run" + suffix)
    for suffix in ("/../escape", ".sql/../../escape", "//escape", "."):
        expect_failure(lambda: backup.container_path("${SOPHON_BACKUP_DIR}" + suffix,
                                                     "/tmp/sophon-run"))


def test_forced_kill_refuses_export_and_recovers(root):
    api, state = fake_api([container(definition=spec(stop=True))])
    state["containers"][ident(1)]["State"]["ExitCode"] = 137
    expect_failure(lambda: prepare(api, root), "forcibly killed")
    assert state["containers"][ident(1)]["State"]["Running"]
    assert not any(event[0] == "archive" for event in state["events"])


def test_initially_stopped_preserved(root):
    api, state = fake_api([container(definition=spec(
        stop=True, pre=[command("pre")], post=[command("post")]), running=False)])
    prepare(api, root)
    assert meaningful(state) == [("archive", ident(1), "/data")]
    assert not state["containers"][ident(1)]["State"]["Running"]


def test_initially_stopped_dump_fails_before_mutation(root):
    api, state = fake_api([container(definition=spec(backup=[command("dump")]),
                                   running=False)])
    expect_failure(lambda: prepare(api, root), "originally stopped")
    assert not meaningful(state)
    assert not state["containers"][ident(1)]["State"]["Running"]


def test_malformed_specs(root):
    definitions = [
        spec(unknown="no"), spec(covers=[]),  # Remove required key below.
        spec(group="../bad"), spec(covers=["/not-a-mount"]),
        spec(excludes={"/data": "duplicate"}), spec(excludes={"/other": "rebuild"}),
        spec(covers=[], excludes={"/data": ""}), spec(paths=[]),
        spec(stop="true"), spec(stop_timeout=0), spec(stop_timeout=True),
        spec(stop=True, backup=[command("dump")]),
        spec(pre=[{"argv": []}]), spec(pre=[{"argv": ["\x00bad"]}]),
        spec(pre=[{"argv": ["dump"], "password": "secret"}]),
        spec(pre=[{"argv": ["dump"], "user": ""}]),
        spec(paths=[{"type": "archive", "path": "/data/../etc", "name": "data"}]),
        spec(paths=[{"type": "archive", "path": "relative", "name": "data"}]),
        spec(paths=[{"type": "archive", "path": "/data", "name": "../data"}]),
        spec(paths=[{"type": "other", "name": "data"}]),
        spec(paths=[{"type": "archive", "path": "/data", "name": "data"}] * 2),
        spec(stop=True, paths=[{"type": "portainer-backup", "name": "config"}]),
        spec(paths=[{"type": "archive", "path": backup.VARIABLE + "/../data",
                     "name": "data"}]),
        spec(paths=[{"type": "archive", "path": backup.VARIABLE + "bad/dump",
                     "name": "data"}]),
    ]
    del definitions[1]["post"]
    for definition in definitions:
        expect_failure(lambda: backup.parse_spec(container(definition=definition)))
    for labels in (
        {backup.VERSION_LABEL: "2", backup.SPEC_LABEL: "{}"},
        {backup.VERSION_LABEL: "1", backup.SPEC_LABEL: "{bad"},
        {backup.VERSION_LABEL: "1"},
        {backup.SPEC_LABEL: "{}"},
        {backup.VERSION_LABEL: "1", backup.SPEC_LABEL: '{"group":"a","group":"b"}'},
    ):
        item = container()
        item["Config"]["Labels"] = labels
        expect_failure(lambda: backup.parse_spec(item))


def test_uncovered_exited_volume(root):
    api, state = fake_api([container(1, spec()), container(2, running=False)])
    expect_failure(lambda: prepare(api, root), "uncovered")
    assert not meaningful(state)


def test_shared_owner_excludes_and_unlabeled_inventory(root):
    owner = container(1, spec(), volume="shared")
    cron = container(2, running=False, volume="shared")
    excluded = container(3, spec(covers=[], paths=[],
                                 excludes={"/data": "reconstructible cache"}))
    excluded["Mounts"].append({"Type": "bind", "Source": "/run/podman.sock",
                               "Destination": "/socket"})
    api, state = fake_api([owner, cron, excluded])
    result = prepare(api, root)
    manifest = json.loads((Path(result["overrideRoot"]) / "manifest.json").read_text())
    assert len(manifest["containers"]) == 3
    assert meaningful(state) == [("archive", ident(1), "/data")]
    assert manifest["protection"]["shared"]["owners"] == [ident(1)]
    assert manifest["protection"]["volume-3"]["excludes"][0]["reason"] == "reconstructible cache"


def test_native_portainer_backup(root):
    api, state = fake_api([container(definition=spec(
        paths=[{"type": "portainer-backup", "name": "portainer"}]))])
    result = prepare(api, root)
    manifest = json.loads((Path(result["overrideRoot"]) / "manifest.json").read_text())
    assert meaningful(state) == [("portainer-backup",)]
    assert manifest["artifacts"][0]["type"] == "portainer-backup"
    assert manifest["artifacts"][0]["path"].endswith(".tar.gz")


def test_export_network_failure_recovers_without_secret(root):
    api, state = fake_api([container(definition=spec(stop=True, pre=[command("pre")],
                                                   post=[command("post")]))])
    state["stream_broken"] = True
    error = expect_failure(lambda: prepare(api, root))
    assert "SECRET" not in error
    assert meaningful(state)[-2:] == [("start", ident(1)), ("cmd", ident(1), "post")]
    assert not (root / "journal.json").exists()
    assert not list(root.glob("runs/*/manifest.json"))
    assert not list((root / "runs").iterdir())


def test_failed_run_cleanup_preserves_success_and_unowned_staging_under_lock(root):
    api, state = fake_api([container(1, spec()), container(2, spec())])
    success = Path(prepare(api, root)["overrideRoot"])
    before = {path.relative_to(success): path.read_bytes()
              for path in success.rglob("*") if path.is_file()}
    unrelated = backup.private_dir(root / "runs" / "preexisting-failed-run")
    (unrelated / "partial").write_bytes(b"must-not-delete-older-staging")
    candidate = None

    def fail_last_archive(method, path, body, state):
        nonlocal candidate
        if f"/containers/{ident(2)}/archive?" in path:
            journal = json.loads((root / "journal.json").read_text())
            candidate = (root / "runs" / journal["run"]).resolve(strict=True)
            state["stream_broken"] = True

    removed = []
    original = shutil.rmtree

    def checked_cleanup(path, *args, **kwargs):
        assert Path(path) == candidate and candidate != success
        assert list(candidate.glob(f"artifacts/app/{ident(1)}/*.tar"))
        assert not (root / "journal.json").exists()
        descriptor = os.open(root / ".collector.lock", os.O_RDWR)
        try:
            try:
                backup.fcntl.flock(descriptor, backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("prepare must retain lock during failed-run deletion")
        finally:
            os.close(descriptor)
        removed.append(Path(path))
        original(path, *args, **kwargs)

    state["hook"] = fail_last_archive
    shutil.rmtree = checked_cleanup
    try:
        expect_failure(lambda: prepare(api, root), "export failed")
    finally:
        shutil.rmtree = original
    assert removed == [candidate] and not candidate.exists()
    assert set((root / "runs").iterdir()) == {success, unrelated}
    assert all((success / relative).read_bytes() == data for relative, data in before.items())
    assert (unrelated / "partial").read_bytes() == b"must-not-delete-older-staging"


def test_cleanup_failure_preserves_original_error_without_secret(root):
    api, state = fake_api([container(definition=spec(pre=[command("pre-fail")]))])
    state["command_fail"]["pre-fail"] = 17
    original = shutil.rmtree

    def broken_cleanup(path, *args, **kwargs):
        raise OSError("cleanup SECRET must not reach stderr")

    shutil.rmtree = broken_cleanup
    try:
        error = expect_failure(lambda: prepare(api, root), "exit=17")
    finally:
        shutil.rmtree = original
    assert "failed-run staging cleanup failed" in error and "SECRET" not in error
    assert not (root / "journal.json").exists()
    assert len(list((root / "runs").iterdir())) == 1


def test_failed_cleanup_refuses_replaced_directory_and_preserves_success(root):
    api, state = fake_api([container(definition=spec())])
    success = Path(prepare(api, root)["overrideRoot"])
    manifest = (success / "manifest.json").read_bytes()
    swapped = None

    def replace_staging(method, path, body, state):
        nonlocal swapped
        if "/archive?" in path:
            journal = json.loads((root / "journal.json").read_text())
            candidate = root / "runs" / journal["run"]
            swapped = root / "runs" / "renamed-failed-run"
            candidate.rename(swapped)
            candidate.symlink_to(success, target_is_directory=True)
            raise RuntimeError("export target failed")

    state["hook"] = replace_staging
    error = expect_failure(lambda: prepare(api, root), "export target failed")
    assert "failed-run staging cleanup failed" in error
    assert success.is_dir() and (success / "manifest.json").read_bytes() == manifest
    assert swapped.is_dir()
    assert not (root / "journal.json").exists()


def test_initial_journal_failure_cleans_only_new_run(root):
    api, state = fake_api([container(definition=spec())])
    original = backup.save_journal

    def broken_save(*args):
        raise RuntimeError("journal persistence failed")

    backup.save_journal = broken_save
    try:
        expect_failure(lambda: prepare(api, root), "journal persistence failed")
    finally:
        backup.save_journal = original
    assert not (root / "journal.json").exists()
    assert not list((root / "runs").iterdir())
    assert not meaningful(state)


def test_dump_command_failure_restarts_other_members(root):
    writer = container(1, spec(stop=True, pre=[command("pre")], post=[command("post")]))
    db = container(2, spec(backup=[command("dump")]))
    api, state = fake_api([writer, db])
    state["command_fail"]["dump"] = 2
    expect_failure(lambda: prepare(api, root), "phase=backup")
    assert meaningful(state)[-2:] == [("start", ident(1)), ("cmd", ident(1), "post")]
    assert state["containers"][ident(1)]["State"]["Running"]


def test_replacement_is_not_post_hooked(root):
    api, state = fake_api([container(definition=spec(stop=True, pre=[command("pre")],
                                                   post=[command("post")]))])

    def replace(method, path, body, state):
        if "/archive?" in path and not "sophon-exec-" in path:
            state["containers"][ident(1)]["Image"] = "sha256:replacement"

    state["hook"] = replace
    expect_failure(lambda: prepare(api, root), "restart failed")
    assert not any(event[0] == "start" for event in meaningful(state))
    assert not any(event[:3] == ("cmd", ident(1), "post") for event in meaningful(state))
    assert (root / "journal.json").exists()


def test_final_new_volume_and_membership_guard(root):
    for kind in ("volume", "member", "mount"):
        case = root / kind
        api, state = fake_api([container(definition=spec())])

        def change(method, path, body, state):
            if path.endswith("/containers/json?all=true") and state["listing_count"] == 1:
                if kind == "volume":
                    state["containers"][ident(2)] = container(2, running=False)
                elif kind == "member":
                    state["containers"][ident(2)] = container(2, spec(), volume="volume-1")
                else:
                    state["containers"][ident(1)]["Mounts"][0]["Name"] = "changed"

        state["hook"] = change
        expect_failure(lambda: prepare(api, case))
        assert not list(case.glob("runs/*/manifest.json"))


def test_fresh_runs_do_not_mutate_previous_source(root):
    api, state = fake_api([container(definition=spec())])
    first = Path(prepare(api, root)["overrideRoot"])
    before = {path.relative_to(first): path.read_bytes()
              for path in first.rglob("*") if path.is_file()}
    state["artifact"] = tar_bytes(b"second-backup")
    second = Path(prepare(api, root)["overrideRoot"])
    assert first != second
    assert all((first / relative).read_bytes() == data for relative, data in before.items())
    assert (first / "manifest.json").exists() and (second / "manifest.json").exists()


def test_empty_and_invalid_archives_fail(root):
    values = [b"", b"not-a-tar", tar_bytes(b""), tar_bytes(name="../escape"),
              tar_bytes(link="/etc/passwd"), tar_bytes(name="data/link", link="../../escape")]
    for index, data in enumerate(values):
        api, state = fake_api([container(definition=spec(stop=True))])
        state["artifact"] = data
        case = root / str(index)
        expect_failure(lambda: prepare(api, case))
        assert state["containers"][ident(1)]["State"]["Running"]
        assert not list(case.glob("runs/*/manifest.json"))


def test_timeout_cancels_exact_group_before_writers_resume(root):
    api, state = fake_api([
        container(1, spec(stop=True, pre=[command("pre")], post=[command("post")])),
        container(2, spec(backup=[command("dump")])),
    ])
    state["hang"].add("dump")
    expect_failure(lambda: prepare(api, root), "timed out")
    events = meaningful(state)
    assert events.index(("cancel", ident(2), "TERM")) < events.index(("start", ident(1)))
    assert not any(value["Running"] for value in state["execs"].values())
    assert not (root / "journal.json").exists()


def test_term_ignored_escalates_to_kill(root):
    api, state = fake_api([container(definition=spec(backup=[command("dump")]))])
    state["hang"].add("dump")
    state["term_ignored"] = True
    expect_failure(lambda: prepare(api, root))
    assert ("cancel", ident(1), "TERM") in meaningful(state)
    assert ("cancel", ident(1), "KILL") in meaningful(state)
    assert not (root / "journal.json").exists()


def test_missing_or_unsafe_pid_keeps_pause_and_journal(root):
    for kind in ("missing", "unsafe"):
        case = root / kind
        api, state = fake_api([
            container(1, spec(stop=True, pre=[command("pre")], post=[command("post")])),
            container(2, spec(backup=[command("dump")])),
        ])
        state["hang"].add("dump")
        state["pid_missing"] = kind == "missing"
        if kind == "unsafe":
            state["pid"] = b"1; kill -9 -1\n"
        expect_failure(lambda: prepare(api, case), "cancel failed")
        assert not state["containers"][ident(1)]["State"]["Running"]
        assert not any(event[0] == "cancel" for event in meaningful(state))
        journal = json.loads((case / "journal.json").read_text())
        assert journal["execs"] and journal["members"][ident(1)]["stop_pending"]
        unresolved = case / "runs" / journal["run"]
        assert unresolved.is_dir()
        assert list(unresolved.glob(f"artifacts/app/{ident(1)}/*.tar")) == []
        state["pid_missing"] = False
        state["pid"] = b"1234\n"
        state["hang"].clear()
        prepare(api, case)
        assert not (case / "journal.json").exists()
        assert state["containers"][ident(1)]["State"]["Running"]
        assert unresolved.is_dir(), "do not sweep staging from earlier invocations"


def test_durable_intent_before_stop_and_exec_start(root):
    api, state = fake_api([container(definition=spec(stop=True, pre=[command("pre")],
                                                   post=[command("post")]))])
    seen = []

    def check(method, path, body, state):
        if "/stop?" in path:
            journal = json.loads((root / "journal.json").read_text())
            assert journal["members"][ident(1)]["stop_pending"] is True
            seen.append("stop")
        if "/exec/" in path and path.endswith("/start"):
            execution = state["execs"][path.split("/")[-2]]
            if "Env" in execution["body"]:
                journal = json.loads((root / "journal.json").read_text())
                assert any(item["id"] == path.split("/")[-2] for item in journal["execs"])
                assert journal["members"][ident(1)]["pending_post"] is True
                seen.append("exec")

    state["hook"] = check
    prepare(api, root)
    assert seen == ["exec", "stop", "exec"]


def test_restart_failure_continues_other_members_and_recovers(root):
    definition = spec(stop=True, pre=[command("pre")], post=[command("post")])
    api, state = fake_api([container(1, definition), container(2, definition)])
    state["start_failures"].add(ident(1))
    error = expect_failure(lambda: prepare(api, root), "restart failed")
    assert "post deferred" in error
    assert state["containers"][ident(2)]["State"]["Running"]
    assert ("cmd", ident(2), "post") in meaningful(state)
    assert (root / "journal.json").exists()
    state["start_failures"].clear()
    state["events"].clear()
    prepare(api, root)
    events = meaningful(state)
    assert events[:2] == [("start", ident(1)), ("cmd", ident(1), "post")]
    assert not (root / "journal.json").exists()


def test_post_failure_continues_all_participants(root):
    first = spec(pre=[command("pre")], post=[command("post-fail"), command("post-second")])
    second = spec(pre=[command("pre")], post=[command("post-other")])
    api, state = fake_api([container(1, first), container(2, second)])
    state["command_fail"]["post-fail"] = 1
    expect_failure(lambda: prepare(api, root), "post failed")
    assert ("cmd", ident(1), "post-second") in meaningful(state)
    assert ("cmd", ident(2), "post-other") in meaningful(state)
    assert (root / "journal.json").exists()
    state["command_fail"].clear()
    prepare(api, root)
    assert not (root / "journal.json").exists()


def test_sigkill_style_journal_recovery_before_discovery(root):
    api, state = fake_api([
        container(1, spec(stop=True, pre=[command("pre")], post=[command("post")])),
        container(2, spec(backup=[command("dump")])),
        container(3, spec(stop=True), running=False),
    ])
    root = backup.private_dir(root)
    discovered = backup.inventory(api)
    members = discovered["members"]
    for member in members.values():
        member["stop_pending"] = member["id"] == ident(1)
        member["pending_post"] = member["id"] == ident(1)
    execution = {"member": ident(2), "id": ident(100), "phase": "backup", "index": 0,
                 "pidfile": "/tmp/sophon-exec-" + "a" * 32}
    state["execs"][ident(100)] = {
        "body": {"Env": ["SOPHON_PIDFILE=" + execution["pidfile"]]},
        "member": ident(2), "Running": True, "ExitCode": 0,
    }
    state["next_exec"] = 101
    state["containers"][ident(1)]["State"]["Running"] = False
    backup.save_journal(root, {
        "version": 1, "run": "interrupted", "backup_dir": "/tmp/sophon-backup-interrupted",
        "members": members, "execs": [execution],
    })
    state["events"].clear()
    prepare(api, root)
    events = state["events"]
    assert events.index(("cancel", ident(2), "TERM")) < events.index(("start", ident(1)))
    assert events.index(("cmd", ident(1), "post")) < events.index(("inventory",))
    assert not any(event[:2] == ("start", ident(3)) for event in events)
    assert not (root / "journal.json").exists()


def test_interruption_enters_finalization(root):
    api, state = fake_api([container(definition=spec(stop=True, pre=[command("pre")],
                                                   post=[command("post")]))])

    def interrupt(method, path, body, state):
        if "/archive?" in path:
            raise InterruptedError("SIGTERM")

    state["hook"] = interrupt
    expect_failure(lambda: prepare(api, root), "interrupted")
    assert meaningful(state)[-2:] == [("start", ident(1)), ("cmd", ident(1), "post")]
    assert not (root / "journal.json").exists()


def test_verify_hashes_extracts_and_returns_unexecuted_recipes(root):
    api, state = fake_api([container(definition=spec(
        restore=[{"argv": ["production-must-never-run"], "user": "postgres"}]))])
    snapshot = Path(prepare(api, root / "source")["overrideRoot"])
    events = list(state["events"])
    result = backup.verify(snapshot, root / "restore" / "verified", root / "restore")
    output = Path(result["output"])
    extracted = output / "app" / ident(1) / "data" / "data" / "file"
    assert extracted.read_bytes() == b"database-data"
    recipe = json.loads(Path(result["recipes"]).read_text())
    assert recipe["restore"][0]["commands"][0]["argv"] == ["production-must-never-run"]
    assert state["events"] == events
    expect_failure(lambda: backup.verify(snapshot, output, root / "restore"), "empty")
    expect_failure(lambda: backup.verify(snapshot, root / "outside", root / "restore"), "under")


def test_verify_checksum_traversal_links_and_snapshot_symlinks(root):
    api, state = fake_api([container(definition=spec())])
    snapshot = Path(prepare(api, root / "source")["overrideRoot"])
    manifest_path = snapshot / "manifest.json"
    original = json.loads(manifest_path.read_text())
    artifact_path = snapshot / original["artifacts"][0]["path"]
    original_tar = artifact_path.read_bytes()
    artifact_path.write_bytes(b"corrupted")
    expect_failure(lambda: backup.verify(
        snapshot, root / "restore" / "bad-hash", root / "restore"), "checksum")
    for index, malicious in enumerate((tar_bytes(name="../escaped"),
                                       tar_bytes(link="/etc/passwd"))):
        artifact_path.write_bytes(malicious)
        modified = copy.deepcopy(original)
        modified["artifacts"][0]["sha256"] = hashlib.sha256(malicious).hexdigest()
        modified["artifacts"][0]["bytes"] = len(malicious)
        backup.write_json(manifest_path, modified)
        expect_failure(lambda: backup.verify(
            snapshot, root / "restore" / f"bad-tar-{index}", root / "restore"))
    backup.write_json(manifest_path, original)
    artifact_path.unlink()
    target = root / "tar-copy"
    target.write_bytes(original_tar)
    artifact_path.symlink_to(target)
    expect_failure(lambda: backup.verify(
        snapshot, root / "restore" / "symlink", root / "restore"), "symlink")
    assert not list((root / "restore").glob("*/restore-recipes.json"))


def test_lock_rejects_concurrent_prepare(root):
    root = backup.private_dir(root)
    descriptor = os.open(root / ".collector.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        backup.fcntl.flock(descriptor, backup.fcntl.LOCK_EX)
        api, state = fake_api([container(definition=spec())])
        expect_failure(lambda: prepare(api, root), "another collector")
        assert not state["events"]
    finally:
        os.close(descriptor)


def test_cli_failure_stdout_and_secret_redaction(root):
    result = subprocess.run([sys.executable, str(Path(backup.__file__)), "prepare"],
                            env={**os.environ, "PORTAINER_URL": "http://invalid",
                                 "PORTAINER_API_KEY": "do-not-print-me",
                                 "SOURCE_ROOT": str(root)},
                            capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout == ""
    assert "do-not-print-me" not in result.stderr
    assert "HTTPS" in result.stderr


def test_nonfinite_timeouts_rejected_before_workload_access(root):
    api, state = fake_api([container(definition=spec())])
    for value in (0, -1, float("nan"), float("inf"), True):
        expect_failure(lambda: backup.prepare(api, root, command_timeout=value))
        expect_failure(lambda: backup.prepare(api, root, poll_interval=value))
        expect_failure(lambda: backup.portainer_api(
            "https://portainer.example", "secret", timeout=value))
    assert not state["events"]


def test_real_wrapper_tracks_group_and_cancellation_script(root):
    api, state = fake_api([container(definition=spec(backup=[command("dump")]))])
    prepare(api, root / "source")
    original = next(value["body"] for value in state["execs"].values() if "Env" in value["body"])
    cmd = original["Cmd"][:4] + [sys.executable, "-c", "import time; time.sleep(20)"]
    pidfile = root / "pid"
    process = subprocess.Popen(cmd, env={**os.environ, "SOPHON_PIDFILE": str(pidfile)},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    try:
        deadline = time.monotonic() + 3
        while not pidfile.exists():
            assert process.poll() is None, "wrapper exited before writing PID"
            assert time.monotonic() < deadline, "wrapper did not write PID"
            time.sleep(.01)
        pid = int(pidfile.read_text().strip())
        assert pid >= 2 and os.getpgid(pid) == pid
        assert pid != os.getpgid(os.getpid()), "refusing to signal test runner's group"
        assert process.poll() is None, "exec wrapper must wait for its group"
        assert pidfile.stat().st_mode & 0o777 == 0o600
        created = []

        def transport(method, path, body, stream):
            if path.endswith("/exec"):
                created.append(body)
                return {"Id": ident(999)}
            if path.endswith("/start"):
                return None
            if path.endswith("/json"):
                return {"Running": False, "ExitCode": 0}
            raise AssertionError(path)

        backup.signal_group({"endpoint": 1, "transport": transport},
                            ident(1), str(pidfile), pid, "TERM", 1, .001)
        cancellation = subprocess.run(created[0]["Cmd"], capture_output=True, timeout=3)
        assert cancellation.returncode == 0, cancellation.stderr
        assert process.wait(timeout=3) != 0
    finally:
        if process.poll() is None:
            if pid is not None and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=3)


def test_verify_rejects_incomplete_or_misleading_recipes(root):
    api, state = fake_api([container(definition=spec())])
    snapshot = Path(prepare(api, root / "source")["overrideRoot"])
    manifest_path = snapshot / "manifest.json"
    original = json.loads(manifest_path.read_text())
    for index, change in enumerate(("missing", "commands", "artifact-list", "owner")):
        modified = copy.deepcopy(original)
        if change == "missing":
            modified["restore"] = []
        elif change == "commands":
            modified["restore"][0]["commands"] = [command("wrong-command")]
        elif change == "artifact-list":
            modified["restore"][0]["artifacts"] = []
        else:
            modified["restore"][0]["member"] = ident(2)
        backup.write_json(manifest_path, modified)
        expect_failure(lambda: backup.verify(
            snapshot, root / "restore" / str(index), root / "restore"))


def test_v2_label_recipe_matches_v1(root):
    definition = spec(
        excludes={"/cache": "Reconstructible cache"},
        pre=[command("prepare")],
        backup=[{"argv": ["sh", "-ec", 'dump "$SOPHON_BACKUP_DIR"'], "user": "0"}],
        post=[command("cleanup")],
        restore=[command("restore")],
    )
    original = container(definition=definition)
    original["Mounts"].append({"Type": "volume", "Name": "cache", "Destination": "/cache"})
    migrated = copy.deepcopy(original)
    migrated["Config"]["Labels"] = {
        "sophon.backup.version": "2",
        "sophon.backup.group": "app",
        "sophon.backup.stop-during-backup": "false",
        "sophon.backup.stop-timeout": "10",
        "sophon.backup.covers.0.path": "/data",
        "sophon.backup.excludes.0.path": "/cache",
        "sophon.backup.excludes.0.reason": "Reconstructible cache",
        "sophon.backup.paths.0.type": "archive",
        "sophon.backup.paths.0.path": "/data",
        "sophon.backup.paths.0.name": "data",
        "sophon.backup.prepare.0.argv": '["prepare"]',
        "sophon.backup.archive-pre.0.argv": '["sh","-ec","dump \\"$SOPHON_BACKUP_DIR\\""]',
        "sophon.backup.archive-pre.0.user": "0",
        "sophon.backup.archive-post.0.argv": '["cleanup"]',
        "sophon.backup.restore.0.argv": '["restore"]',
    }
    assert backup.parse_spec(original) == definition
    assert backup.parse_spec(migrated) == definition


def test_v2_encoder_preserves_order_and_argument_boundaries(root):
    definition = spec(
        paths=[
            {"type": "archive", "path": "/data", "name": "data"},
            {"type": "portainer-backup", "name": "control"},
        ],
        backup=[
            {"argv": ["sh", "-ec", 'printf "%s" "$SOPHON_BACKUP_DIR"'], "user": "0"},
            {"argv": ["export", "argument with spaces", "line\nbreak", "${SOPHON_BACKUP_DIR}.sql"]},
        ],
    )
    labels = backup.encode_labels(definition)
    assert labels["sophon.backup.version"] == "2"
    assert backup.SPEC_LABEL not in labels
    assert labels["sophon.backup.paths.1.type"] == "portainer-backup"
    assert "sophon.backup.paths.1.path" not in labels
    assert labels["sophon.backup.archive-pre.0.user"] == "0"
    assert json.loads(labels["sophon.backup.archive-pre.1.argv"]) == definition["backup"][1]["argv"]
    migrated = container(definition=definition)
    migrated["Config"]["Labels"] = dict(reversed(list(labels.items())))
    assert backup.parse_spec(migrated) == definition


def test_v2_rejects_ambiguous_incomplete_and_unsafe_labels(root):
    original = container(definition=spec())
    valid = backup.encode_labels(spec())
    cases = [
        {**valid, backup.SPEC_LABEL: json.dumps(spec())},
        {**original["Config"]["Labels"], "sophon.backup.group": "app"},
        {key: value for key, value in valid.items() if key != backup.VERSION_LABEL},
        {key: value for key, value in valid.items() if key != "sophon.backup.group"},
        {**valid, "sophon.backup.stop-during-backup": "yes"},
        {**valid, "sophon.backup.stop-timeout": "0"},
        {**valid, "sophon.backup.stop-timeout": "1.5"},
        {**valid, "sophon.backup.stop-timeout": "01"},
        {**valid, "sophon.backup.stop-timeout": "9" * 5000},
        {**valid, "sophon.backup.group": "unsafe/group"},
        {**valid, "sophon.backup.typo": "value"},
        {**valid, "sophon.backup.paths.0.extra": "value"},
        {**valid, "sophon.backup.paths.01.name": "other"},
        {**valid, "sophon.backup.paths.2.name": "other"},
        {**valid, "sophon.backup.paths." + "9" * 5000 + ".name": "other"},
        {**valid, "sophon.backup.paths.0.path": "/data/../etc"},
        {**valid, "sophon.backup.covers.0.path": "/missing"},
        {**valid, "sophon.backup.covers.1.path": "/data"},
        {**valid, "sophon.backup.archive-pre.0.user": "0"},
        {**valid, "sophon.backup.archive-pre.0.argv": "not JSON"},
        {**valid, "sophon.backup.archive-pre.0.argv": '[""]'},
        {**valid, "sophon.backup.archive-pre.0.argv": '{"argv":["dump"]}'},
        {**valid, "sophon.backup.archive-pre.0.argv": '["dump"]',
         "sophon.backup.archive-pre.0.user": "0\nother"},
        {**valid, "sophon.backup.archive-pre.0.argv": '["dump"]',
         "sophon.backup.archive-pre.0.shell": "true"},
        {**valid, "sophon.backup.stop-during-backup": "true",
         "sophon.backup.archive-pre.0.argv": '["dump"]'},
        {**valid, "sophon.backup.excludes.0.path": "/data",
         "sophon.backup.excludes.0.reason": "duplicate",
         "sophon.backup.excludes.1.path": "/data",
         "sophon.backup.excludes.1.reason": "duplicate"},
        {**valid, "sophon.backup.excludes.0.path": "/data"},
        {**valid, "sophon.backup.group": 1},
    ]
    for labels in cases:
        current = copy.deepcopy(original)
        current["Config"]["Labels"] = labels
        expect_failure(lambda: backup.parse_spec(current))


def test_v2_capture_restore_preserves_group_order_and_stopped_state(root):
    writer = container(1, spec(
        stop=True, pre=[command("prepare")], post=[command("cleanup")],
        restore=[command("restore-writer")]))
    database = container(2, spec(
        backup=[command("dump")], post=[command("remove-dump")],
        restore=[command("restore-database")]))
    stopped = container(3, spec(group="stopped", stop=True), running=False)
    for current in (writer, database, stopped):
        current["Config"]["Labels"] = backup.encode_labels(backup.parse_spec(current))
    api, state = fake_api([writer, database, stopped])
    result = backup.prepare(api, root / "source")
    snapshot = Path(result["overrideRoot"])
    manifest = backup.read_json(snapshot / "manifest.json")
    assert all(c["Config"]["Labels"][backup.VERSION_LABEL] == "2" for c in manifest["containers"])
    events = state["events"]
    prepare = next(i for i, e in enumerate(events) if e[:3] == ("cmd", ident(1), "prepare"))
    stop = events.index(("stop", ident(1)))
    dump = next(i for i, e in enumerate(events) if e[:3] == ("cmd", ident(2), "dump"))
    archive = next(i for i, e in enumerate(events) if e[0] == "archive")
    start = events.index(("start", ident(1)))
    cleanup = next(i for i, e in enumerate(events) if e[:3] == ("cmd", ident(1), "cleanup"))
    assert prepare < stop < dump < archive < start < cleanup
    assert ("start", ident(3)) not in events
    assert state["containers"][ident(3)]["State"]["Running"] is False
    checked = backup.verify(snapshot, root / "restored", root)
    assert checked["artifacts"] == 3
    assert not (root / "source" / "journal.json").exists()


def run_tests():
    base = Path(__file__).parent / (".backup-tests-" + uuid.uuid4().hex)
    base.mkdir(mode=0o700)
    tests = sorted((name, value) for name, value in globals().items()
                   if name.startswith("test_") and callable(value))
    failures = []
    try:
        for name, test in tests:
            case = base / name
            case.mkdir(mode=0o700)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    test(case)
                print(f"PASS {name}")
            except BaseException:
                failures.append(name)
                print(f"FAIL {name}")
                traceback.print_exc()
        print(f"{len(tests) - len(failures)}/{len(tests)} tests passed")
        return int(bool(failures))
    finally:
        shutil.rmtree(base)


if __name__ == "__main__":
    sys.exit(run_tests())
