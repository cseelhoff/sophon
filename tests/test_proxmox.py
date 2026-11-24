"""Proxmox ISO upload helper: python3 tests/test_proxmox.py (stdlib only)."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proxmox_upload", ROOT / "roles/proxmox/files/upload.py")
UPLOAD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(UPLOAD)
cpu_spec = importlib.util.spec_from_file_location("proxmox_cpu", ROOT / "roles/proxmox/files/cpu.py")
CPU = importlib.util.module_from_spec(cpu_spec)
cpu_spec.loader.exec_module(CPU)


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

    def test_cluster_cpu_selection_prefers_host_when_homogeneous_and_highest_baseline_when_mixed(self):
        v2_aes = "cx16 lahf_lm popcnt sse4_1 sse4_2 ssse3 aes"
        v3 = f"{v2_aes} avx avx2 bmi1 bmi2 fma abm movbe xsave"
        v4 = f"{v3} avx512f avx512bw avx512cd avx512dq avx512vl"
        xeon_5215 = {"model": "Intel(R) Xeon(R) Gold 5215 CPU @ 2.50GHz", "flags": v4}
        epyc_zen3 = {"model": "AMD EPYC 7443P 24-Core Processor", "flags": v3}
        epyc_zen4 = {"model": "AMD EPYC 9354 32-Core Processor", "flags": v4.replace("abm", "lzcnt")}
        westmere = {"model": "Intel(R) Xeon(R) CPU E5620", "flags": v2_aes}

        # Single node or homogeneous multi-node cluster -> native 'host'
        self.assertEqual(CPU.select_cpu_type([xeon_5215]), "host")
        self.assertEqual(CPU.select_cpu_type([xeon_5215, dict(xeon_5215), dict(xeon_5215)]), "host")

        # Heterogeneous clusters -> highest common live-migratable microarchitecture level
        self.assertEqual(CPU.select_cpu_type([xeon_5215, epyc_zen4]), "x86-64-v4")
        self.assertEqual(CPU.select_cpu_type([xeon_5215, epyc_zen3]), "x86-64-v3")
        self.assertEqual(CPU.select_cpu_type([xeon_5215, westmere]), "x86-64-v2-AES")
        self.assertEqual(CPU.select_cpu_type([]), "x86-64-v2-AES")

        # Explicit SOPHON_PROXMOX_CPU_TYPE override wins; invalid strings rejected
        self.assertEqual(CPU.select_cpu_type([xeon_5215], override="x86-64-v3"), "x86-64-v3")
        with self.assertRaises(ValueError):
            CPU.select_cpu_type([xeon_5215], override="host; evil")


if __name__ == "__main__":
    unittest.main()
