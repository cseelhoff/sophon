import base64
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
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
                  .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
                  .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                  .add_extension(x509.SubjectKeyIdentifier.from_public_key(cls.key.public_key()), critical=False)
                  .sign(cls.key, hashes.SHA256()))
        cls.int_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.int_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Fixture Intermediate')])
        cls.intermediate = (x509.CertificateBuilder().subject_name(cls.int_name).issuer_name(cls.ca_name)
                            .public_key(cls.int_key.public_key()).serial_number(x509.random_serial_number())
                            .not_valid_before(cls.now - timedelta(days=2)).not_valid_after(cls.now + timedelta(days=300))
                            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                            .add_extension(x509.SubjectKeyIdentifier.from_public_key(cls.int_key.public_key()), critical=False)
                            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(cls.key.public_key()), critical=False)
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
            self.entry('fixture.example', ['*.fixture.example'])]}}
        # What actually crosses the boundary: the wildcard certificate and key, without the account or other entries.
        self.minimal = json.loads(json.dumps({'letsencrypt': {
            'Certificates': self.payload['letsencrypt']['Certificates']}}))
        self.save()

    def entry(self, name, sans=(), expired=False, key=None, via_intermediate=False):
        key = key or self.key
        signer, issuer, authority = ((self.int_key, self.int_name, self.int_key) if via_intermediate
                                     else (self.key, self.ca_name, self.key))
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(issuer).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(self.now - timedelta(days=2))
                .not_valid_after(self.now + timedelta(days=-1 if expired else 80))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(item) for item in (name, *sans)]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(signer, hashes.SHA256()))
        chain_pem = leaf.public_bytes(serialization.Encoding.PEM)
        if via_intermediate:
            chain_pem += self.intermediate.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        return {'domain': {'main': name, 'sans': list(sans)}, 'certificate': base64.b64encode(chain_pem).decode(),
                'key': base64.b64encode(key_pem).decode(), 'Store': 'default'}

    def save(self):
        self.source.write_text(json.dumps(self.payload))
        self.source.chmod(0o600)

    def fake_iso(self, data, output):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(data)
        return Path(output)

    def test_one_wildcard_certificate_covers_the_domain_and_every_service_name(self):
        result = certificates.validate(self.source, 'Fixture.Example.')
        self.assertEqual(set(result), {'fixture.example', *(f'{service}.fixture.example' for service in certificates.SERVICES)})
        self.assertTrue(all(datetime.fromisoformat(expiry) > self.now for expiry in result.values()))

    def test_legacy_per_service_stores_are_not_accepted(self):
        self.payload['letsencrypt']['Certificates'] = [
            self.entry(f'{service}.fixture.example') for service in certificates.SERVICES]
        self.save()
        with self.assertRaisesRegex(ValueError, 'Missing the pre-issued certificate for fixture.example'):
            certificates.validate(self.source, 'fixture.example')

    def test_the_certificate_must_name_the_wildcard_and_really_cover_it(self):
        entries = self.payload['letsencrypt']['Certificates']
        entries[0] = self.entry('fixture.example')
        self.save()
        with self.assertRaisesRegex(ValueError, r'must also list \*\.fixture\.example'):
            certificates.validate(self.source, 'fixture.example')
        # Metadata that claims the wildcard while the real certificate lacks it is caught by the chain check.
        entries[0] = self.entry('fixture.example') | {'domain': {'main': 'fixture.example', 'sans': ['*.fixture.example']}}
        self.save()
        with self.assertRaisesRegex(ValueError, 'does not have a trusted'):
            certificates.validate(self.source, 'fixture.example')
        # A wildcard for another parent domain does not cover these names either.
        entries[0] = self.entry('fixture.example', ['*.other.example']) | {
            'domain': {'main': 'fixture.example', 'sans': ['*.fixture.example']}}
        self.save()
        with self.assertRaises(ValueError):
            certificates.validate(self.source, 'fixture.example')

    def test_minimal_store_keeps_only_the_domains_certificate_and_key(self):
        extra = self.entry('git.fixture.example')
        self.payload['letsencrypt']['Certificates'].append(extra)
        self.payload['letsencrypt']['Account'] = {'Email': 'a@b.example', 'PrivateKey': 'ACCOUNT_KEY_MATERIAL'}
        self.save()
        trimmed = certificates.minimal_store(self.source.read_bytes(), 'Fixture.Example.')
        self.assertEqual(json.loads(trimmed), self.minimal)
        self.assertNotIn(b'ACCOUNT_KEY_MATERIAL', trimmed)
        self.assertNotIn(extra['certificate'].encode(), trimmed)
        self.assertEqual(len(json.loads(trimmed)['letsencrypt']['Certificates']), 1)
        # An entry without a Store would not be loaded by Traefik, so the default store is always written.
        self.payload['letsencrypt']['Certificates'][0].pop('Store')
        self.save()
        self.assertEqual(json.loads(certificates.minimal_store(self.source.read_bytes(), 'fixture.example'))
                         ['letsencrypt']['Certificates'][0]['Store'], 'default')

    def test_the_full_chain_survives_import_unchanged(self):
        self.payload['letsencrypt']['Certificates'] = [
            self.entry('fixture.example', ['*.fixture.example'], via_intermediate=True)]
        self.save()
        destination = self.directory / 'imported/acme.json'
        certificates.import_file(self.source, 'fixture.example', destination)
        stored = json.loads(destination.read_bytes())['letsencrypt']['Certificates'][0]
        self.assertEqual(stored['certificate'], self.payload['letsencrypt']['Certificates'][0]['certificate'])
        self.assertEqual(len(x509.load_pem_x509_certificates(base64.b64decode(stored['certificate']))), 2)

    def test_file_import_is_private_atomic_and_rejects_bad_input_without_replacement(self):
        destination = self.directory / 'imported/acme.json'
        certificates.import_file(self.source, 'fixture.example', destination)
        self.assertEqual(json.loads(destination.read_bytes()), self.minimal)
        self.assertNotIn(b'"Account"', destination.read_bytes())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        bad = self.directory / 'bad.json'
        link = self.directory / 'link.json'
        link.symlink_to(self.source)
        huge = self.directory / 'huge.json'
        huge.write_bytes(b' ' * (certificates.MAX_FILE + 1))
        bad.write_text('not json')
        arrays = self.directory / 'array.json'
        arrays.write_text('[]')
        accountonly = self.directory / 'account.json'
        accountonly.write_text('{"letsencrypt":{"Account":{}}}')
        for invalid in (bad, link, huge, arrays, accountonly, self.directory / 'missing.json'):
            with self.subTest(file=invalid.name), self.assertRaises(ValueError):
                certificates.import_file(invalid, 'fixture.example', destination)
            self.assertEqual(json.loads(destination.read_bytes()), self.minimal)
        with self.assertRaises(ValueError):
            certificates.import_file(self.source, 'wrong.example', destination)
        self.assertEqual(json.loads(destination.read_bytes()), self.minimal)

    @unittest.skipUnless(shutil.which('xorriso'), 'xorriso is only in the tools container')
    def test_the_secret_iso_has_its_label_and_carries_acme_json(self):
        data = certificates.minimal_store(self.source.read_bytes(), 'fixture.example')
        iso = certificates.write_certificate_iso(data, self.directory / 'secrets/sophon-certificates.iso')
        self.assertEqual(stat.S_IMODE(iso.stat().st_mode), 0o600)
        with iso.open('rb') as stream:
            stream.seek(32808)
            self.assertEqual(stream.read(32).strip(), certificates.CERTIFICATE_LABEL.encode())
        extracted = self.directory / 'extracted'
        subprocess.run(['xorriso', '-osirrox', 'on', '-indev', str(iso), '-extract', '/', str(extracted)],
                       check=True, capture_output=True)
        self.assertEqual((extracted / 'acme.json').read_bytes(), data)
        self.assertEqual(sorted(path.name for path in extracted.iterdir()), ['acme.json'])
        self.assertFalse(list(iso.parent.glob('.sophon-certs-*')))
        link = self.directory / 'link.iso'
        link.symlink_to(iso)
        with self.assertRaises(ValueError):
            certificates.write_certificate_iso(data, link)

    def test_ready_to_export_requires_the_wildcard_certificate_to_be_valid_for_30_days(self):
        self.assertTrue(certificates.ready_to_export(self.source, 'fixture.example'))
        self.payload['letsencrypt']['Certificates'] = [
            self.entry(f'{service}.fixture.example') for service in certificates.SERVICES]
        self.save()
        self.assertFalse(certificates.ready_to_export(self.source, 'fixture.example'))
        self.payload['letsencrypt']['Certificates'] = [self.entry('fixture.example', ['*.fixture.example'], expired=True)]
        self.save()
        self.assertFalse(certificates.ready_to_export(self.source, 'fixture.example'))

    def test_issue_command_reuses_valid_store_and_writes_the_secret_iso_without_printing_it(self):
        state = self.directory / 'state' / 'fixture.example'
        state.mkdir(parents=True, mode=0o700)
        (state / 'acme.json').write_bytes(self.source.read_bytes())
        (state / 'acme.json').chmod(0o600)
        iso = self.directory / 'out/sophon-certificates.iso'
        extra = self.directory / 'out/acme.json'
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as progress, \
                patch('subprocess.Popen') as process, \
                patch('certificates.write_certificate_iso', side_effect=self.fake_iso) as write, \
                patch('getpass.getpass') as secret:
            self.assertEqual(certificates.main([
                'issue', '--domain', 'fixture.example', '--email', 'admin@fixture.example',
                '--state-dir', str(self.directory / 'state'), '--output-iso', str(iso),
                '--output-file', str(extra)]), 0)
        self.assertEqual(output.getvalue(), '')
        self.assertIn(str(iso), progress.getvalue())
        self.assertEqual(json.loads(write.call_args.args[0]), self.minimal)
        self.assertEqual(json.loads(extra.read_bytes()), self.minimal)
        self.assertEqual(stat.S_IMODE(extra.stat().st_mode), 0o600)
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
        iso = self.directory / 'secrets/sophon-certificates.iso'
        with patch.dict(os.environ, environment, clear=True), \
                patch('getpass.getpass', return_value='FAKE_CLOUDFLARE_TOKEN') as secret, \
                patch('builtins.input', side_effect=['fixture.example', 'admin@fixture.example']), \
                patch('certificates.write_certificate_iso', side_effect=self.fake_iso) as write, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as progress:
            self.assertEqual(certificates.main([
                'issue', '--state-dir', str(self.directory / 'issued'), '--timeout', '10',
                '--output-iso', str(iso)]), 0)
        invocation = json.loads(received.read_text())
        self.assertNotIn('FAKE_CLOUDFLARE_TOKEN', ' '.join(invocation['args']) + output.getvalue() + progress.getvalue())
        self.assertEqual(invocation['token'], 'FAKE_CLOUDFLARE_TOKEN')
        self.assertEqual(json.loads(write.call_args.args[0]), self.minimal)
        # The issuer asks for EC256 and one wildcard certificate, not one certificate per service name.
        self.assertIn('--certificatesresolvers.letsencrypt.acme.keyType=EC256', invocation['args'])
        routers = json.loads((self.directory / 'issued/fixture.example/routers.json').read_text())['http']['routers']
        self.assertEqual(list(routers), ['wildcard'])
        self.assertEqual(routers['wildcard']['tls']['domains'], [{'main': 'fixture.example', 'sans': ['*.fixture.example']}])
        self.assertEqual(routers['wildcard']['tls']['certResolver'], 'letsencrypt')
        with self.assertRaises(ProcessLookupError):
            os.kill(invocation['pid'], 0)
        private = self.directory / 'issued/fixture.example'
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((private / 'issuer.log').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((private / 'acme.json').stat().st_mode), 0o600)

    def test_invalid_issuer_inputs_never_export_a_disk(self):
        for arguments in (['--domain', '../../escape'], ['--domain', 'fixture.example', '--timeout', '0']):
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()), \
                    patch('certificates.write_certificate_iso') as write, self.assertRaises(SystemExit) as error:
                certificates.main(['issue', '--state-dir', str(self.directory), *arguments])
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(output.getvalue(), '')
            write.assert_not_called()

    def test_failed_issuer_exports_nothing_and_keeps_its_log_private(self):
        executable = self.directory / 'traefik'
        executable.write_text(f'#!{sys.executable}\nprint("FAKE_PRIVATE_ISSUER_DETAIL")\nraise SystemExit(27)\n')
        executable.chmod(0o700)
        with patch.dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ['PATH'],
                        CF_DNS_API_TOKEN='FAKE_TOKEN'), \
                patch('certificates.write_certificate_iso') as write, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as stopped:
            certificates.main(['issue', '--domain', 'fixture.example', '--email', 'admin@fixture.example',
                               '--state-dir', str(self.directory / 'failed')])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        write.assert_not_called()
        self.assertIn('issuer exited', error.getvalue())
        self.assertNotIn('FAKE_PRIVATE_ISSUER_DETAIL', error.getvalue())
        self.assertEqual(stat.S_IMODE((self.directory / 'failed/fixture.example/issuer.log').stat().st_mode), 0o600)

    def test_service_file_is_staged_and_its_imported_copy_removed_but_never_forwarded(self):
        import deployment
        from test_console import service_values
        root = self.directory / 'project'
        (root / 'imports').mkdir(parents=True)
        staged_copy = root / 'imports/acme.json'
        shutil.copy(self.source, staged_copy)
        settings = service_values() | {'SOPHON_DOMAIN': 'fixture.example', 'SOPHON_ACME_FILE': str(staged_copy)}
        current = deployment.new_control(settings, 'configure')
        self.assertNotIn('SOPHON_ACME_FILE', deployment.SITE_FIELDS)
        self.assertNotIn('SOPHON_ACME_FILE', deployment.step_fields('provision'))
        values, error = deployment.validate_settings(current['values'], {}, 'configure')
        self.assertIsNone(error)
        with patch('deployment.ROOT', root):
            staged = deployment.stage_certificates(values)
        self.assertNotIn('SOPHON_ACME_FILE', staged)
        self.assertEqual(json.loads((root / 'artifacts/traefik/acme.json').read_bytes()), self.minimal)
        self.assertFalse(staged_copy.exists(), 'the secret disk copy must not be left in the project')
        self.assertTrue(self.source.exists(), 'files outside the imports folder are never deleted')
        with patch.dict(os.environ, {'SOPHON_ACME_FILE': str(self.source)}), \
                patch('deployment.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(deployment.launch(['4-configure.yml']), 0)
        self.assertNotIn('SOPHON_ACME_FILE', run.call_args.kwargs['env'])

    def test_an_unusable_certificate_file_is_rejected_with_its_path(self):
        import deployment
        from test_console import service_values
        broken = self.directory / 'broken.json'
        broken.write_text('{"letsencrypt":{"Account":{}}}')
        settings = service_values() | {'SOPHON_DOMAIN': 'fixture.example', 'SOPHON_ACME_FILE': str(broken)}
        values, error = deployment.validate_settings(deployment.new_control(settings, 'configure')['values'], {}, 'configure')
        self.assertIsNone(values)
        self.assertIn(str(broken), error)

    def test_wizard_uses_the_staged_file_and_deploys_without_a_final_confirmation(self):
        import console
        from test_console import service_values
        root = self.directory / 'deployed'
        settings = service_values() | {'SOPHON_DOMAIN': 'fixture.example', 'SOPHON_ACME_FILE': str(self.source)}
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return 'y' if prompt.startswith('Is this site air-gapped') else ''

        with patch.dict(os.environ, settings, clear=False), \
                patch('console.deployment.ROOT', root), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=0) as run, \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('console.getpass.getpass', return_value=''), \
                patch('builtins.input', side_effect=answer), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(console.main([]), 0)
            self.assertFalse(os.environ.get('SOPHON_ACME_FILE'), 'Wizard forwarded the certificate file path')
        self.assertTrue([prompt for prompt in prompts if prompt.startswith('Pre-issued certificates file')])
        self.assertFalse([prompt for prompt in prompts if prompt.startswith('Deploy')])
        run.assert_called_once()
        self.assertEqual(json.loads((root / 'artifacts/traefik/acme.json').read_bytes()), self.minimal)

    def test_air_gapped_wizard_explains_a_missing_disk_and_asks_for_a_real_file(self):
        import console
        from test_console import service_values
        root = self.directory / 'deployed'
        settings = service_values() | {'SOPHON_DOMAIN': 'fixture.example'}
        settings.pop('SOPHON_ACME_FILE', None)
        replies = iter(['y', '', str(self.directory / 'nope.json'), str(self.source)])

        def answer(prompt):
            return next(replies) if prompt.startswith(('Is this site', 'Pre-issued')) else ''

        with patch.dict(os.environ, settings, clear=False), \
                patch('console.deployment.ROOT', root), \
                patch('console.deployment.load_site_settings', return_value={}), \
                patch('console.deployment.main', return_value=0), \
                patch('console.sys.stdin.isatty', return_value=True), \
                patch('console.getpass.getpass', return_value=''), \
                patch('builtins.input', side_effect=answer), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            os.environ.pop('SOPHON_ACME_FILE', None)
            self.assertEqual(console.main([]), 0)
        text = output.getvalue()
        self.assertIn('SOPHON_CERTS', text)
        self.assertIn('This value is required.', text)
        self.assertIn('File not found', text)
        self.assertEqual(json.loads((root / 'artifacts/traefik/acme.json').read_bytes()), self.minimal)

    def test_missing_expired_and_wrong_key_certificates_fail(self):
        entries = self.payload['letsencrypt']['Certificates']
        wrong_key = self.other_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption())
        for replacement in (
            self.entry('fixture.example', ['*.fixture.example'], expired=True),
            entries[0] | {'key': base64.b64encode(wrong_key).decode()},
        ):
            entries[0:1] = [replacement]
            self.save()
            with self.assertRaises(ValueError):
                certificates.validate(self.source, 'fixture.example')
        entries.clear()
        self.save()
        with self.assertRaisesRegex(ValueError, 'Missing the pre-issued'):
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
                         {'fixture.example', *(f'{service}.fixture.example' for service in certificates.SERVICES)})
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
                    result = run_fixture(directory, [
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
