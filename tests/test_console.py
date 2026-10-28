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
                patch('builtins.input', side_effect=lambda prompt: ''), \
                patch('console.getpass.getpass', return_value='token') as hidden, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main([]), 19)
            run.assert_called_once_with(['--step', 'configure', '--', '4-configure.yml'])
            # Supplied secrets are not asked again; only the Cloudflare token, licence key and Gitea DB password are.
            self.assertEqual(hidden.call_count, 3)
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
            return ''

        with patch.dict(os.environ, service_values() | site, clear=True), \
                patch('console.deployment.load_site_settings', return_value=site), \
                patch('console.deployment.main', return_value=0), \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=answer), patch('console.getpass.getpass', return_value='token'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(console.main([]), 0)
        self.assertFalse([prompt for prompt in prompts if 'SOPHON_INFRAVM_IP' in prompt or 'SOPHON_VNET_DNS' in prompt])
        self.assertTrue([prompt for prompt in prompts if 'SOPHON_DOMAIN' in prompt])
        self.assertIn('192.0.2.53 (from installation)', output.getvalue())

    def wizard(self, environment, answers, secrets=()):
        """Run the interactive wizard with scripted answers; returns (exit code, prompts, secret prompts, output)."""
        prompts, hidden = [], []
        answers, secrets = iter(answers), iter(secrets)

        def ask(prompt):
            prompts.append(prompt)
            return next(answers)

        def secret(prompt):
            hidden.append(prompt)
            return next(secrets)

        with patch.dict(os.environ, environment, clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=0), \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=ask), patch('console.getpass.getpass', side_effect=secret), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = console.main([])
        return code, prompts, hidden, output.getvalue()

    def test_connected_site_asks_for_email_and_token_but_not_certificates(self):
        values = service_values() | {'SOPHON_PORTAINER_ADMIN_PASSWORD': '', 'SOPHON_ACME_EMAIL': ''}
        # Inputs: air-gapped?, same password?, then DNS, IP, domain, email, tunnel, realm, realm name.
        code, prompts, hidden, _ = self.wizard(
            values, ['n', '', '', '', '', 'me@example.test', '', '', ''],
            ['token', 'long-enough-password', '', ''])
        self.assertEqual(code, 0)
        text = ' '.join(prompts + hidden)
        self.assertIn('SOPHON_ACME_EMAIL', text)
        self.assertIn('SOPHON_CF_TOKEN', text)
        self.assertIn('SOPHON_CF_TUNNEL_NAME', text)
        self.assertNotIn('SOPHON_ACME_PAYLOAD', text)
        self.assertTrue([prompt for prompt in hidden if 'SOPHON_PORTAINER_ADMIN_PASSWORD' in prompt])

    def test_air_gapped_site_asks_only_for_the_certificate_payload(self):
        values = service_values() | {'SOPHON_CF_TOKEN': 'WOULD_LEAK'}
        # Inputs: air-gapped?, then DNS, IP, domain, realm, realm name (no email, token or tunnel name).
        code, prompts, hidden, _ = self.wizard(values, ['y', '', '', '', '', ''], ['not-a-payload', '', ''])
        text = ' '.join(prompts + hidden)
        self.assertNotIn('SOPHON_ACME_EMAIL', text)
        self.assertNotIn('SOPHON_CF_TOKEN', text)
        self.assertNotIn('SOPHON_CF_TUNNEL_NAME', text)
        self.assertEqual(code, 2)  # the payload is asked for, validated and rejected before any deployment
        self.assertTrue([prompt for prompt in hidden if 'Pre-issued certificates' in prompt])

    def test_required_values_are_asked_again_and_portainer_password_has_a_minimum(self):
        values = service_values() | {'SOPHON_PORTAINER_ADMIN_PASSWORD': ''}
        code, prompts, hidden, output = self.wizard(
            values, ['n', '', '', '', '', 'me@example.test', '', '', ''],
            ['token', '', 'short', 'long-enough-password', '', ''])
        self.assertEqual(code, 0)
        self.assertEqual(len([prompt for prompt in hidden if 'SOPHON_PORTAINER_ADMIN_PASSWORD' in prompt]), 3)
        self.assertIn('at least 12 characters', output)

    def test_same_password_for_all_accounts(self):
        values = service_values() | {name: '' for name, field in deployment.step_fields('configure').items()
                                      if field[2] == 'password'}
        code, prompts, hidden, output = self.wizard(
            values, ['n', 'y'] + [''] * 7,
            ['same-password-123', 'same-password-123', 'token', ''])
        self.assertEqual(code, 0, output)
        asked = [prompt.split('(')[-1].split(')')[0] for prompt in hidden if 'SOPHON_' in prompt]
        self.assertEqual(asked, ['SOPHON_CF_TOKEN', 'SOPHON_PORTAINER_LICENSE_KEY'])

    def test_secret_prompt_shows_asterisks_and_handles_backspace(self):
        pid, terminal = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, '-c',
                f'import sys; sys.path.insert(0,{str(ROOT / "src")!r}); import console; '
                'value=console.read_secret("PW: "); print("GOT=" + value, flush=True)'])
        try:
            output = b''
            deadline = time.monotonic() + 15
            while b'PW: ' not in output:
                self.assertLess(time.monotonic(), deadline)
                if select.select([terminal], [], [], 1)[0]:
                    output += os.read(terminal, 4096)
            os.write(terminal, b'abcx\x7fd\r')
            while b'GOT=' not in output:
                self.assertLess(time.monotonic(), deadline)
                if select.select([terminal], [], [], 1)[0]:
                    output += os.read(terminal, 4096)
            os.waitpid(pid, 0)
            pid = None
            self.assertIn(b'GOT=abcd', output)
            self.assertIn(b'****', output)
            self.assertNotIn(b'abc', output.replace(b'GOT=abcd', b''))
        finally:
            os.close(terminal)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_ctrl_c_during_the_questions_does_not_deploy(self):
        with patch.dict(os.environ, service_values(), clear=True), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main') as run, \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(console.main([]), 130)
            run.assert_not_called()
            self.assertIn('cancelled', error.getvalue())

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
