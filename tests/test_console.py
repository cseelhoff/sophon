import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import console
import deployment


def service_values():
    values = deployment.new_control({}, 'services')['values']
    values.update({name: 'FIXTURE_SECRET_VALUE' for name, field in deployment.phase_fields('services').items()
                   if field[2] == 'password' and field[4]})
    values.update(SOPHON_DOMAIN='example.test', SOPHON_INFRAVM_IP='192.0.2.3',
                  SOPHON_DEPLOYMENT_PHASE='services')
    return values


class ConsoleTests(unittest.TestCase):
    def test_run_dispatch_and_hidden_input(self):
        environment = service_values()
        with patch.dict(os.environ, environment, clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=19) as run, \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=lambda prompt: 'y' if prompt.endswith('[y/N] ') else ''), \
                patch('console.getpass.getpass', return_value='') as hidden, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main(['--state-dir', '/workspace/state']), 19)
            run.assert_called_once_with(['--phase', 'services', '--state-dir', '/workspace/state', '--', 'site.yml'])
            self.assertEqual(hidden.call_count, sum(field[2] == 'password'
                                                   for field in deployment.phase_fields('services').values()))
            self.assertNotIn('FIXTURE_SECRET_VALUE', output.getvalue())

    def test_invalid_run_does_not_launch_or_expose_secret(self):
        for updates in ({'SOPHON_PORTAINER_ADMIN_PASSWORD': 'short'}, {'SOPHON_DOMAIN': ''},
                        {'SOPHON_INFRAVM_IP': '999.0.0.1'}):
            with self.subTest(updates=updates), patch.dict(os.environ, service_values() | updates, clear=True), \
                    patch('console.deployment.load_site_settings', return_value={}), \
                    patch('console.deployment.main') as run, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(console.main(['--run']), 2)
                run.assert_not_called()
                self.assertNotIn('FIXTURE_SECRET_VALUE', error.getvalue())
                self.assertIn('Configuration rejected:', error.getvalue())

    def test_dashboard_starts_without_service_secrets(self):
        with patch.dict(os.environ, {'SOPHON_DEPLOYMENT_PHASE': 'services'}, clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=0) as run:
            self.assertEqual(console.main(['--dashboard', '--port', '0']), 0)
            run.assert_called_once_with(['--phase', 'services', '--serve', '--port', '0'])

    def test_decline_does_not_deploy(self):
        with patch.dict(os.environ, service_values(), clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main') as run, \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=''), patch('console.getpass.getpass', return_value=''), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main([]), 0)
            run.assert_not_called()
            self.assertIn('cancelled', output.getvalue())

    def test_real_runner_filters_environment_and_reports_playbook_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / 'ansible-playbook'
            received = directory / 'received.json'
            binary.write_text(
                f'#!{sys.executable}\nimport json, os, sys\n'
                f'open({str(received)!r}, "w").write(json.dumps(dict(os.environ)))\n'
                'sys.exit(7)\n')
            binary.chmod(0o700)
            environment = dict(os.environ, **service_values(),
                               SOPHON_PROXMOX_PASSWORD='DO_NOT_FORWARD', SOPHON_NFS_IP='DO_NOT_FORWARD',
                               SOPHON_KOPIA_REPOSITORY_PASSWORD='DO_NOT_FORWARD',
                               PATH=str(directory) + os.pathsep + os.environ['PATH'])
            result = subprocess.run(
                [sys.executable, str(ROOT / 'console.py'), '--run', '--state-dir', str(directory / 'state')],
                env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
            forwarded = json.loads(received.read_text())
            self.assertNotIn('DO_NOT_FORWARD', forwarded.values())
            self.assertEqual(forwarded['SOPHON_INFRAVM_IP'], '192.0.2.3')
            self.assertEqual((directory / 'state/ansible.log').stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((directory / 'state/status.json').read_text())['exit_code'], 7)

    def test_optional_dashboard_launches_services_with_edited_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            received = directory / 'received.json'
            binary = directory / 'ansible-playbook'
            binary.write_text(
                f'#!{sys.executable}\nimport json, os, sys\n'
                f'open({str(received)!r}, "w").write(json.dumps([sys.argv[1:], dict(os.environ)]))\n')
            binary.chmod(0o700)
            control = deployment.new_control(dict(os.environ, **service_values(),
                PATH=str(directory) + os.pathsep + os.environ['PATH']), 'services')
            data = json.dumps({'SOPHON_DOMAIN': 'edited.example'}).encode()
            environment = {
                'SERVER_PORT': '12345', 'HTTP_HOST': '127.0.0.1:12345', 'REQUEST_METHOD': 'POST',
                'PATH_INFO': '/run', 'HTTP_ORIGIN': 'http://127.0.0.1:12345',
                'HTTP_X_SOPHON_TOKEN': control['token'], 'CONTENT_TYPE': 'application/json',
                'CONTENT_LENGTH': str(len(data)), 'wsgi.input': io.BytesIO(data),
            }
            status, payload = deployment.action_request(directory, control, environment)
            self.assertEqual(status, '202 Accepted', payload)
            control['processes'][0].wait(timeout=10)
            args, forwarded = json.loads(received.read_text())
            self.assertEqual(args, ['site.yml'])
            self.assertEqual(forwarded['SOPHON_DOMAIN'], 'edited.example')
            self.assertEqual(forwarded['SOPHON_DEPLOYMENT_PHASE'], 'services')


if __name__ == '__main__':
    unittest.main()
