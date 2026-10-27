import base64
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tarfile
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import certificates


class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.now = datetime.now(timezone.utc)
        cls.ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Fixture CA')])
        cls.ca = (x509.CertificateBuilder().subject_name(cls.ca_name).issuer_name(cls.ca_name)
                  .public_key(cls.key.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(cls.now - timedelta(days=2)).not_valid_after(cls.now + timedelta(days=365))
                  .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                  .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                  .add_extension(x509.SubjectKeyIdentifier.from_public_key(cls.key.public_key()), critical=False)
                  .sign(cls.key, hashes.SHA256()))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'acme.json'
        ca = self.directory / 'ca.pem'
        ca.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        environment = patch.dict(os.environ, SSL_CERT_FILE=str(ca), SSL_CERT_DIR=str(self.directory))
        environment.start()
        self.addCleanup(environment.stop)
        self.payload = {'letsencrypt': {'Account': {}, 'Certificates': [
            self.entry(f'{service}.fixture.example') for service in certificates.SERVICES]}}
        self.save()

    def entry(self, name, expired=False):
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(self.ca_name).public_key(self.key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(self.now - timedelta(days=2))
                .not_valid_after(self.now + timedelta(days=-1 if expired else 80))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(self.key, hashes.SHA256()))
        key = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
        return {'domain': {'main': name}, 'certificate': base64.b64encode(
            leaf.public_bytes(serialization.Encoding.PEM)).decode(),
                'key': base64.b64encode(key).decode(), 'Store': 'default'}

    def save(self):
        self.source.write_text(json.dumps(self.payload))
        self.source.chmod(0o600)

    def test_all_service_names_chains_keys_and_expiries_are_validated(self):
        result = certificates.validate(self.source, 'Fixture.Example.')
        self.assertEqual(set(result), {f'{service}.fixture.example' for service in certificates.SERVICES})
        self.assertTrue(all(datetime.fromisoformat(expiry) > self.now for expiry in result.values()))

    def test_issue_command_reuses_valid_store_and_prints_only_base64(self):
        state = self.directory / 'state' / 'fixture.example'
        state.mkdir(parents=True, mode=0o700)
        (state / 'acme.json').write_bytes(self.source.read_bytes())
        (state / 'acme.json').chmod(0o600)
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()), \
                patch('subprocess.Popen') as process, \
                patch('getpass.getpass') as secret:
            self.assertEqual(certificates.main([
                'issue', '--domain', 'fixture.example', '--email', 'admin@fixture.example',
                '--state-dir', str(self.directory / 'state')]), 0)
        self.assertEqual(json.loads(base64.b64decode(output.getvalue().strip())), self.payload)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        process.assert_not_called()
        secret.assert_not_called()

    def test_issue_command_prompts_secret_and_runs_a_real_private_issuer_process(self):
        executable = self.directory / 'traefik'
        received = self.directory / 'issuer-arguments.json'
        executable.write_text(
            f'#!{sys.executable}\n'
            'import json,os,sys,time\nfrom pathlib import Path\n'
            f'Path({str(received)!r}).write_text(json.dumps(dict(args=sys.argv[1:],pid=os.getpid(),'
            'token=os.environ.get("CF_DNS_API_TOKEN"))))\n'
            'target=next(arg.split("=",1)[1] for arg in sys.argv if ".acme.storage=" in arg)\n'
            f'Path(target).write_bytes(Path({str(self.source)!r}).read_bytes())\n'
            'Path(target).chmod(0o600)\ntime.sleep(60)\n')
        executable.chmod(0o700)
        environment = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ['PATH'])
        environment.pop('CF_DNS_API_TOKEN', None)
        with patch.dict(os.environ, environment, clear=True), \
                patch('getpass.getpass', return_value='FAKE_CLOUDFLARE_TOKEN') as secret, \
                patch('builtins.input', side_effect=['fixture.example', 'admin@fixture.example']), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as progress:
            self.assertEqual(certificates.main([
                'issue', '--state-dir', str(self.directory / 'issued'), '--timeout', '10']), 0)
        invocation = json.loads(received.read_text())
        self.assertNotIn('FAKE_CLOUDFLARE_TOKEN', ' '.join(invocation['args']) + output.getvalue() + progress.getvalue())
        self.assertEqual(invocation['token'], 'FAKE_CLOUDFLARE_TOKEN')
        self.assertEqual(json.loads(base64.b64decode(output.getvalue().strip())), self.payload)
        secret.assert_called_once()
        with self.assertRaises(ProcessLookupError):
            os.kill(invocation['pid'], 0)
        private = self.directory / 'issued/fixture.example'
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((private / 'issuer.log').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((private / 'acme.json').stat().st_mode), 0o600)

    def test_invalid_issuer_inputs_never_export_a_payload(self):
        for arguments in (['--domain', '../../escape'], ['--domain', 'fixture.example', '--timeout', '0']):
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                certificates.main(['issue', '--state-dir', str(self.directory), *arguments])
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(output.getvalue(), '')

    def test_failed_issuer_exports_nothing_and_keeps_its_log_private(self):
        executable = self.directory / 'traefik'
        executable.write_text(f'#!{sys.executable}\nprint("FAKE_PRIVATE_ISSUER_DETAIL")\nraise SystemExit(27)\n')
        executable.chmod(0o700)
        with patch.dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ['PATH'],
                        CF_DNS_API_TOKEN='FAKE_TOKEN'), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as stopped:
            certificates.main(['issue', '--domain', 'fixture.example', '--email', 'admin@fixture.example',
                               '--state-dir', str(self.directory / 'failed')])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('issuer exited', error.getvalue())
        self.assertNotIn('FAKE_PRIVATE_ISSUER_DETAIL', error.getvalue())
        self.assertEqual(stat.S_IMODE((self.directory / 'failed/fixture.example/issuer.log').stat().st_mode), 0o600)

    def test_payload_import_is_private_atomic_and_rejects_bad_input_without_replacement(self):
        encoded = base64.b64encode(self.source.read_bytes()).decode()
        self.assertGreater(len(encoded), 4096)
        destination = self.directory / 'imported/acme.json'
        certificates.import_payload(encoded, 'fixture.example', destination)
        self.assertEqual(json.loads(destination.read_bytes()), self.payload)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        for invalid in ('not base64!', base64.b64encode(b'{"letsencrypt":{"Account":{}}}').decode(),
                        base64.b64encode(b'[]').decode(), 'x' * (certificates.MAX_PAYLOAD + 1)):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                certificates.import_payload(invalid, 'fixture.example', destination)
            self.assertEqual(json.loads(destination.read_bytes()), self.payload)
        with self.assertRaises(ValueError):
            certificates.import_payload(encoded, 'wrong.example', destination)

    def test_service_payload_is_staged_but_never_forwarded_in_environment_or_command(self):
        import deployment
        from test_console import service_values
        encoded = base64.b64encode(self.source.read_bytes()).decode()
        settings = service_values() | {'SOPHON_DOMAIN': 'fixture.example', 'SOPHON_ACME_PAYLOAD': encoded}
        current = deployment.new_control(settings, 'configure')
        self.assertNotIn('SOPHON_ACME_PAYLOAD', current['environment'])
        self.assertNotIn('SOPHON_ACME_PAYLOAD', deployment.SITE_FIELDS)
        self.assertNotIn('SOPHON_ACME_PAYLOAD', deployment.step_fields('provision'))
        values, error = deployment.validate_settings(current['values'], {}, 'configure')
        self.assertIsNone(error)
        with patch('deployment.ROOT', self.directory):
            staged = deployment.stage_certificates(values)
            command = deployment.shell_command(self.directory, staged, current['environment'], 'configure')
        self.assertNotIn(encoded, command)
        self.assertNotIn('SOPHON_ACME_PAYLOAD', staged)
        self.assertEqual(json.loads((self.directory / 'artifacts/traefik/acme.json').read_bytes()), self.payload)

    def test_dashboard_accepts_large_certificate_import_without_putting_it_in_the_command(self):
        import deployment
        from test_console import service_values
        encoded = base64.b64encode(self.source.read_bytes() + b' ' * 32768).decode()
        changes = {'SOPHON_ACME_PAYLOAD': encoded, 'SOPHON_DOMAIN': 'fixture.example'}
        data = json.dumps(changes).encode()
        self.assertGreater(len(data), 32768)
        control = deployment.new_control(service_values(), 'configure')
        environment = {
            'SERVER_PORT': '12345', 'HTTP_ORIGIN': 'http://127.0.0.1:12345',
            'HTTP_X_SOPHON_TOKEN': control['token'], 'CONTENT_TYPE': 'application/json',
            'CONTENT_LENGTH': str(len(data)), 'PATH_INFO': '/command', 'wsgi.input': io.BytesIO(data),
        }
        with patch('deployment.ROOT', self.directory):
            status, response = deployment.action_request(self.directory, control, environment)
        self.assertEqual(status, '200 OK', response)
        self.assertNotIn(encoded, response['command'])
        imported = self.directory / 'artifacts/traefik/acme.json'
        self.assertEqual(json.loads(imported.read_bytes()), self.payload)
        self.assertEqual(stat.S_IMODE(imported.stat().st_mode), 0o600)

    def test_console_wizard_imports_only_after_confirmation_and_never_exports_the_payload(self):
        import console
        from test_console import service_values
        encoded = base64.b64encode(self.source.read_bytes()).decode()
        for confirmed in (False, True):
            root = self.directory / ('confirmed' if confirmed else 'cancelled')
            settings = service_values() | {'SOPHON_DOMAIN': 'fixture.example'}
            with self.subTest(confirmed=confirmed), patch.dict(os.environ, settings, clear=False), \
                    patch('console.deployment.ROOT', root), \
                    patch('console.deployment.load_site_settings', return_value={}), \
                    patch('console.deployment.main', return_value=0) as run, \
                    patch('console.sys.stdin.isatty', return_value=True), \
                    patch('console.getpass.getpass', side_effect=lambda prompt:
                          encoded if prompt.startswith('Pre-issued certificates') else ''), \
                    patch('builtins.input', side_effect=lambda prompt:
                          'y' if confirmed and prompt.endswith('[y/N] ') else ''), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(console.main([]), 0)
                self.assertNotIn(encoded, output.getvalue())
                self.assertFalse(os.environ.get('SOPHON_ACME_PAYLOAD'), 'Wizard forwarded the certificate payload')
            target = root / 'artifacts/traefik/acme.json'
            self.assertEqual(target.exists(), confirmed)
            self.assertEqual(run.call_count, int(confirmed))
            if confirmed:
                self.assertEqual(json.loads(target.read_bytes()), self.payload)

    def test_missing_expired_wrong_host_and_wrong_private_key_fail(self):
        entries = self.payload['letsencrypt']['Certificates']
        original = entries[0]
        wrong_key = self.other_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption())
        for replacement in (
            self.entry('traefik.fixture.example', expired=True),
            self.entry('wrong.fixture.example') | {'domain': {'main': 'traefik.fixture.example'}},
            original | {'key': base64.b64encode(wrong_key).decode()},
        ):
            entries[0] = replacement
            self.save()
            with self.assertRaises(ValueError):
                certificates.validate(self.source, 'fixture.example')
        entries.pop(0)
        self.save()
        with self.assertRaisesRegex(ValueError, 'Missing pre-issued'):
            certificates.validate(self.source, 'fixture.example')

    def test_untrusted_certificates_are_not_accepted(self):
        empty = self.directory / 'empty.pem'
        empty.write_bytes(b'')
        with patch.dict(os.environ, SSL_CERT_FILE=str(empty), SSL_CERT_DIR='/nonexistent'):
            with self.assertRaises(ValueError):
                certificates.validate(self.source, 'fixture.example')

    def test_tokens_and_unrelated_configuration_are_rejected(self):
        self.payload['cloudflare_token'] = 'DO_NOT_TRANSPORT'
        self.save()
        with self.assertRaises(ValueError):
            certificates.validate(self.source, 'fixture.example')
        del self.payload['cloudflare_token']
        self.payload['letsencrypt']['Account']['CloudflareToken'] = 'DO_NOT_TRANSPORT'
        self.save()
        with self.assertRaises(ValueError):
            certificates.validate(self.source, 'fixture.example')

    def test_check_command_validates_a_file_and_rejects_a_bad_one(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(certificates.main(['check', '--source', str(self.source), '--domain', 'fixture.example']), 0)
        self.assertEqual(set(json.loads(output.getvalue())['certificate_expiries']),
                         {f'{service}.fixture.example' for service in certificates.SERVICES})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            certificates.main(['check', '--source', str(self.source), '--domain', 'other.example'])
        self.assertEqual(stopped.exception.code, 1)


class AcmeRestoreTests(unittest.TestCase):
    def exercise(self, live_exists, replace):
        from test_deployment import run_fixture
        calls = []
        live = io.BytesIO()
        with tarfile.open(fileobj=live, mode='w') as archive:
            data = b'newer live certificate store'
            info = tarfile.TarInfo('acme.json')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

        class Handler(BaseHTTPRequestHandler):
            def respond(self, status, value=None):
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/octet-stream' if isinstance(value, bytes) else 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                calls.append(('POST', self.path, body))
                if self.path == '/api/auth':
                    self.respond(200, {'jwt': 'fixture-token'})
                elif self.path.endswith('/images/load'):
                    self.respond(200, {'stream': 'Loaded offline image'})
                elif self.path.endswith('/containers/create?name=traefik-acme-seed'):
                    self.respond(201, {'Id': 'seed'})
                elif '/containers/traefik/stop?' in self.path:
                    self.respond(204)
                else:
                    self.respond(500, {'message': 'Unexpected network operation'})

            def do_GET(self):
                calls.append(('GET', self.path, b''))
                if self.path == '/api/endpoints':
                    self.respond(200, [{'Id': 1}])
                else:
                    self.respond(200 if live_exists else 404, live.getvalue() if live_exists else {'message': 'No file'})

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                calls.append(('PUT', self.path, body))
                self.respond(200)

            def do_DELETE(self):
                calls.append(('DELETE', self.path, b''))
                self.respond(204)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary, HTTPServer(('127.0.0.1', 0), Handler) as server:
            directory = Path(temporary)
            source = directory / 'acme.json'
            source.write_text('local carried certificate store')
            image = directory / 'traefik.tar'
            image.write_bytes(b'prestaged image archive')
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with patch.dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / 'roles')):
                    result, _ = run_fixture(directory, [
                        {'ansible.builtin.set_fact': {
                            'infravm_portainer_url': f'http://127.0.0.1:{server.server_port}',
                            'portainer_admin_password': 'fixture-password',
                            'traefik_acme_backup_path': str(source),
                            'traefik_acme_artifacts_dir': str(directory),
                            'traefik_image_tars': [str(image)],
                            'traefik_acme_replace_existing': replace,
                        }},
                        {'ansible.builtin.include_role': {'name': 'traefik', 'tasks_from': 'restore_acme'}},
                    ])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.shutdown()
                thread.join()
        self.assertFalse(any('/images/create' in path for _, path, _ in calls))
        self.assertTrue(any(path.endswith('/images/load') for _, path, _ in calls))
        puts = [body for method, _, body in calls if method == 'PUT']
        self.assertEqual(len(puts), int(not live_exists or replace))
        if puts:
            with tarfile.open(fileobj=io.BytesIO(puts[0])) as archive:
                self.assertEqual(archive.extractfile('acme.json').read(), b'local carried certificate store')
        stops = [path for _, path, _ in calls if '/containers/traefik/stop?' in path]
        self.assertEqual(len(stops), int(replace))
        self.assertEqual(calls[-1][0], 'DELETE')

    def test_fresh_offline_volume_is_seeded_without_registry_access(self):
        self.exercise(False, False)

    def test_rerun_never_overwrites_a_newer_live_store_with_a_stale_snapshot(self):
        self.exercise(True, False)

    def test_explicit_refresh_stops_traefik_before_replacing_the_store(self):
        self.exercise(True, True)


if __name__ == '__main__':
    unittest.main()
