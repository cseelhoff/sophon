import datetime as dt
import http.server
import os
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src'), str(ROOT / 'tests')]

import certificates
import secrets_iso
import verify_restore
from test_secrets_iso import make_test_acme


class VerifyRestoreTests(unittest.TestCase):
    def _build_seed_iso(self, tmp_dir: Path) -> tuple[Path, Path]:
        acme_bytes, ca_pem = make_test_acme('drill.test')
        ca_file = tmp_dir / 'ca.pem'
        ca_file.write_bytes(ca_pem)
        acme_file = tmp_dir / 'acme.json'
        acme_file.write_bytes(acme_bytes)
        iso_path = tmp_dir / 'sophon-dev-secrets.iso'

        env = {
            'SSL_CERT_FILE': str(ca_file),
            'SOPHON_SHARED_PASSWORD': 'SophonSharedPassword123!',
            'SOPHON_DOMAIN': 'drill.test',
            'SOPHON_NETWORK_CIDR': '10.0.60.107/24',
            'SOPHON_NETWORK_GATEWAY': '10.0.60.1',
            'SOPHON_VNET_DNS': '10.0.60.1',
            'SOPHON_KOPIA_SFTP_HOST': '10.0.60.100',
            'SOPHON_KOPIA_PASSWORD': 'KopiaRepoPassword123!',
        }
        old_env = os.environ.copy()
        try:
            os.environ.update(env)
            rc = secrets_iso.main([
                '--run',
                '--readonly-sftp',
                '--acme-file', str(acme_file),
                '--output-iso', str(iso_path),
                '--state-dir', str(tmp_dir / 'state'),
            ])
            self.assertEqual(rc, 0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return iso_path, ca_file

    def test_refuses_production_vm_names_without_override(self):
        for prod_name in ('sophon-infravm', 'sophon-backup'):
            rc = verify_restore.main(['--vm-name', prod_name])
            self.assertEqual(rc, 2)

    def test_rejects_certificate_only_iso_without_seed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            acme_bytes, _ca_pem = make_test_acme('drill.test')
            cert_only_iso = tmp_dir / 'cert-only.iso'
            secrets_iso.write_private_iso({'acme.json': (acme_bytes, 0o400)}, 'SOPHON_CERTS', cert_only_iso)
            rc = verify_restore.main(['--vm-name', 'sophon-dev-infravm', '--secrets-iso', str(cert_only_iso)])
            self.assertEqual(rc, 2)

    def test_hypervisor_agnostic_destroy_and_reprovision_across_all_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso_path, _ca = self._build_seed_iso(Path(tmp))
            for provider in ('proxmox', 'vcenter', 'hyperv'):
                calls = []
                old_hv = os.environ.get('SOPHON_HYPERVISOR')
                try:
                    os.environ['SOPHON_HYPERVISOR'] = provider
                    with mock.patch.object(
                        verify_restore, 'run_playbook',
                        side_effect=lambda pb, env: (calls.append((pb, dict(env))), 0)[1]
                    ):
                        rc = verify_restore.main(['--vm-name', 'sophon-dev-infravm', '--secrets-iso', str(iso_path)])
                finally:
                    if old_hv is None:
                        os.environ.pop('SOPHON_HYPERVISOR', None)
                    else:
                        os.environ['SOPHON_HYPERVISOR'] = old_hv
                self.assertEqual(rc, 0)
                self.assertEqual([c[0] for c in calls], ['3-destroy-vm.yml', '2-provision.yml'])
                self.assertEqual(calls[0][1]['SOPHON_DESTROY_VM_NAME'], 'sophon-dev-infravm')
                self.assertEqual(calls[1][1]['SOPHON_INFRAVM_NAME'], 'sophon-dev-infravm')
                self.assertEqual(Path(calls[1][1]['SOPHON_SECRETS_ISO']), iso_path.resolve())

    def test_sni_https_helper_validates_san_and_rejects_untrusted_cert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            now = dt.datetime.now(dt.timezone.utc)
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'drill.test')])
            cert = (
                x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(hours=1))
                .not_valid_after(now + dt.timedelta(days=1))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName('drill.test'), x509.DNSName('*.drill.test')]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            cert_pem = tmp_dir / 'cert.pem'
            key_pem = tmp_dir / 'key.pem'
            cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_pem.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ))

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')

                def log_message(self, *args):
                    pass

            srv = http.server.HTTPServer(('127.0.0.1', 0), Handler)
            port = srv.server_address[1]
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert_pem), str(key_pem))
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                orig_create = verify_restore.socket.create_connection
                with mock.patch.object(
                    verify_restore.socket, 'create_connection',
                    side_effect=lambda addr, timeout=None: orig_create((addr[0], port), timeout=timeout)
                ):
                    status, body = verify_restore._sni_https_get(
                        '127.0.0.1', 'auth.drill.test', '/realms/master/.well-known/openid-configuration',
                        ca_file=str(cert_pem),
                    )
                    self.assertEqual(status, 200)
                    self.assertIn(b'ok', body)
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        verify_restore._sni_https_get('127.0.0.1', 'wrong.domain', '/', ca_file=str(cert_pem))
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == '__main__':
    unittest.main()
