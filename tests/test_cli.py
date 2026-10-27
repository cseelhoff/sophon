from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import cli


class CommandTests(unittest.TestCase):
    def test_commands_map_to_the_step_entry_points(self):
        self.assertEqual(cli.COMMANDS['prepare'](['-e', 'prepare_force=true']),
                         ['ansible-playbook', '1-prepare.yml', '-e', 'prepare_force=true'])
        self.assertEqual(cli.COMMANDS['provision']([])[-2:], ['--', '2-provision.yml'])
        self.assertIn('--step', cli.COMMANDS['provision']([]))
        self.assertNotIn('dashboard', cli.COMMANDS)
        self.assertEqual(cli.COMMANDS['certificates'](['issue']), ['python3', 'src/certificates.py', 'issue'])

    def test_every_script_the_commands_run_exists(self):
        scripts = {part for command in cli.COMMANDS.values() for part in command([])
                   if part.endswith(('.py', '.yml')) and ' ' not in part}
        for script in scripts:
            self.assertTrue((ROOT / script).is_file(), script)


if __name__ == '__main__':
    unittest.main()
