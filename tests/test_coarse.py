import copy
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
    def test_opt_in_rejects_overlapping_or_root_host_paths(self):
        from test_console import service_values
        import deployment
        values = service_values() | {"SOPHON_KOPIA_COARSE_ENABLED": "true",
                                     "SOPHON_KOPIA_COARSE_VOLUMES_DIR": "/srv/podman/volumes",
                                     "SOPHON_KOPIA_COARSE_WORK_DIR": "/srv/coarse"}
        self.assertIsNone(deployment.validate_settings(values, {}, "configure")[1])
        for path in ("/", "//", "/srv", "/srv/podman/volumes", "/srv/podman/volumes/stage", "/srv/../coarse"):
            with self.subTest(path=path):
                self.assertIsNotNone(deployment.validate_settings(
                    values, {"SOPHON_KOPIA_COARSE_WORK_DIR": path}, "configure")[1])

    def fixture(self, root):
        volumes, work, source = (root / name for name in ("volumes", "work", "source"))
        for path in (volumes, work, source):
            path.mkdir()
        (volumes / "state").write_text("irreproducible state")
        rows = {str(number) * 64: {
            "Id": str(number) * 64, "Names": [name], "State": state, "ExitCode": 0,
            "IsInfra": False, "Image": "fixture:1", "ImageID": "a" * 64, "Labels": labels,
            "Env": ["MUST_NOT_CAPTURE_ENV=secret"]} for number, name, state, labels in (
                (1, "worker", "running", {coarse.CONTROL: "worker"}),
                (2, "proxy", "running", {coarse.CONTROL: "proxy"}),
                (3, "application", "running", {}), (4, "database", "running", {}),
                (5, "intentionally-stopped", "exited", {}), (6, "pod-infrastructure", "running", {}))}
        rows["6" * 64]["IsInfra"] = True
        events = []

        def transport(method, path, body, stream):
            if method == "GET":
                self.assertEqual(path, "/libpod/containers/json?all=true")
                return copy.deepcopy(list(rows.values()))
            key = path.split("/")[2]
            events.append((method, path))
            if key not in rows:
                raise coarse.backup.EngineError(method, path, 404)
            rows[key]["State"] = "exited" if "/stop?" in path else "running"

        def command(argv, **kwargs):
            if argv[0] == "cp":
                self.assertEqual(rows["3" * 64]["State"], "exited")
                self.assertEqual(rows["4" * 64]["State"], "exited")
                self.assertIn("--reflink=always", argv)
                self.assertTrue((source / "coarse.json").exists())
                shutil.copytree(volumes, Path(argv[-1]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        return volumes, work, source, rows, events, {"transport": transport}, command

    def test_cold_capture_resumes_only_original_running_set_and_keeps_real_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            volumes, work, source, rows, events, api, command = self.fixture(Path(temporary))
            with patch.object(coarse.subprocess, "run", side_effect=command):
                coarse.capture(api, volumes, work, source, "fixture-source")
            self.assertEqual((work / "staging/volumes/state").read_text(), "irreproducible state")
            self.assertNotIn("MUST_NOT_CAPTURE_ENV", (work / "staging/manifest.json").read_text())
            self.assertEqual(rows["3" * 64]["State"], "running")
            self.assertEqual(rows["4" * 64]["State"], "running")
            self.assertEqual(rows["5" * 64]["State"], "exited")
            self.assertEqual(rows["6" * 64]["State"], "running")
            self.assertFalse((source / "coarse.json").exists())
            self.assertFalse((work / "journal.json").exists())
            self.assertTrue((work / "pending.json").exists())
            self.assertEqual(len(events), 4)

    def test_reflink_failure_has_no_plain_copy_fallback_or_ready_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            volumes, work, source, rows, events, api, _ = self.fixture(Path(temporary))
            with patch.object(coarse.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, "", "Operation not supported")) as commands:
                with self.assertRaisesRegex(RuntimeError, "no plain-copy fallback"):
                    coarse.capture(api, volumes, work, source, "fixture")
            self.assertEqual(commands.call_count, 1)
            self.assertEqual(rows["3" * 64]["State"], "running")
            self.assertEqual(rows["4" * 64]["State"], "running")
            self.assertFalse((work / "pending.json").exists())
            self.assertFalse((work / "staging").exists())

    def test_lost_stop_response_is_settled_before_resuming_all_original_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            volumes, work, source, rows, events, api, _ = self.fixture(Path(temporary))
            original = api["transport"]
            failed = False

            def transport(method, path, body, stream):
                nonlocal failed
                result = original(method, path, body, stream)
                if method == "POST" and "/stop?" in path and not failed:
                    failed = True
                    raise OSError("stop response lost")
                return result
            api["transport"] = transport
            with self.assertRaisesRegex(OSError, "stop response lost"):
                coarse.capture(api, volumes, work, source, "fixture")
            self.assertEqual(rows["3" * 64]["State"], "running")
            self.assertEqual(rows["4" * 64]["State"], "running")
            self.assertEqual([path for _, path in events if "/stop?" in path].count(
                "/containers/" + "3" * 64 + "/stop?t=120"), 2)

    def test_deleted_container_is_not_replaced_by_name_and_other_restarts_continue(self):
        with tempfile.TemporaryDirectory() as temporary:
            volumes, work, source, rows, events, api, _ = self.fixture(Path(temporary))
            def command(argv, **kwargs):
                del rows["3" * 64]
                return subprocess.CompletedProcess(argv, 1, "", "clone failure")
            with patch.object(coarse.subprocess, "run", side_effect=command):
                with self.assertRaisesRegex(RuntimeError, "container was deleted"):
                    coarse.capture(api, volumes, work, source, "fixture")
            self.assertEqual(rows["4" * 64]["State"], "running")
            self.assertEqual(json.loads((work / "journal.json").read_text())["remaining"], ["3" * 64])
            self.assertTrue((source / "coarse.json").exists())


if __name__ == "__main__":
    unittest.main()
