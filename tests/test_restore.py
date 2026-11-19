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
        identity = source / "app/writer/path-identity"
        identity.write_text("original-generated-identity")
        identity.chmod(0o400)
        os.chown(identity, 1001, 1002)
        target = {
            "Id": "a" * 64, "Name": "/writer", "Image": "sha256:" + "b" * 64,
            "Config": {"Image": "fixture:1", "Env": ["SECRET=NEVER_SAVE_ENV"], "Labels": {
                "sophon.backup.enable": "true", "sophon.backup.group": "app",
                "sophon.backup.paths": "/data/identity"}},
            "State": {"Running": True},
            "Mounts": [{"Type": "volume", "Name": "fresh-data", "Destination": "/data", "RW": True}],
        }
        manifest = {"version": 3, "sophon_version": "fixture", "failures": [], "groups": [{
            "name": "app", "members": [{"name": "writer", "image": "fixture:1", "image_id": target["Image"]}],
            "paths": [{"name": "path-identity", "member": "writer", "volume": "old-data", "path": "identity",
                       "optional": False, "present": True, "snapshot_path": "app/writer/path-identity"}], "dumps": []}]}
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
            (source / "app/writer/path-identity").rename(source / "app/writer/path-nested-identity")
            target["Config"]["Labels"]["sophon.backup.paths"] = "/data/nested/identity"
            manifest["groups"][0]["paths"][0].update(
                name="path-nested-identity", path="nested/identity", snapshot_path="app/writer/path-nested-identity")
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
            (source / "app/writer/path-identity").write_text("corrupted-after-plan")
            with self.assertRaisesRegex(RuntimeError, "changed after planning"):
                restore.apply({"transport": lambda *args: calls.append(args)}, root, True, "e" * 64, "restore-state")
        self.assertEqual(calls, [])

    def test_whole_volume_plus_ini_merge_preserves_fresh_db_password_and_skips_kopiaignore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            vol_snap = snapshot / "gitea/gitea/volume-data"
            (vol_snap / "git/lfs/objects").mkdir(parents=True)
            (vol_snap / "git/lfs/objects/oid1").write_text("lfs-content")
            (vol_snap / ".kopiaignore").write_text("/git/lfs/tmp/\n/gitea/conf/app.ini\n")
            ini_snap = snapshot / "gitea/gitea/ini-app.ini"
            ini_snap.write_text("[security]\nSECRET_KEY = restored-secret\n[server]\nLFS_JWT_SECRET = restored-lfs-jwt\n")

            target = {
                "Id": "a" * 64, "Name": "/gitea", "Image": "sha256:" + "b" * 64,
                "Config": {"Image": "gitea:1", "Env": [], "Labels": {
                    "sophon.backup.enable": "true",
                    "sophon.backup.group": "gitea",
                    "sophon.backup.volumes": "/data",
                    "sophon.backup.exclude": "git/lfs/tmp/",
                    "sophon.backup.ini": "/data/gitea/conf/app.ini:security.SECRET_KEY,server.LFS_JWT_SECRET",
                }},
                "State": {"Running": True},
                "Mounts": [{"Type": "volume", "Name": "gitea_gitea_data", "Destination": "/data", "RW": True}],
            }
            manifest = {"version": 3, "sophon_version": "fixture", "failures": [], "groups": [{
                "name": "gitea",
                "members": [{"name": "gitea", "image": "gitea:1", "image_id": target["Image"]}],
                "paths": [
                    {"name": "volume-data", "member": "gitea", "volume": "old_gitea_data", "path": ".",
                     "optional": False, "present": True, "snapshot_path": "gitea/gitea/volume-data"},
                    {"name": "ini-app.ini", "member": "gitea", "volume": "old_gitea_data",
                     "path": "gitea/conf/app.ini", "optional": False, "present": True,
                     "ini": ["security.SECRET_KEY", "server.LFS_JWT_SECRET"],
                     "snapshot_path": "gitea/gitea/ini-app.ini"},
                ],
                "dumps": [],
            }]}
            (snapshot / "manifest.json").write_text(json.dumps(manifest))

            fresh_vol = root / "fresh-gitea-vol"
            (fresh_vol / "gitea/conf").mkdir(parents=True)
            (fresh_vol / "gitea/conf/app.ini").write_text(
                "[database]\nPASSWD = fresh-rotated-db-password\n[security]\nSECRET_KEY = temporary-fresh-secret\n")
            (fresh_vol / "stale-file").write_text("remove-me")

            plan = restore.make_plan(snapshot, [target], "gitea")
            restore.replace_paths(plan, snapshot, {"gitea_gitea_data": str(fresh_vol)})

            self.assertFalse((fresh_vol / ".kopiaignore").exists())
            self.assertFalse((fresh_vol / "stale-file").exists())
            self.assertEqual((fresh_vol / "git/lfs/objects/oid1").read_text(), "lfs-content")
            merged_ini = (fresh_vol / "gitea/conf/app.ini").read_text()
            self.assertIn("fresh-rotated-db-password", merged_ini)
            self.assertIn("restored-secret", merged_ini)
            self.assertIn("restored-lfs-jwt", merged_ini)

    def test_postgres_custom_plan_and_compose_peer_stop_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            (snapshot / "keycloak/keycloak-db").mkdir(parents=True)
            dump_file = snapshot / "keycloak/keycloak-db/app.dump"
            dump_bytes = b"PGDMP-custom-archive"
            dump_file.write_bytes(dump_bytes)
            sha = hashlib.sha256(dump_bytes).hexdigest()

            db_target = {
                "Id": "1" * 64, "Name": "/keycloak-db", "Image": "sha256:" + "2" * 64,
                "Config": {
                    "Image": "postgres:18",
                    "Env": ["POSTGRES_USER=keycloak", "POSTGRES_DB=keycloak", "PGDATA=/var/lib/postgresql/18/docker"],
                    "Labels": {
                        "com.docker.compose.project": "keycloak",
                        "sophon.backup.enable": "true",
                        "sophon.backup.group": "keycloak",
                        "sophon.backup.dump": 'exec pg_dump -Fc -Z0 -U "$POSTGRES_USER" "$POSTGRES_DB"',
                        "sophon.backup.dump.name": "app.dump",
                        "sophon.backup.dump.restore": "postgres-custom",
                    },
                },
                "State": {"Running": True},
                "Mounts": [{"Type": "volume", "Name": "keycloak_db_data", "Destination": "/var/lib/postgresql", "RW": True}],
            }
            peer_app = {
                "Id": "3" * 64, "Name": "/keycloak", "Image": "sha256:" + "4" * 64,
                "Config": {"Image": "keycloak:26", "Env": [], "Labels": {"com.docker.compose.project": "keycloak"}},
                "State": {"Running": True},
                "Mounts": [],
            }
            manifest = {"version": 3, "sophon_version": "fixture", "failures": [], "groups": [{
                "name": "keycloak",
                "members": [{"name": "keycloak-db", "image": "postgres:18", "image_id": db_target["Image"]}],
                "paths": [],
                "dumps": [{"name": "app.dump", "member": "keycloak-db", "sha256": sha,
                           "bytes": len(dump_bytes), "snapshot_path": "keycloak/keycloak-db/app.dump"}],
            }]}
            (snapshot / "manifest.json").write_text(json.dumps(manifest))
            plan = restore.make_plan(snapshot, [db_target, peer_app], "keycloak")
            self.assertEqual([p["name"] for p in plan["peers"]], ["keycloak"])
            self.assertEqual(plan["dumps"][0]["restore"], "postgres-custom")

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
            second["Config"]["Labels"] = {"com.docker.compose.project": "app"}
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

    def test_inspect_snapshot_returns_ordered_stateful_groups_and_omits_reconstructible_groups(self):
        from unittest.mock import patch
        snap_id = "d" * 32
        manifest = {
            "version": 3,
            "failures": [],
            "groups": [
                {"name": "arcane", "paths": [], "dumps": []},
                {"name": "gitea", "paths": [{"name": "repos"}], "dumps": [{"name": "db"}]},
                {"name": "coredns", "paths": [], "dumps": []},
                {"name": "traefik", "paths": [{"name": "acme"}], "dumps": []},
                {"name": "keycloak", "paths": [], "dumps": [{"name": "db"}]},
                {"name": "ldap", "paths": [], "dumps": [{"name": "ldif"}]},
            ],
        }

        def fake_kopia(env, *args, **kwargs):
            if args[:2] == ("snapshot", "list"):
                return json.dumps([{"id": snap_id, "endTime": "2026-11-17T16:34:23Z",
                                    "rootEntry": {"obj": "kroot"}}]).encode()
            if args == ("show", "kroot"):
                return json.dumps({"entries": [{"name": "manifest.json", "obj": "kman"}]}).encode()
            if args == ("show", "kman"):
                return json.dumps(manifest).encode()
            raise AssertionError(args)

        with patch.object(restore.runtime, "connect"), patch.object(restore.runtime, "kopia", side_effect=fake_kopia):
            summary = restore.inspect_snapshot(
                {"KOPIA_CLIENT_USERNAME": "backup", "KOPIA_CLIENT_HOSTNAME": "sophon"}, "latest")
        self.assertEqual(summary["snapshot"], snap_id)
        self.assertEqual([g["name"] for g in summary["groups"]], ["traefik", "ldap", "keycloak", "gitea"])


if __name__ == "__main__":
    unittest.main()
