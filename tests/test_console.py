import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import console
import deployment


def service_values():
    values = deployment.new_control({}, 'configure')['values']
    values.update({name: 'FIXTURE_SECRET_VALUE' for name, field in deployment.step_fields('configure').items()
                   if field[2] == 'password' and field[4]})
    values.update(SOPHON_DOMAIN='example.test', SOPHON_INFRAVM_IP='192.0.2.3',
                  SOPHON_DEPLOYMENT_STEP='configure')
    return values


class ConsoleTests(unittest.TestCase):
    def test_large_certificate_paste_is_not_echoed_or_truncated_in_a_real_terminal(self):
        pid, terminal = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, '-c',
                f'import sys,termios; sys.path.insert(0,{str(ROOT / "src")!r}); import console; '
                'before=termios.tcgetattr(0); value=console.read_certificate("CERTIFICATE: "); '
                'assert termios.tcgetattr(0)==before; '
                'assert value=="X"*120000; print("COMPLETE_PRIVATE_PASTE",flush=True)'])
        try:
            output = b''
            deadline = time.monotonic() + 15
            while b'CERTIFICATE: ' not in output:
                self.assertLess(time.monotonic(), deadline, 'Certificate prompt did not start')
                if select.select([terminal], [], [], 1)[0]:
                    output += os.read(terminal, 4096)
            data = b'X' * 120000 + b'\n'
            while data:
                data = data[os.write(terminal, data):]
            while b'COMPLETE_PRIVATE_PASTE' not in output:
                self.assertLess(time.monotonic(), deadline, 'Large certificate input was truncated or blocked')
                if select.select([terminal], [], [], 1)[0]:
                    output += os.read(terminal, 4096)
            self.assertNotIn(b'XXXX', output)
            _, status = os.waitpid(pid, 0)
            pid = None
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            os.close(terminal)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_run_dispatch_and_hidden_input(self):
        environment = service_values()
        with patch.dict(os.environ, environment, clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=19) as run, \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=lambda prompt: 'y' if prompt.endswith('[y/N] ') else ''), \
                patch('console.getpass.getpass', return_value='') as hidden, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main([]), 19)
            run.assert_called_once_with(['--step', 'configure', '--', '4-configure.yml'])
            self.assertEqual(hidden.call_count, sum(field[2] in ('password', 'certificate')
                                                   for field in deployment.step_fields('configure').values()))
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

    def test_certificate_refresh_is_explicit_and_console_only(self):
        with patch.dict(os.environ, service_values(), clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=0) as run:
            self.assertEqual(console.main(['--run', '--refresh-certificates']), 0)
            run.assert_called_once_with(['--step', 'configure', '--', '4-configure.yml',
                                        '--extra-vars', 'traefik_acme_replace_existing=true'])
            run.reset_mock()
            with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
                console.main(['--serve'])
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_values_chosen_at_install_are_not_asked_again(self):
        site = {'SOPHON_INFRAVM_IP': '192.0.2.3', 'SOPHON_VNET_DNS': '192.0.2.53'}
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return 'y' if prompt.endswith('[y/N] ') else ''

        with patch.dict(os.environ, service_values() | site, clear=True), \
                patch('console.deployment.load_site_settings', return_value=site), \
                patch('console.deployment.main', return_value=0), \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=answer), patch('console.getpass.getpass', return_value=''), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main([]), 0)
        self.assertFalse([prompt for prompt in prompts if 'SOPHON_INFRAVM_IP' in prompt or 'SOPHON_VNET_DNS' in prompt])
        self.assertTrue([prompt for prompt in prompts if 'SOPHON_DOMAIN' in prompt])
        self.assertIn('192.0.2.53 (from installation)', output.getvalue())

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
                [sys.executable, str(ROOT / 'src/console.py'), '--run'],
                env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
            forwarded = json.loads(received.read_text())
            self.assertNotIn('DO_NOT_FORWARD', forwarded.values())
            self.assertEqual(forwarded['SOPHON_INFRAVM_IP'], '192.0.2.3')
            self.assertEqual(forwarded['SOPHON_DEPLOYMENT_STEP'], 'configure')


if __name__ == '__main__':
    unittest.main()
