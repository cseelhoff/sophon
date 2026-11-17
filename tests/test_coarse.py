import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "roles/kopia/files"
sys.path.insert(0, str(FILES))
import coarse


class CoarseTests(unittest.TestCase):
    def test_opt_in_defaults_and_rejects_overlapping_or_root_host_paths(self):
        from test_console import service_values
        import deployment
        values = service_values() | {"SOPHON_KOPIA_COARSE_ENABLED": "true"}
        validated, error = deployment.validate_settings(values, {}, "configure")
        self.assertIsNone(error)
        self.assertEqual(validated["SOPHON_KOPIA_COARSE_VOLUMES_DIR"],
                         "/var/home/core/.local/share/containers/storage/volumes")
        self.assertEqual(validated["SOPHON_KOPIA_COARSE_WORK_DIR"], "/var/home/core/sophon-coarse")
        custom = values | {"SOPHON_KOPIA_COARSE_VOLUMES_DIR": "/srv/podman/volumes",
                           "SOPHON_KOPIA_COARSE_WORK_DIR": "/srv/coarse"}
        self.assertIsNone(deployment.validate_settings(custom, {}, "configure")[1])
        for path in ("/", "//", "/srv", "/srv/podman/volumes", "/srv/podman/volumes/stage", "/srv/../coarse"):
            with self.subTest(path=path):
                self.assertIsNotNone(deployment.validate_settings(
                    custom, {"SOPHON_KOPIA_COARSE_WORK_DIR": path}, "configure")[1])

    def test_zero_downtime_btrfs_snapshot_creates_readonly_staging_without_container_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volumes, work, source = (root / name for name in ("volumes", "work", "source"))
            for path in (volumes, work, source):
                path.mkdir()
            (volumes / "gitea_data").mkdir()
            (volumes / "gitea_data/state").write_text("live irreproducible state")

            def fake_snapshot(src, parent, name="volumes"):
                self.assertEqual(Path(src), volumes)
                shutil.copytree(src, Path(parent) / name)

            with patch.object(coarse, "fs_magic", return_value=coarse.BTRFS_SUPER_MAGIC), \
                    patch.object(coarse, "is_btrfs_subvolume", return_value=True), \
                    patch.object(coarse, "create_readonly_snapshot", side_effect=fake_snapshot) as snap, \
                    patch.object(coarse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                coarse.capture(volumes, work, source, "fixture-source")

            snap.assert_called_once_with(volumes, work / "staging", "volumes")
            self.assertEqual((work / "staging/volumes/gitea_data/state").read_text(), "live irreproducible state")
            manifest = json.loads((work / "staging/manifest.json").read_text())
            self.assertEqual(manifest["format"], "sophon-coarse-2")
            self.assertEqual(manifest["mode"], "btrfs-readonly-subvolume")
            self.assertTrue(manifest["last_resort_only"])
            self.assertEqual(json.loads((work / "pending.json").read_text()),
                             {"version": 2, "run": manifest["run"]})
            self.assertFalse((work / "journal.json").exists())
            self.assertFalse((source / "coarse.json").exists())

    def test_non_btrfs_or_non_subvolume_source_is_refused_with_no_plain_copy_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volumes, work, source = (root / name for name in ("volumes", "work", "source"))
            for path in (volumes, work, source):
                path.mkdir()
            with patch.object(coarse, "fs_magic", return_value=0x58465342), \
                    patch.object(coarse, "is_btrfs_subvolume", return_value=False), \
                    patch.object(coarse, "create_readonly_snapshot") as snap:
                with self.assertRaisesRegex(RuntimeError, "no plain-copy fallback"):
                    coarse.capture(volumes, work, source, "fixture")
            snap.assert_not_called()
            self.assertFalse((work / "pending.json").exists())
            self.assertFalse((work / "staging").exists())

    def test_active_operator_restore_defers_coarse_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volumes, work, source = (root / name for name in ("volumes", "work", "source"))
            for path in (volumes, work, source):
                path.mkdir()
            (source / "restore.json").write_text('{"version":3}')
            with self.assertRaisesRegex(RuntimeError, "restoration"):
                coarse.capture(volumes, work, source, "fixture")
            self.assertFalse((work / "staging").exists())

    def test_snapshot_failure_cleans_staging_and_leaves_no_pending_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volumes, work, source = (root / name for name in ("volumes", "work", "source"))
            for path in (volumes, work, source):
                path.mkdir()
            with patch.object(coarse, "fs_magic", return_value=coarse.BTRFS_SUPER_MAGIC), \
                    patch.object(coarse, "is_btrfs_subvolume", return_value=True), \
                    patch.object(coarse.fcntl, "ioctl", side_effect=PermissionError(13, "Permission denied")):
                with self.assertRaisesRegex(RuntimeError, "no plain-copy fallback"):
                    coarse.capture(volumes, work, source, "fixture")
            self.assertFalse((work / "pending.json").exists())
            self.assertFalse((work / "staging").exists())

    def test_serve_initializes_empty_work_dir_before_schedule_and_leaves_nonempty_dir_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volumes, work, source = (root / name for name in ("volumes", "work", "source"))
            for path in (volumes, work, source):
                path.mkdir()
            env = {"COARSE_VOLUMES": str(volumes), "COARSE_ROOT": str(work), "SOURCE_ROOT": str(source)}
            called = []

            def fake_run(environment):
                coarse.initialize(Path(environment["COARSE_ROOT"]))
                called.append(True)

            with patch.dict(coarse.os.environ, env, clear=False), \
                    patch.object(coarse, "validate_mounts"), \
                    patch.object(coarse, "run", side_effect=fake_run), \
                    patch.object(coarse.time, "sleep", side_effect=KeyboardInterrupt), \
                    patch.object(sys, "argv", ["coarse.py", "serve"]):
                self.assertEqual(coarse.main(), 130)
            self.assertEqual(called, [True])
            self.assertTrue((work / ".sophon-coarse.json").exists())
            self.assertTrue((work / "schedule.json").exists())
            self.assertFalse((work / "last-run.json").exists())

            nonempty = root / "nonempty"
            nonempty.mkdir()
            (nonempty / "existing.txt").write_text("keep")
            bad_env = env | {"COARSE_ROOT": str(nonempty)}
            with patch.dict(coarse.os.environ, bad_env, clear=False), \
                    patch.object(coarse, "validate_mounts"), \
                    patch.object(sys, "argv", ["coarse.py", "serve"]):
                self.assertEqual(coarse.main(), 1)
            self.assertEqual(sorted(p.name for p in nonempty.iterdir()), ["existing.txt"])

    def test_distinct_btrfs_subvolume_anonymous_st_dev_is_accepted_on_shared_superblock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volumes, work, source = (root / name for name in ("volumes", "work", "source"))
            for path in (volumes, work, source):
                path.mkdir()
            orig_stat = Path.stat

            def fake_stat(self_path, *args, **kwargs):
                st = orig_stat(self_path, *args, **kwargs)
                if self_path == volumes:
                    return os.stat_result((st.st_mode, 256, 61, st.st_nlink, st.st_uid, st.st_gid,
                                           st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
                if self_path == work:
                    return os.stat_result((st.st_mode, 538, 41, st.st_nlink, st.st_uid, st.st_gid,
                                           st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
                return st

            mountinfo = (
                f"1299 1308 0:49 /home/core/.local/share/containers/storage/volumes {volumes} ro,noatime - btrfs /dev/vda5 rw,subvolid=256\n"
                f"1300 1308 0:49 /home/core/sophon-coarse {work} rw,noatime - btrfs /dev/vda5 rw,subvolid=5\n"
            )
            orig_read_text = Path.read_text

            def fake_read_text(self_path, *args, **kwargs):
                if str(self_path) == "/proc/self/mountinfo":
                    return mountinfo
                return orig_read_text(self_path, *args, **kwargs)

            with patch.object(Path, "read_text", fake_read_text):
                coarse.validate_mounts(volumes, work)
            overlap_mountinfo = (
                f"1299 1308 0:49 /home/core/sophon-coarse/volumes {volumes} ro,noatime - btrfs /dev/vda5 rw,subvolid=256\n"
                f"1300 1308 0:49 /home/core/sophon-coarse {work} rw,noatime - btrfs /dev/vda5 rw,subvolid=5\n"
            )
            with patch.object(Path, "read_text", lambda p, *a, **k: overlap_mountinfo if str(p) == "/proc/self/mountinfo" else orig_read_text(p, *a, **k)):
                with self.assertRaisesRegex(RuntimeError, "overlap"):
                    coarse.validate_mounts(volumes, work)

            import os
            with patch.object(Path, "stat", fake_stat), \
                    patch.object(coarse, "fs_magic", return_value=coarse.BTRFS_SUPER_MAGIC), \
                    patch.object(coarse, "create_readonly_snapshot", side_effect=lambda s, p, n="volumes": (Path(p) / n).mkdir()), \
                    patch.object(coarse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                coarse.capture(volumes, work, source, "fixture-subvol-dev")
            self.assertTrue((work / "pending.json").exists())


if __name__ == "__main__":
    unittest.main()
