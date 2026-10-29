"""Proxmox ISO upload helper: python3 tests/test_proxmox.py (stdlib only)."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proxmox_upload", ROOT / "roles/proxmox/files/upload.py")
UPLOAD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(UPLOAD)


class UploadTests(unittest.TestCase):
    def test_upload_uses_stdin_validates_json_and_upid(self):
        config = {
            "url": "https://pve:8006/api2/json/nodes/pve/storage/local/upload",
            "ticket": "TICKET-secret", "csrf": "CSRF-secret",
            "path": '/home/operator/image space;"quoted".iso',
            "filename": "sophon.iso", "validate_certs": True,
        }
        with patch.object(UPLOAD.subprocess, "run") as run:
            run.return_value.stdout = '{"data":"UPID:pve:001:upload:"}'
            self.assertEqual(UPLOAD.upload(config), "UPID:pve:001:upload:")
            args, kwargs = run.call_args
            self.assertNotIn(config["ticket"], repr(args))
            self.assertNotIn(config["csrf"], repr(args))
            self.assertIn("--config", args[0])
            self.assertIn("Cookie: PVEAuthCookie=TICKET-secret", kwargs["input"])
            self.assertIn("CSRFPreventionToken: CSRF-secret", kwargs["input"])
            self.assertNotIn("\ninsecure", kwargs["input"])
            self.assertTrue(kwargs["check"])
            for invalid in ("<html>failure</html>", '{"data":null}', '{"data":"not-upid"}', "[]"):
                run.return_value.stdout = invalid
                with self.assertRaises(ValueError):
                    UPLOAD.upload(config)
            run.return_value.stdout = '{"data":"UPID:pve:001:upload:"}'
            UPLOAD.upload(dict(config, validate_certs=False))
            self.assertIn("\ninsecure\n", run.call_args.kwargs["input"])
        for invalid in ("bad\npath", "bad\rpath", "bad\0path"):
            with self.assertRaises(ValueError):
                UPLOAD.upload(dict(config, path=invalid))
        with self.assertRaises(ValueError):
            UPLOAD.upload(dict(config, filename="../escape.iso"))


class VmTasksTests(unittest.TestCase):
    def test_every_api_module_call_honours_proxmox_validate_certs(self):
        text = (ROOT / "roles/proxmox/tasks/vm.yml").read_text()
        self.assertEqual(text.count("community.proxmox.proxmox_kvm:"), 2)
        self.assertEqual(text.count('validate_certs: "{{ proxmox_validate_certs }}"'), 4)


if __name__ == "__main__":
    unittest.main()
