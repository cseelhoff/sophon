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
        self.assertEqual(cli.COMMANDS['build-isos']([]),
                         ['sh', '-eu', '-c', 'python3 src/secrets_iso.py "$@" && ansible-playbook 1-prepare.yml', 'build-isos'])
        self.assertEqual(cli.COMMANDS['build-isos'](['--force']),
                         ['sh', '-eu', '-c', 'python3 src/secrets_iso.py "$@" && ansible-playbook 1-prepare.yml -e prepare_force=true', 'build-isos'])
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

    def test_every_script_the_commands_run_exists(self):
        scripts = {part for command in cli.COMMANDS.values() for part in command([])
                   if part.endswith(('.py', '.yml')) and ' ' not in part}
        for script in scripts:
            self.assertTrue((ROOT / script).is_file(), script)


if __name__ == '__main__':
    unittest.main()
