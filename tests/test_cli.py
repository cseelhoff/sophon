import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import cli


class CommandTests(unittest.TestCase):
    def test_commands_map_to_the_step_entry_points(self):
        expected_default = (
            'trap "rm -f /tmp/sophon-build-dotenv.json" EXIT; '
            'python3 src/secrets_iso.py --defer-dotenv /tmp/sophon-build-dotenv.json "$@" && '
            'ansible-playbook 1-prepare.yml && '
            'python3 src/secrets_iso.py --apply-dotenv /tmp/sophon-build-dotenv.json'
        )
        expected_force = (
            'trap "rm -f /tmp/sophon-build-dotenv.json" EXIT; '
            'python3 src/secrets_iso.py --defer-dotenv /tmp/sophon-build-dotenv.json "$@" && '
            'ansible-playbook 1-prepare.yml -e prepare_force=true && '
            'python3 src/secrets_iso.py --apply-dotenv /tmp/sophon-build-dotenv.json'
        )
        self.assertEqual(cli.COMMANDS['build-isos']([]),
                         ['sh', '-eu', '-c', expected_default, 'build-isos'])
        self.assertEqual(cli.COMMANDS['build-isos'](['--force']),
                         ['sh', '-eu', '-c', expected_force, 'build-isos'])
        self.assertEqual(cli.COMMANDS['build-isos'](['--os-only', '--force']),
                         ['ansible-playbook', '1-prepare.yml', '-e', 'prepare_force=true'])
        self.assertEqual(cli.COMMANDS['build-isos'](['--secrets-only']),
                         ['python3', 'src/secrets_iso.py'])
        self.assertEqual(cli.COMMANDS['prepare'](['-e', 'prepare_force=true']),
                         ['ansible-playbook', '1-prepare.yml', '-e', 'prepare_force=true'])
        self.assertEqual(cli.COMMANDS['provision']([])[-2:], ['--', '2-provision.yml'])
        self.assertEqual(cli.COMMANDS['provision'](['--recreate', '--yes']),
                         ['python3', 'src/verify_restore.py', '--prod-recreate', '--yes'])
        self.assertEqual(cli.COMMANDS['dev'](['--wait-https']),
                         ['python3', 'src/verify_restore.py', '--wait-https'])
        self.assertEqual(cli.COMMANDS['provision-dev'](['--wait-https']),
                         ['python3', 'src/verify_restore.py', '--wait-https'])
        self.assertEqual(cli.COMMANDS['backup']([])[-2:], ['--', '2-provision-backup.yml'])
        self.assertEqual(cli.COMMANDS['provision-backup']([])[-2:], ['--', '2-provision-backup.yml'])
        self.assertEqual(cli.COMMANDS['destroy-vm']([]), ['python3', 'src/verify_restore.py', '--destroy-only'])
        self.assertEqual(cli.COMMANDS['destroy-vm'](['--vm-name', 'sophon-backup']),
                         ['python3', 'src/verify_restore.py', '--destroy-only', '--vm-name', 'sophon-backup'])
        self.assertIn('--step', cli.COMMANDS['provision']([]))
        self.assertNotIn('dashboard', cli.COMMANDS)
        self.assertEqual(cli.COMMANDS['verify-restore'](['--teardown', 'destroy']),
                         ['python3', 'src/verify_restore.py', '--teardown', 'destroy'])
        self.assertEqual(cli.COMMANDS['certificates'](['issue']), ['python3', 'src/certificates.py', 'issue'])

    def test_load_dotenv_sets_0600_respects_shell_precedence_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text('SOPHON_DOMAIN="sophon.example.com"\nSOPHON_ACME_EMAIL=ops@example.com\nIGNORED_VAR=1\n')
            os.chmod(env_path, 0o644)
            link_path = Path(tmp) / 'link.env'
            link_path.symlink_to(env_path)
            with self.assertRaises(ValueError):
                cli.load_dotenv(link_path)

            old_env = os.environ.copy()
            try:
                os.environ['SOPHON_DOMAIN'] = 'override.example.com'
                os.environ.pop('SOPHON_ACME_EMAIL', None)
                cli.load_dotenv(env_path)
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
                self.assertEqual(os.environ['SOPHON_DOMAIN'], 'override.example.com')
                self.assertEqual(os.environ['SOPHON_ACME_EMAIL'], 'ops@example.com')
                self.assertNotIn('IGNORED_VAR', os.environ)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_save_dotenv_sets_0600_preserves_existing_settings_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            env_path = tmp_dir / '.env'
            link_path = tmp_dir / 'symlink.env'
            link_path.symlink_to(env_path)
            with self.assertRaises(ValueError):
                cli.save_dotenv({'SOPHON_DOMAIN': 'sophon.example.com'}, path=link_path)

            cli.save_dotenv(
                {
                    'SOPHON_DOMAIN': 'sophon.example.com',
                    'SOPHON_SHARED_PASSWORD': ' Complex#Pass"With\'Quotes! ',
                    'SOPHON_WITH_BACKUP_SECRETS': 'true',
                },
                path=env_path,
                template_path=ROOT / '.env.example',
            )
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            text = env_path.read_text(encoding='utf-8')
            self.assertIn('# --- Proxmox', text)
            self.assertIn('SOPHON_PROXMOX_PORT=8006', text)

            # Updating an existing .env preserves prior hypervisor entries and comments
            env_path.write_text(text.replace('SOPHON_PROXMOX_HOST=', 'SOPHON_PROXMOX_HOST=10.0.20.10'), encoding='utf-8')
            cli.save_dotenv({'SOPHON_DEV_NETWORK_CIDR': '10.0.60.107/24'}, path=env_path, template_path=ROOT / '.env.example')
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

            old_env = os.environ.copy()
            try:
                for key in list(os.environ):
                    if key.startswith('SOPHON_') or key == 'CF_DNS_API_TOKEN':
                        os.environ.pop(key, None)
                loaded = cli.load_dotenv(env_path)
                self.assertEqual(loaded['SOPHON_PROXMOX_HOST'], '10.0.20.10')
                self.assertEqual(loaded['SOPHON_DOMAIN'], 'sophon.example.com')
                self.assertEqual(loaded['SOPHON_DEV_NETWORK_CIDR'], '10.0.60.107/24')
                self.assertEqual(loaded['SOPHON_SHARED_PASSWORD'], ' Complex#Pass"With\'Quotes! ')
                self.assertEqual(loaded['SOPHON_WITH_BACKUP_SECRETS'], 'true')
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_every_script_the_commands_run_exists(self):
        scripts = {part for command in cli.COMMANDS.values() for part in command([])
                   if part.endswith(('.py', '.yml')) and ' ' not in part}
        for script in scripts:
            self.assertTrue((ROOT / script).is_file(), script)

    def test_standalone_wrapper_scripts_and_help_entrypoints(self):
        for name in (
            'sophon-provision.sh',
            'sophon-provision-dev.sh',
            'sophon-destroy-vm.sh',
            'sophon-provision.ps1',
            'sophon-provision-dev.ps1',
            'sophon-destroy-vm.ps1',
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn('provision-dev', cli.HELP_BY_ENTRYPOINT)
        self.assertIn('destroy-vm', cli.HELP_BY_ENTRYPOINT)
        self.assertIn('sophon-provision-dev.sh', cli.HELP_PROVISION_DEV)
        self.assertIn('sophon-destroy-vm.sh', cli.HELP_DESTROY_VM)
        self.assertIn('sophon-provision-dev.sh', cli.HELP)
        self.assertIn('sophon-destroy-vm.sh', cli.HELP)
        self.assertNotIn('destroy-vm', cli.HELP_PROVISION)


if __name__ == '__main__':
    unittest.main()
