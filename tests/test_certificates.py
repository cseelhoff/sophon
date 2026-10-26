import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
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
sys.path.insert(0, str(ROOT))
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

    def test_private_iso_contains_only_identity_handoff_and_is_not_reusable_bundle(self):
        output = self.directory / 'certificates.iso'
        self.assertEqual(certificates.main(['--source', str(self.source), '--domain', 'fixture.example',
                                            '--output', str(output)]), 0)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        extracted = self.directory / 'extracted'
        subprocess.run(['xorriso', '-osirrox', 'on', '-indev', str(output), '-extract', '/', str(extracted)],
                       check=True, capture_output=True)
        self.assertEqual({path.name for path in extracted.iterdir()}, {'acme.json', 'import.sh'})
        self.assertEqual(json.loads((extracted / 'acme.json').read_bytes()), self.payload)
        script = (extracted / 'import.sh').read_text()
        self.assertNotIn('CF_TOKEN', script)
        self.assertIn('/var/lib/sophon/project', script)
        self.assertIn('--refresh-certificates', script)
        subprocess.run(['bash', '-n', str(extracted / 'import.sh')], check=True)
        with self.assertRaises(ValueError):
            certificates.build_iso(self.source, self.source)


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
