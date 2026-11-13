#!/usr/bin/env python3
"""V3 collector checks at the prepare/run and engine transport surfaces."""
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roles/kopia/files"))
import backup


def participant(number, name, group, stop=False, paths=(), dumps=(), volume=None):
    labels = {"sophon.backup.version": "3", "sophon.backup.group": group,
              "sophon.backup.stop": str(stop).lower()}
    for collection, entries in (("paths", paths), ("dumps", dumps)):
        for index, record in enumerate(entries):
            for key, value in record.items():
                labels[f"sophon.backup.{collection}.{index}.{key}"] = (
                    json.dumps(value) if key == "argv" else value)
    return {"Id": f"{number:064x}", "Name": "/" + name, "Image": "sha256:" + f"{number:064x}",
            "Config": {"Image": "fixture:1", "Env": ["SECRET=NEVER_CAPTURE_ENV"], "Labels": labels},
            "State": {"Running": True, "ExitCode": 0},
            "Mounts": [{"Type": "volume", "Name": volume, "Destination": "/data", "RW": True}] if volume else []}


class Engine:
    def __init__(self, containers, source=None):
        self.containers = {c["Id"]: c for c in containers}
        self.events = []
        self.execs = {}
        self.source = source
        self.dump_exit = 0
        self.start_error = False
        self.stop_error_once = False
        self.stop_calls = 0
        self.dump_stdout = b"-- native fixture SQL\nCREATE TABLE fixture(id integer);\n"

    def __call__(self, method, path, body, stream):
        self.events.append((method, path))
        parts = path.split("?")[0].strip("/").split("/")
        if path.startswith("/containers/json"):
            return [{"Id": key} for key in self.containers]
        if parts[0] == "containers":
            current = self.containers[parts[1]]
            if parts[2] == "json":
                return current
            if parts[2] == "stop":
                self.stop_calls += 1
                if self.stop_error_once:
                    self.stop_error_once = False
                    raise OSError("fixture stop response lost")
                current["State"].update(Running=False, ExitCode=0)
                return None
            if parts[2] == "start":
                if self.start_error:
                    raise RuntimeError("fixture restart failed")
                if self.source:
                    self.source()
                current["State"]["Running"] = True
                return None
            if parts[2] == "exec":
                execution = f"{len(self.execs) + 1:064x}"
                self.execs[execution] = body
                return {"Id": execution}
        if parts[0] == "exec":
            if parts[2] == "json":
                return {"Running": False, "ExitCode": self.dump_exit}
            sql = self.dump_stdout
            data = struct.pack(">BxxxI", 1, len(sql)) + sql
            output = io.BytesIO(data)
            return {"read": lambda size: output.read(min(size, 3)), "close": output.close}
        raise AssertionError((method, path, body))


class CollectorTests(unittest.TestCase):
    def test_prepare_clones_before_restart_and_never_captures_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "volume"
            source.mkdir()
            (source / "identity").write_text("application-generated-key")
            (source / "app.ini").write_text(
                "APP_NAME = configured\n[database]\nPASSWD = DO_NOT_BACK_UP_CONFIG\n"
                "[security]\nSECRET_KEY = application-generated-secret\n")
            writer = participant(1, "writer", "app", True,
                                 paths=[{"name": "identity", "volume": "app-data", "path": "identity"},
                                        {"name": "identity.ini", "volume": "app-data", "path": "app.ini",
                                         "ini": '["security.SECRET_KEY"]'}],
                                 volume="app-data")
            database = participant(2, "database", "app", dumps=[
                {"name": "database.sql", "argv": ["pg_dumpall", "--clean"]}], volume="raw-db")
            api = Engine([writer, database])
            api.source = lambda: self.assertTrue(list((root / "stage").rglob("identity")))
            result = backup.prepare({"transport": api}, {"app-data": str(source)}, root / "stage", "fixture-version")
            manifest = json.loads((Path(result["path"]) / "manifest.json").read_text())
            self.assertEqual(manifest["version"], 3)
            self.assertEqual(manifest["sophon_version"], "fixture-version")
            self.assertEqual(manifest["failures"], [])
            self.assertEqual(manifest["undeclared_volumes"], ["raw-db"])
            self.assertNotIn("NEVER_CAPTURE_ENV", json.dumps(manifest))
            self.assertFalse(any("archive" in event[1] or "api/backup" in event[1] for event in api.events))
            self.assertTrue(writer["State"]["Running"])
            self.assertFalse(list(Path(result["path"]).rglob("*.tar")))
            self.assertEqual(next(Path(result["path"]).rglob("identity")).read_text(), "application-generated-key")
            self.assertIn("CREATE TABLE", next(Path(result["path"]).rglob("database.sql")).read_text())
            identity = next(Path(result["path"]).rglob("identity.ini")).read_text()
            self.assertIn("application-generated-secret", identity)
            self.assertNotIn("DO_NOT_BACK_UP_CONFIG", identity)

    def test_missing_volume_fails_only_that_group_and_reports_how_to_add_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            bad = participant(1, "external", "external", True, paths=[
                {"name": "data", "volume": "missing", "path": "state"}], volume="missing")
            good = participant(2, "database", "good", dumps=[
                {"name": "dump.sql", "argv": ["pg_dumpall"]}])
            api = Engine([bad, good])
            result = backup.prepare({"transport": api}, {}, Path(temporary), "fixture-version")
            manifest = json.loads((Path(result["path"]) / "manifest.json").read_text())
            self.assertEqual(manifest["failures"][0]["group"], "external")
            self.assertIn("SOPHON_KOPIA_EXTRA_VOLUMES", manifest["failures"][0]["error"])
            self.assertTrue(list(Path(result["path"]).rglob("dump.sql")))
            self.assertTrue(bad["State"]["Running"])

    def test_retired_labels_are_rejected_without_decoding_a_legacy_recipe(self):
        container = participant(1, "old", "old")
        container["Config"]["Labels"] = {"sophon.backup.version": "1", "sophon.backup.spec": "{}"}
        with self.assertRaisesRegex(RuntimeError, "version 3"):
            backup.parse_spec(container)

    def test_failed_native_dump_does_not_publish_group_and_resumes_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = participant(1, "writer", "app", True)
            database = participant(2, "database", "app", dumps=[
                {"name": "database.sql", "argv": ["pg_dumpall"]}])
            api = Engine([writer, database])
            api.dump_exit = 7
            result = backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
            self.assertEqual(result["manifest"]["groups"], [])
            self.assertIn("exit 7", result["manifest"]["failures"][0]["error"])
            self.assertTrue(writer["State"]["Running"])
            self.assertFalse((Path(temporary) / "journal.json").exists())

    def test_pending_restart_is_recovered_before_the_next_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = participant(1, "writer", "app", True)
            api = Engine([writer])
            api.start_error = True
            backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
            self.assertFalse(writer["State"]["Running"])
            self.assertTrue((Path(temporary) / "journal.json").exists())
            api.start_error = False
            with backup.locked(temporary) as root:
                backup.recover({"transport": api}, root)
            self.assertTrue(writer["State"]["Running"])
            self.assertFalse((Path(temporary) / "journal.json").exists())

    def test_partial_run_uploads_successful_groups_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            bad = participant(1, "missing", "bad", True, paths=[
                {"name": "state", "volume": "missing", "path": "state"}], volume="missing")
            good = participant(2, "database", "good", dumps=[
                {"name": "dump.sql", "argv": ["pg_dumpall"]}])
            api = Engine([bad, good])
            environment = {"ENGINE_URL": "http://engine:2375", "BACKUP_VOLUMES": "{}",
                           "SOURCE_ROOT": temporary, "SOPHON_VERSION": "fixture",
                           "KOPIA_CLIENT_USERNAME": "backup", "KOPIA_CLIENT_HOSTNAME": "dev"}
            completed = __import__("subprocess").CompletedProcess(
                [], 0, json.dumps({"id": "a" * 32, "rootEntry": {"summ": {"numFailed": 0}}}), "")
            with patch.object(backup, "engine_api", return_value={"transport": api}), \
                    patch.object(backup.subprocess, "run", return_value=completed) as upload:
                self.assertEqual(backup.run(environment), 1)
            self.assertIn("--override-source=backup@dev:/sophon-state", upload.call_args.args[0])
            report = json.loads((Path(temporary) / "last-run.json").read_text())
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["snapshot"]["id"], "a" * 32)
            self.assertEqual(report["upload_status"], "complete")
            self.assertEqual([group["name"] for group in report["groups"]], ["good"])

    def test_recovery_refuses_a_replaced_writer_and_preserves_pending_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = participant(1, "writer", "app", True)
            api = Engine([writer])
            api.start_error = True
            backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
            api.start_error = False
            writer["Image"] = "sha256:" + "f" * 64
            with backup.locked(temporary) as root, self.assertRaisesRegex(RuntimeError, "recovery incomplete"):
                backup.recover({"transport": api}, root)
            self.assertFalse(writer["State"]["Running"])
            self.assertTrue((Path(temporary) / "journal.json").exists())

    def test_lost_stop_response_is_settled_before_clearing_recovery_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = participant(1, "writer", "app", True)
            api = Engine([writer])
            api.stop_error_once = True
            result = backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
            self.assertEqual(len(result["manifest"]["failures"]), 1)
            self.assertEqual(api.stop_calls, 2)
            self.assertTrue(writer["State"]["Running"])
            self.assertFalse((Path(temporary) / "journal.json").exists())

    def test_empty_native_export_is_not_accepted_as_successful_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = participant(1, "database", "app", dumps=[
                {"name": "database.sql", "argv": ["pg_dumpall"]}])
            api = Engine([database])
            api.dump_stdout = b""
            result = backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
            self.assertEqual(result["manifest"]["groups"], [])
            self.assertIn("empty stdout", result["manifest"]["failures"][0]["error"])

    def test_failed_intent_persistence_never_stops_a_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = participant(1, "writer", "app", True)
            api = Engine([writer])
            replace = backup.os.replace
            failed = []

            def unavailable(source, target):
                if Path(target).name == "journal.json" and not failed:
                    failed.append(True)
                    raise OSError("fixture durable write unavailable")
                return replace(source, target)

            with patch.object(backup.os, "replace", side_effect=unavailable):
                result = backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
            self.assertEqual(api.stop_calls, 0)
            self.assertTrue(writer["State"]["Running"])
            self.assertIn("durable write unavailable", result["manifest"]["failures"][0]["error"])

if __name__ == "__main__":
    unittest.main()
