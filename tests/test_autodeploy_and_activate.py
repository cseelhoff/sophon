import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

import activate
import backup_destination
import console
import deployment
from library import hyperv_vm, vcenter_vm


def valid_configure_env() -> dict[str, str]:
    pw = 'SophonStrongPassword123!'
    return {
        'SOPHON_DEPLOYMENT_STEP': 'configure',
        'SOPHON_DOMAIN': 'example.com',
        'SOPHON_INFRAVM_IP': '10.0.60.101',
        'SOPHON_VNET_DNS': '10.0.60.1',
        'SOPHON_CF_TOKEN': 'cf_live_token_value',
        'SOPHON_CF_TUNNEL_ENABLED': 'true',
        'SOPHON_CF_TUNNEL_NAME': 'sophon',
        'SOPHON_ACME_EMAIL': 'admin@example.com',
        'SOPHON_KEYCLOAK_ADMIN_PASSWORD': pw,
        'SOPHON_KEYCLOAK_DB_PASSWORD': pw,
        'SOPHON_KEYCLOAK_ARCANE_CLIENT_SECRET': pw,
        'SOPHON_GITEA_ADMIN_PASSWORD': pw,
        'SOPHON_GITEA_DB_PASSWORD': pw,
        'SOPHON_OPENLDAP_ADMIN_PASSWORD': pw,
        'SOPHON_OPENLDAP_EXAMPLE_USER_PASSWORD': pw,
        'SOPHON_OPENLDAP_EXAMPLE_ADMIN_PASSWORD': pw,
        'SOPHON_ARCANE_ADMIN_PASSWORD': pw,
        'SOPHON_KOPIA_PASSWORD': pw,
        'SOPHON_KOPIA_SERVER_PASSWORD': pw,
        'SOPHON_KOPIA_SFTP_HOST': '10.0.60.100',
        'SOPHON_KOPIA_SFTP_PORT': '22',
        'SOPHON_KOPIA_SFTP_USERNAME': 'sophon-backup',
        'SOPHON_KOPIA_SFTP_PATH': '/repository',
        'SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64': base64.b64encode(b'fake-key').decode(),
        'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64': base64.b64encode(b'10.0.60.100 ssh-ed25519 AAAA\n').decode(),
    }


class AutoDeployAndActivateTests(unittest.TestCase):
    def test_alpine_autoseed_shell_syntax_and_noop_without_iso(self):
        script_path = ROOT / 'guest/backup-appliance/sophon-backup-autoseed'
        res = subprocess.run(['sh', '-n', str(script_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_backup_destination_auto_distinguishes_day1_and_standby_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(work_root / 'id_ed25519')],
                check=True, capture_output=True,
            )
            subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(work_root / 'host_ed25519')],
                check=True, capture_output=True,
            )
            key_b64 = base64.b64encode((work_root / 'id_ed25519').read_bytes()).decode()
            host_pub = ' '.join((work_root / 'host_ed25519.pub').read_text().strip().split()[:2])
            kh_line = f'192.0.2.100 {host_pub}\n'
            kh_b64 = base64.b64encode(kh_line.encode()).decode()

            base_vals = {
                'SOPHON_KOPIA_SFTP_HOST': '192.0.2.100',
                'SOPHON_KOPIA_SFTP_PORT': '22',
                'SOPHON_KOPIA_SFTP_USERNAME': 'sophon-backup',
                'SOPHON_KOPIA_SFTP_PATH': '/repository',
                'SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64': key_b64,
                'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64': kh_b64,
                'SOPHON_KOPIA_PASSWORD': 'KopiaRepoPassword123!',
            }

            class FakeSFTP:
                def __init__(self):
                    self.files = {}
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def get_channel(self):
                    return mock.MagicMock()
                def stat(self, p):
                    s = mock.MagicMock()
                    s.st_mode = 0o040755
                    return s
                def listdir(self, p): return []
                def open(self, p, mode='r'):
                    store = self.files
                    class Buf:
                        def __enter__(self_b): return self_b
                        def __exit__(self_b, *a): pass
                        def write(self_b, data): store[p] = data
                        def read(self_b): return store.get(p, b'')
                    return Buf()
                def remove(self, p):
                    self.files.pop(p, None)

            class FakeSSH:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def load_host_keys(self, p): pass
                def connect(self, *a, **kw): pass
                def open_sftp(self): return FakeSFTP()

            # Case 1: Existing repository with snapshots -> standby_restore without write probe
            def run_existing(args, **kw):
                cmd = ' '.join(args)
                if 'ssh-keyscan' in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=kh_line, stderr='')
                if args[0] == 'sftp':
                    return subprocess.CompletedProcess(args, 0, stdout='Size Used Avail (root) %Capacity\n1000000 100 999900 999900 1%\n', stderr='')
                if 'repository connect' in cmd:
                    self.assertIn('--readonly', args)
                    return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
                if 'snapshot list' in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout='[{"id":"snap1"}]', stderr='')
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

            ro_vals = dict(base_vals, SOPHON_KOPIA_SFTP_USERNAME='sophon-restore')
            with mock.patch.object(backup_destination.paramiko, 'SSHClient', FakeSSH), \
                 mock.patch.object(backup_destination.subprocess, 'run', side_effect=run_existing):
                out_ro = backup_destination.prepare(ro_vals, root=work_root, auto=True)
            self.assertEqual(out_ro['_SOPHON_AUTO_MODE'], 'standby_restore')
            self.assertEqual(out_ro['SOPHON_KOPIA_BACKUPS_ENABLED'], 'false')

            # Case 2: Empty repository + sophon-backup -> Day-1 repository create
            created = []
            connected = {'done': False}

            def run_empty(args, **kw):
                cmd = ' '.join(args)
                if 'ssh-keyscan' in cmd:
                    return subprocess.CompletedProcess(args, 0, stdout=kh_line, stderr='')
                if args[0] == 'sftp':
                    return subprocess.CompletedProcess(args, 0, stdout='Size Used Avail (root) %Capacity\n1000000 100 999900 999900 1%\n', stderr='')
                if 'repository connect' in cmd:
                    if connected['done']:
                        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
                    return subprocess.CompletedProcess(args, 1, stdout='', stderr=backup_destination.NOT_INITIALIZED)
                if 'repository create' in cmd:
                    created.append(args)
                    connected['done'] = True
                    return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

            with mock.patch.object(backup_destination.paramiko, 'SSHClient', FakeSSH), \
                 mock.patch.object(backup_destination.subprocess, 'run', side_effect=run_empty):
                out_day1 = backup_destination.prepare(dict(base_vals), root=work_root, auto=True)
            self.assertEqual(out_day1['_SOPHON_AUTO_MODE'], 'day1')
            self.assertEqual(len(created), 1)
            connected['done'] = False
            # Empty repository + read-only sophon-restore must fail closed!
            with mock.patch.object(backup_destination.paramiko, 'SSHClient', FakeSSH), \
                 mock.patch.object(backup_destination.subprocess, 'run', side_effect=run_empty):
                with self.assertRaisesRegex(ValueError, 'read-only SFTP account'):
                    backup_destination.prepare(dict(ro_vals), root=work_root, auto=True)

    def test_console_auto_day1_vs_standby_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            seed_file = work_root / 'sophon-seed.json'
            seed_env = valid_configure_env()
            seed_file.write_text(json.dumps({'version': 1, 'env': seed_env}))

            def fake_prepare_standby(values, *, root, confirm=None, readonly=False, auto=False):
                self.assertTrue(auto)
                out = dict(values)
                out['_SOPHON_AUTO_MODE'] = 'standby_restore'
                return out

            captured_env = {}

            def fake_deploy_main(args):
                captured_env.update(os.environ)
                return 0

            old_env = os.environ.copy()
            try:
                for k in list(os.environ):
                    if k.startswith('SOPHON_'):
                        os.environ.pop(k, None)
                os.environ['SOPHON_DEPLOYMENT_STEP'] = 'configure'
                (work_root / 'site.json').write_text(json.dumps({'SOPHON_INFRAVM_IP': '10.0.60.101', 'SOPHON_VNET_DNS': '10.0.60.1'}))
                with mock.patch.object(deployment, 'ROOT', work_root), \
                     mock.patch.object(backup_destination, 'prepare', side_effect=fake_prepare_standby), \
                     mock.patch.object(deployment, 'main', side_effect=fake_deploy_main):
                    rc = console.main(['--auto', '--seed-file', str(seed_file)])
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(rc, 0)
            # Seed file must be deleted immediately on startup!
            self.assertFalse(seed_file.exists())
            # Standby restore must postpone Cloudflare tunnel & Kopia scheduled backups while recording desired prod settings.
            self.assertEqual(captured_env.get('SOPHON_PREPARE_RESTORE'), 'true')
            self.assertEqual(captured_env.get('SOPHON_CF_TUNNEL_ENABLED'), 'false')
            self.assertEqual(captured_env.get('SOPHON_CF_TOKEN'), '')
            self.assertEqual(captured_env.get('SOPHON_KOPIA_BACKUPS_ENABLED'), 'false')

            standby = json.loads((work_root / 'standby.json').read_text())
            self.assertTrue(standby['desired_cf_tunnel_enabled'])
            self.assertTrue(standby['desired_backups_enabled'])
            deploy_result = json.loads((work_root / 'deploy-result.json').read_text())
            self.assertEqual(deploy_result['mode'], 'standby_restore')

    def test_activate_promotes_standby_and_refuses_readonly_sftp_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            site_path = work_root / 'site.json'
            site_path.write_text(json.dumps({'SOPHON_INFRAVM_IP': '10.0.60.101', 'SOPHON_VNET_DNS': '10.0.60.1'}))
            (work_root / 'standby.json').write_text(json.dumps({
                'domain': 'example.com',
                'desired_cf_tunnel_enabled': False,
                'desired_backups_enabled': True,
                'desired_snapshot_interval': '6h',
            }))

            ro_kopia_env = json.dumps([
                'KOPIA_PASSWORD=secret',
                'KOPIA_SERVER_PASSWORD=secret',
                'KOPIA_SFTP_HOST=10.0.60.100',
                'KOPIA_SFTP_USERNAME=sophon-restore',
                'KOPIA_SFTP_PATH=/repository',
                'KOPIA_SFTP_PRIVATE_KEY_B64=ZmFrZQ==',
                'KOPIA_SFTP_KNOWN_HOSTS_B64=ZmFrZQ==',
            ])
            old_env = os.environ.copy()
            try:
                os.environ['SOPHON_DEPLOYMENT_STEP'] = 'configure'
                os.environ['SOPHON_ACTIVATE_KOPIA_ENV_JSON'] = ro_kopia_env
                with mock.patch.object(deployment, 'ROOT', work_root):
                    rc_ro = activate.main(['--yes'])
                self.assertEqual(rc_ro, 2)

                rw_kopia_env = ro_kopia_env.replace('sophon-restore', 'sophon-backup')
                os.environ['SOPHON_ACTIVATE_KOPIA_ENV_JSON'] = rw_kopia_env
                called_playbooks = []
                with mock.patch.object(deployment, 'ROOT', work_root), \
                     mock.patch.object(backup_destination, 'prepare', side_effect=lambda v, **kw: v), \
                     mock.patch.object(deployment, 'main', side_effect=lambda a: (called_playbooks.append(a), 0)[1]):
                    rc_rw = activate.main(['--yes'])
                self.assertEqual(rc_rw, 0)
                self.assertEqual(called_playbooks, [['--step', 'configure', '--', '5-activate.yml']])
                self.assertFalse((work_root / 'standby.json').exists())

                # When standby.json includes the paired sophon-backup key (from a dev seed ISO),
                # activate promotes sophon-restore -> sophon-backup automatically!
                (work_root / 'standby.json').write_text(json.dumps({
                    'domain': 'example.com',
                    'desired_cf_tunnel_enabled': False,
                    'desired_backups_enabled': True,
                    'desired_snapshot_interval': '6h',
                    'desired_sftp_username': 'sophon-backup',
                    'desired_sftp_private_key_b64': 'cndsZXk=',
                }))
                os.environ['SOPHON_ACTIVATE_KOPIA_ENV_JSON'] = ro_kopia_env
                captured_prepare = {}

                def capture_prepare(vals, **kw):
                    captured_prepare.update(vals)
                    return vals

                with mock.patch.object(deployment, 'ROOT', work_root), \
                     mock.patch.object(backup_destination, 'prepare', side_effect=capture_prepare), \
                     mock.patch.object(deployment, 'main', return_value=0):
                    self.assertEqual(activate.main(['--yes']), 0)
                self.assertEqual(captured_prepare['SOPHON_KOPIA_SFTP_USERNAME'], 'sophon-backup')
                self.assertEqual(captured_prepare['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64'], 'cndsZXk=')
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_vcenter_and_hyperv_support_optional_secrets_iso_and_safe_destroy(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            iso1 = d / 'installer.iso'; iso1.write_bytes(b'inst')
            iso2 = d / 'bundle.iso'; iso2.write_bytes(b'bund')
            iso3 = d / 'secrets.iso'; iso3.write_bytes(b'secr')

            vc_calls = []

            def run_govc(*args):
                vc_calls.append(args)
                if args[0] == 'find':
                    return ''
                if args[0] == 'datastore.ls':
                    return json.dumps([{'file': []}])
                if args[0] == 'device.cdrom.add':
                    return f'cdrom-{len(vc_calls)}\n'
                return ''

            res_vc = vcenter_vm.provision({
                'name': 'sophon-dev', 'datacenter': 'DC', 'cluster': 'C', 'datastore': 'DS', 'network': 'N',
                'folder': '', 'cores': 4, 'memory': 4096, 'disk_gb': 60,
                'coreos_iso': str(iso1), 'bundle_iso': str(iso2), 'secrets_iso': str(iso3),
            }, run_govc)
            self.assertTrue(res_vc['created'])
            inserts = [c for c in vc_calls if c[0] == 'device.cdrom.insert']
            self.assertEqual(len(inserts), 2)

            # Unmanaged VM destroy must fail closed on vCenter!
            def run_govc_unmanaged(*args):
                if args[0] == 'find':
                    return '/DC/vm/unmanaged'
                if args[0] == 'vm.info':
                    return json.dumps({'virtualMachines': [{'config': {'annotation': 'other', 'template': False},
                                                            'runtime': {'powerState': 'poweredOn'}}]})
                return ''

            with self.assertRaisesRegex(ValueError, 'unmanaged'):
                vcenter_vm.destroy({'name': 'unmanaged', 'datacenter': 'DC', 'folder': ''}, run_govc_unmanaged)

            # Hyper-V 3-ISO test
            scripts = []
            uploads = []

            class StubHV:
                def run(self, src):
                    scripts.append(src)
                    if '$vms = @(Get-VM' in src:
                        return {'count': 0, 'state': '', 'notes': ''}
                    if 'Get-Item -LiteralPath' in src:
                        return [{'name': n, 'size': -1} for n in json.loads(
                            base64.b64decode(src.split("FromBase64String('")[1].split("'")[0])
                        )['names']]
                    return {}

                def upload(self, l, r):
                    uploads.append((l, r))

            res_hv = hyperv_vm.provision({
                'name': 'sophon-dev', 'switch': 'Ext', 'path': '', 'cores': 4, 'memory': 4096, 'disk_gb': 60,
                'coreos_iso': str(iso1), 'bundle_iso': str(iso2), 'secrets_iso': str(iso3),
            }, StubHV(), {'host': 'HV1', 'vm_path': 'D:\\Hyper-V'})
            self.assertTrue(res_hv['created'])
            self.assertEqual(len(uploads), 3)


if __name__ == '__main__':
    unittest.main()
