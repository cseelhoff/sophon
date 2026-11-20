"""Unit tests for sophon-mount-secrets, sophon-refresh-certs, and sophon-control-sync cert detection."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src'), str(ROOT / 'tests')]

import certificates
import mount_secrets
import secrets_iso
from test_secrets_iso import make_test_acme


class MountSecretsAndGuestRefreshTests(unittest.TestCase):
    def test_verify_secrets_iso_and_local_iso_deletion_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            acme_bytes, ca_pem = make_test_acme('sophon.example.com', days=89)
            ca_file = tmp_dir / 'ca.pem'
            ca_file.write_bytes(ca_pem)

            good_iso = tmp_dir / 'sophon-secrets.iso'
            certificates.write_certificate_iso(acme_bytes, good_iso)

            no_acme_iso = tmp_dir / 'no-acme.iso'
            secrets_iso.write_private_iso(
                {'sophon-seed.json': (b'{"env":{"SOPHON_DOMAIN":"sophon.example.com"}}', 0o600)},
                'SOPHON_CERTS',
                no_acme_iso,
            )

            old_env = os.environ.copy()
            try:
                os.environ['SSL_CERT_FILE'] = str(ca_file)
                os.environ['SOPHON_HYPERVISOR'] = 'proxmox'
                os.environ['SOPHON_DOMAIN'] = 'sophon.example.com'

                domain, expires_at = mount_secrets.verify_secrets_iso(good_iso)
                self.assertEqual(domain, 'sophon.example.com')
                self.assertIn('T', expires_at)

                with self.assertRaisesRegex(ValueError, 'does not contain /acme.json'):
                    mount_secrets.verify_secrets_iso(no_acme_iso)

                # Refuses backup VM target
                self.assertEqual(
                    mount_secrets.main(['--vm-name', 'sophon-backup', '--secrets-iso', str(good_iso)]),
                    2,
                )

                # If Ansible mount fails, local ISO is preserved so operator doesn't lose newly issued cert
                with mock.patch('mount_secrets.run_mount_playbook', return_value=1):
                    rc_fail = mount_secrets.main(['--secrets-iso', str(good_iso)])
                self.assertEqual(rc_fail, 1)
                self.assertTrue(good_iso.is_file())

                # With --keep-local, local ISO stays on disk
                with mock.patch('mount_secrets.run_mount_playbook', return_value=0):
                    rc_keep = mount_secrets.main(['--keep-local', '--secrets-iso', str(good_iso)])
                self.assertEqual(rc_keep, 0)
                self.assertTrue(good_iso.is_file())

                # Default run removes local ISO after successful hypervisor attach
                with mock.patch('mount_secrets.run_mount_playbook', return_value=0):
                    rc_ok = mount_secrets.main(['--secrets-iso', str(good_iso)])
                self.assertEqual(rc_ok, 0)
                self.assertFalse(good_iso.exists())
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_guest_control_sync_and_refresh_certs_rotate_snapshot_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            old_acme, ca_old = make_test_acme('sophon.example.com', days=35)
            new_acme, ca_new = make_test_acme('sophon.example.com', days=89)
            ca_file = tmp_dir / 'ca.pem'
            ca_file.write_bytes(ca_old + ca_new)

            project_dir = tmp_dir / 'project'
            (project_dir / 'artifacts/traefik').mkdir(parents=True)
            (project_dir / 'imports').mkdir(parents=True)
            (project_dir / 'deploy-result.json').write_text(
                json.dumps({'domain': 'sophon.example.com', 'mode': 'production_active', 'infravm_ip': '10.0.60.101'}),
                encoding='utf-8',
            )
            (project_dir / 'artifacts/traefik/acme.json').write_bytes(old_acme)
            (project_dir / 'imports/acme.json').write_bytes(new_acme)

            control_dir = tmp_dir / 'control'
            control_dir.mkdir()

            cert_checker = tmp_dir / 'cert-check.sh'
            cert_checker.write_text(
                f'#!/usr/bin/env bash\nexec "{sys.executable}" "{ROOT / "src/certificates.py"}" check --source "$1" --domain "$2"\n',
                encoding='utf-8',
            )
            cert_checker.chmod(0o755)

            env = {
                **os.environ,
                'SSL_CERT_FILE': str(ca_file),
                'SOPHON_PROJECT_DIR': str(project_dir),
                'SOPHON_CONTROL_DIR': str(control_dir),
                'SOPHON_CERTS_DEVICE': str(tmp_dir / 'nonexistent-cdrom'),
                'SOPHON_CERT_CHECK_CMD': str(cert_checker),
                'SOPHON_ALLOW_NON_CORE': '1',
            }

            # 1) sophon-control-sync reads real JSON output from src/certificates.py check
            sync_res = subprocess.run(
                ['bash', str(ROOT / 'guest/sophon-control-sync'), '--probe'],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(sync_res.returncode, 0, sync_res.stdout + sync_res.stderr)
            state = json.loads((control_dir / 'appliance-state.json').read_text(encoding='utf-8'))
            self.assertTrue(state['live_cert_expires_at'])
            self.assertTrue(state['cert_disk']['present'])
            self.assertTrue(state['cert_disk']['valid'])
            self.assertTrue(state['cert_disk']['expires_at'])
            self.assertNotEqual(state['live_cert_expires_at'], state['cert_disk']['expires_at'])

            # 2) Stub podman & verify sophon-refresh-certs swaps Traefik acme.json, restarts certs-dumper + ldap, and runs Kopia backup
            traefik_live = tmp_dir / 'traefik-container-acme.json'
            traefik_live.write_bytes(old_acme)
            calls_log = tmp_dir / 'podman-calls.jsonl'
            podman_stub = tmp_dir / 'podman'
            podman_stub.write_text(
                f'#!{sys.executable}\n'
                'import json, os, shutil, sys\n'
                'from pathlib import Path\n'
                'args = sys.argv[1:]\n'
                'assert args[0] == "--remote=false"\n'
                'args = args[1:]\n'
                f'with open({str(calls_log)!r}, "a", encoding="utf-8") as fh:\n'
                '    fh.write(json.dumps(args) + "\\n")\n'
                f'live = Path({str(traefik_live)!r})\n'
                'if args[:2] == ["cp", "traefik:/data/acme.json"]:\n'
                '    shutil.copyfile(live, args[2])\n'
                'elif args[0] == "cp" and args[2] == "traefik:/data/acme.json":\n'
                '    shutil.copyfile(args[1], live)\n'
                'elif args == ["start", "traefik"] and os.environ.get("FAIL_TRAEFIK_START") == "1":\n'
                '    os.environ["FAIL_TRAEFIK_START"] = "0"\n'
                '    sys.exit(1)\n'
                'sys.exit(0)\n',
                encoding='utf-8',
            )
            podman_stub.chmod(0o755)
            env['SOPHON_PODMAN_BIN'] = str(podman_stub)

            # First verify rollback when Traefik fails to start with the new acme.json
            fail_res = subprocess.run(
                ['bash', str(ROOT / 'guest/sophon-refresh-certs')],
                env={**env, 'FAIL_TRAEFIK_START': '1'},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(fail_res.returncode, 0)
            self.assertEqual(traefik_live.read_bytes(), old_acme)
            self.assertTrue((project_dir / 'imports/acme.json').is_file())

            # Now run normal active-production refresh -> rotates cert, snapshots Kopia, cleans staged import
            calls_log.unlink(missing_ok=True)
            ok_res = subprocess.run(
                ['bash', str(ROOT / 'guest/sophon-refresh-certs')],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(ok_res.returncode, 0, ok_res.stdout + ok_res.stderr)
            self.assertEqual(traefik_live.read_bytes(), new_acme)
            self.assertEqual((project_dir / 'artifacts/traefik/acme.json').read_bytes(), new_acme)
            self.assertFalse((project_dir / 'imports/acme.json').exists())
            self.assertIn('Certificate update complete: wildcard TLS certificate for sophon.example.com valid until', ok_res.stdout)

            recorded = [json.loads(line) for line in calls_log.read_text(encoding='utf-8').splitlines()]
            self.assertIn(['restart', 'certs-dumper'], recorded)
            self.assertIn(['restart', 'ldap'], recorded)
            self.assertIn(['exec', 'kopia', 'python3', '/app/backup.py', 'run'], recorded)

            # In Safe Standby mode (standby.json present), Kopia write snapshot is skipped
            (project_dir / 'standby.json').write_text(json.dumps({'domain': 'sophon.example.com'}), encoding='utf-8')
            (project_dir / 'imports/acme.json').write_bytes(new_acme)
            calls_log.unlink(missing_ok=True)
            sb_res = subprocess.run(
                ['bash', str(ROOT / 'guest/sophon-refresh-certs')],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(sb_res.returncode, 0, sb_res.stdout + sb_res.stderr)
            recorded_sb = [json.loads(line) for line in calls_log.read_text(encoding='utf-8').splitlines()]
            self.assertNotIn(['exec', 'kopia', 'python3', '/app/backup.py', 'run'], recorded_sb)


if __name__ == '__main__':
    unittest.main()
