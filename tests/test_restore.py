import hashlib
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

FILES = Path(__file__).resolve().parents[1] / "roles/kopia/files"
sys.path.insert(0, str(FILES))
import restore


class RestoreTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "snapshot"
        (source / "app/writer").mkdir(parents=True)
        identity = source / "app/writer/identity"
        identity.write_text("original-generated-identity")
        identity.chmod(0o400)
        os.chown(identity, 1001, 1002)
        target = {
            "Id": "a" * 64, "Name": "/writer", "Image": "sha256:" + "b" * 64,
            "Config": {"Image": "fixture:1", "Env": ["SECRET=NEVER_SAVE_ENV"], "Labels": {
                "sophon.backup.version": "3", "sophon.backup.group": "app", "sophon.backup.stop": "true",
                "sophon.backup.paths.0.name": "identity", "sophon.backup.paths.0.volume": "fresh-data",
                "sophon.backup.paths.0.path": "identity"}},
            "State": {"Running": True},
            "Mounts": [{"Type": "volume", "Name": "fresh-data", "Destination": "/data", "RW": True}],
        }
        manifest = {"version": 3, "sophon_version": "fixture", "failures": [], "groups": [{
            "name": "app", "members": [{"name": "writer", "image": "fixture:1", "image_id": target["Image"]}],
            "paths": [{"name": "identity", "member": "writer", "volume": "old-data", "path": "identity",
                       "optional": False, "present": True, "snapshot_path": "app/writer/identity"}], "dumps": []}]}
        (source / "manifest.json").write_text(json.dumps(manifest))
        return source, target, manifest

    def test_plan_is_bound_to_current_labels_and_file_replacement_preserves_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target, _ = self.fixture(root)
            plan = restore.make_plan(source, [target], "app")
            self.assertNotIn("NEVER_SAVE_ENV", json.dumps(plan))
            destination = root / "fresh"
            destination.mkdir()
            (destination / "identity").write_text("new-generated-identity")
            restore.replace_paths(plan, source, {"fresh-data": str(destination)})
            attributes = (destination / "identity").stat()
            self.assertEqual((attributes.st_uid, attributes.st_gid, attributes.st_mode & 0o777), (1001, 1002, 0o400))
            self.assertEqual((destination / "identity").read_text(), "original-generated-identity")

    def test_plan_rejects_failed_group_changed_image_and_unsafe_snapshot_path(self):
        for case in ("failed", "image", "path"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, target, manifest = self.fixture(root)
                if case == "failed":
                    manifest["failures"] = [{"group": "app", "error": "failed export"}]
                elif case == "image":
                    target["Image"] = "sha256:" + "c" * 64
                else:
                    manifest["groups"][0]["paths"][0]["snapshot_path"] = "../../outside"
                (source / "manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(RuntimeError):
                    restore.make_plan(source, [target], "app")

    def test_apply_requires_approval_before_any_engine_mutation(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(RuntimeError, "approval"):
            restore.apply({"transport": lambda *args: calls.append(args)}, Path(temporary), approve=False)
        self.assertEqual(calls, [])

    def test_destination_parent_symlink_is_refused_without_touching_external_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target, manifest = self.fixture(root)
            target["Config"]["Labels"]["sophon.backup.paths.0.path"] = "nested/identity"
            manifest["groups"][0]["paths"][0]["path"] = "nested/identity"
            (source / "manifest.json").write_text(json.dumps(manifest))
            plan = restore.make_plan(source, [target], "app")
            destination, outside = root / "fresh", root / "outside"
            destination.mkdir()
            outside.mkdir()
            (outside / "identity").write_text("do-not-change")
            (destination / "nested").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                restore.replace_paths(plan, source, {"fresh-data": str(destination)})
            self.assertEqual((outside / "identity").read_text(), "do-not-change")

    def test_apply_refuses_changed_staging_before_engine_access(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target, _ = self.fixture(root)
            state = {"version": 3, "owner": "f" * 32, "phase": "planned",
                     "resources": [], "plan": restore.make_plan(source, [target], "app")}
            restore.save(root, state)
            (source / "app/writer/identity").write_text("corrupted-after-plan")
            with self.assertRaisesRegex(RuntimeError, "changed after planning"):
                restore.apply({"transport": lambda *args: calls.append(args)}, root, True, "e" * 64, "restore-state")
        self.assertEqual(calls, [])

    def test_failed_start_stops_the_whole_group_and_retry_settles_missing_helpers(self):
        self.failed_start_scenario()

    def test_failed_journal_write_does_not_prevent_stopping_a_partially_started_group(self):
        self.failed_start_scenario(storage_failure=True)

    def failed_start_scenario(self, storage_failure=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "work"
            root.mkdir()
            source, target, manifest = self.fixture(root)
            second = copy.deepcopy(target)
            second.update(Id="c" * 64, Name="/zsecond", Mounts=[])
            second["Config"]["Labels"] = {"sophon.backup.version": "3", "sophon.backup.group": "app",
                                         "sophon.backup.stop": "true"}
            manifest["groups"][0]["members"].append(
                {"name": "zsecond", "image": "fixture:1", "image_id": second["Image"]})
            (source / "manifest.json").write_text(json.dumps(manifest))
            state = {"version": 3, "owner": "f" * 32, "phase": "planned", "approved": False,
                     "resources": [{"name": "sophon-restore-pending", "id": None, "image": "e" * 64, "binds": []}],
                     "plan": restore.make_plan(source, [target, second], "app")}
            restore.save(root, state)
            members = {value["Id"]: value for value in (target, second)}
            helpers = {}
            fail_start = True

            def transport(method, path, body, stream):
                nonlocal fail_start
                if path == "/containers/json?all=true":
                    return [{"Id": key} for key in members]
                if path.startswith("/containers/create"):
                    identifier = "d" * 64
                    helpers[identifier] = {
                        "Id": identifier, "Image": body["Image"], "Config": body,
                        "HostConfig": body["HostConfig"], "State": {"Running": False, "ExitCode": 0},
                        "Mounts": [{"Type": "volume", "Name": bind.split(":")[0],
                                    "Destination": bind.split(":")[1], "RW": not bind.endswith(":ro")}
                                   for bind in body["HostConfig"]["Binds"]]}
                    return {"Id": identifier}
                identifier = path.split("/")[2].split("?", 1)[0]
                value = members.get(identifier, helpers.get(identifier))
                if value is None:
                    raise restore.backup.EngineError(method, path, 404)
                if method == "GET":
                    return copy.deepcopy(value)
                if method == "DELETE":
                    del helpers[identifier]
                elif path.endswith("/start") and identifier in members:
                    if identifier == second["Id"] and fail_start:
                        fail_start = False
                        if storage_failure:
                            root.rename(root.with_name("unavailable"))
                        raise RuntimeError("injected failed start")
                    value["State"]["Running"] = True
                elif "/stop?" in path:
                    value["State"]["Running"] = False

            api = {"transport": transport}
            with self.assertRaisesRegex(RuntimeError, "injected failed start"):
                restore.apply(api, root, True, "e" * 64, "restore-state")
            self.assertFalse(any(value["State"]["Running"] for value in members.values()))
            if storage_failure:
                return
            self.assertEqual(restore.state_file(root)["phase"], "failed")
            result = restore.apply(api, root, True, "e" * 64, "restore-state")
            self.assertEqual(result["phase"], "complete")
            self.assertTrue(all(value["State"]["Running"] for value in members.values()))
            self.assertEqual(helpers, {})


if __name__ == "__main__":
    unittest.main()
