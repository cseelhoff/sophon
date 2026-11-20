import base64
import datetime as dt
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

import certificates
import secrets_iso


def make_test_acme(domain: str) -> tuple[bytes, bytes]:
    """Create a valid RFC-5280 CA + wildcard leaf Traefik acme.json for tests."""
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Sophon Test Root CA')])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=60))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=60))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain), x509.DNSName(f'*.{domain}')]),
            critical=False,
        )
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_chain_pem = (
        leaf_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    store = {
        'letsencrypt': {
            'Certificates': [
                {
                    'domain': {'main': domain, 'sans': [f'*.{domain}']},
                    'certificate': base64.b64encode(cert_chain_pem).decode('ascii'),
                    'key': base64.b64encode(key_pem).decode('ascii'),
                }
            ]
        }
    }
    return json.dumps(store).encode('utf-8'), ca_cert.public_bytes(serialization.Encoding.PEM)


class SecretsIsoTests(unittest.TestCase):
    def test_sha512_crypt_matches_openssl_passwd(self):
        pw = 'SophonStrongPass123!'
        salt = 'Salt1234567890Ab'
        ours = secrets_iso.sha512_crypt(pw, salt)
        ref = subprocess.check_output(['openssl', 'passwd', '-6', '-salt', salt, pw], text=True).strip()
        self.assertEqual(ours, ref)

    def test_paired_sftp_keys_persist_and_format_known_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            m1 = secrets_iso.ensure_paired_sftp_keys(state, '10.0.60.100', 22)
            m2 = secrets_iso.ensure_paired_sftp_keys(state, '10.0.60.100', 22)
            self.assertEqual(m1['host_private_key'], m2['host_private_key'])
            self.assertEqual(m1['client_backup_private_key'], m2['client_backup_private_key'])
            self.assertTrue(m1['known_hosts'].startswith(b'10.0.60.100 ssh-ed25519 '))
            self.assertTrue(m1['authorized_keys_backup'].startswith(b'restrict ssh-ed25519 '))
            self.assertTrue(m1['authorized_keys_restore'].endswith(b' sophon-restore\n'))

            m_port = secrets_iso.ensure_paired_sftp_keys(state, '10.0.60.100', 2222)
            self.assertTrue(m_port['known_hosts'].startswith(b'[10.0.60.100]:2222 ssh-ed25519 '))

    def test_main_builds_both_private_isos_with_expected_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            acme_bytes, ca_pem = make_test_acme('example.test')
            ca_file = tmp_dir / 'ca.pem'
            ca_file.write_bytes(ca_pem)
            acme_file = tmp_dir / 'acme.json'
            acme_file.write_bytes(acme_bytes)

            infra_iso = tmp_dir / 'sophon-secrets.iso'
            backup_iso = tmp_dir / 'sophon-sftp-secrets.iso'
            state_dir = tmp_dir / 'state'

            env = {
                'SSL_CERT_FILE': str(ca_file),
                'SOPHON_SHARED_PASSWORD': 'SophonSharedPassword123!',
                'SOPHON_DOMAIN': 'example.test',
                'SOPHON_NETWORK_CIDR': '10.0.60.101/24',
                'SOPHON_NETWORK_GATEWAY': '10.0.60.1',
                'SOPHON_VNET_DNS': '10.0.60.1',
                'SOPHON_DEV_NETWORK_CIDR': '10.0.60.107/24',
                'SOPHON_BACKUP_NETWORK_CIDR': '10.0.60.100/24',
                'SOPHON_BACKUP_NETWORK_GATEWAY': '10.0.60.1',
                'SOPHON_KOPIA_PASSWORD': 'KopiaRepoPassword123!',
            }
            old_env = os.environ.copy()
            try:
                os.environ.update(env)
                rc = secrets_iso.main([
                    '--run',
                    '--with-backup',
                    '--acme-file', str(acme_file),
                    '--output-iso', str(infra_iso),
                    '--backup-output-iso', str(backup_iso),
                    '--state-dir', str(state_dir),
                ])
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(rc, 0)
            self.assertEqual(stat.S_IMODE(infra_iso.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup_iso.stat().st_mode), 0o600)

            extracted = tmp_dir / 'extracted-infra'
            extracted.mkdir()
            subprocess.run(
                ['xorriso', '-osirrox', 'on', '-indev', str(infra_iso), '-extract', '/', str(extracted)],
                check=True, capture_output=True,
            )
            seed = json.loads((extracted / 'sophon-seed.json').read_text())
            self.assertEqual(seed['target'], 'prod')
            self.assertTrue(seed['core_password_hash'].startswith('$6$'))
            self.assertEqual(seed['network'], {'mode': 'static', 'cidr': '10.0.60.101/24', 'gateway': '10.0.60.1', 'dns': '10.0.60.1'})
            self.assertEqual(seed['dev_network'], {'mode': 'static', 'cidr': '10.0.60.107/24', 'gateway': '10.0.60.1', 'dns': '10.0.60.1'})
            self.assertEqual(seed['env']['SOPHON_KOPIA_SFTP_HOST'], '10.0.60.100')
            self.assertEqual(seed['env']['SOPHON_KOPIA_SFTP_USERNAME'], 'sophon-backup')
            self.assertIn('backup_private_key_b64', seed['sftp'])
            self.assertIn('restore_private_key_b64', seed['sftp'])
            self.assertNotEqual(seed['sftp']['backup_private_key_b64'], seed['sftp']['restore_private_key_b64'])
            self.assertTrue((extracted / 'acme.json').is_file())

            derived_iso = tmp_dir / 'derived-dev.iso'
            secrets_iso.derive_dev_secrets_iso(infra_iso, {}, derived_iso)
            extracted_dev = tmp_dir / 'extracted-dev'
            extracted_dev.mkdir()
            subprocess.run(
                ['xorriso', '-osirrox', 'on', '-indev', str(derived_iso), '-extract', '/', str(extracted_dev)],
                check=True, capture_output=True,
            )
            dev_seed = json.loads((extracted_dev / 'sophon-seed.json').read_text())
            self.assertEqual(dev_seed['target'], 'dev')
            self.assertEqual(dev_seed['network']['cidr'], '10.0.60.107/24')
            self.assertEqual(dev_seed['prod_network']['cidr'], '10.0.60.101/24')
            self.assertEqual(dev_seed['env']['SOPHON_INFRAVM_IP'], '10.0.60.107')
            self.assertEqual(dev_seed['env']['SOPHON_KOPIA_SFTP_USERNAME'], 'sophon-restore')
            self.assertEqual(dev_seed['env']['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64'], seed['sftp']['restore_private_key_b64'])
            self.assertEqual(dev_seed['sftp']['backup_private_key_b64'], seed['sftp']['backup_private_key_b64'])
            self.assertTrue((extracted_dev / 'acme.json').is_file())

            extracted_bk = tmp_dir / 'extracted-bk'
            extracted_bk.mkdir()
            subprocess.run(
                ['xorriso', '-osirrox', 'on', '-indev', str(backup_iso), '-extract', '/', str(extracted_bk)],
                check=True, capture_output=True,
            )
            for fname in ('admin.shadow', 'network.env', 'ssh_host_ed25519_key', 'authorized_keys_backup', 'authorized_keys_restore'):
                self.assertTrue((extracted_bk / fname).is_file(), fname)
            self.assertIn('IP=10.0.60.100', (extracted_bk / 'network.env').read_text())

    def test_default_build_auto_enables_backup_and_preserves_cf_token_with_cached_acme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            acme_bytes, ca_pem = make_test_acme('sophon.example.com')
            ca_file = tmp_dir / 'ca.pem'
            ca_file.write_bytes(ca_pem)
            state_dir = tmp_dir / 'state'
            cert_dir = state_dir / 'certificates' / 'sophon.example.com'
            cert_dir.mkdir(parents=True)
            (cert_dir / 'acme.json').write_bytes(acme_bytes)
            sophon_iso = tmp_dir / 'sophon-secrets.iso'
            backup_iso = tmp_dir / 'sophon-sftp-secrets.iso'

            old_env = os.environ.copy()
            try:
                for key in list(os.environ):
                    if key.startswith('SOPHON_') or key == 'CF_DNS_API_TOKEN':
                        os.environ.pop(key, None)
                os.environ.update({
                    'SSL_CERT_FILE': str(ca_file),
                    'SOPHON_DOMAIN': 'sophon.example.com',
                    'SOPHON_ACME_EMAIL': 'ops@sophon.example.com',
                    'SOPHON_CF_TOKEN': 'cf-live-token-123456',
                    'SOPHON_SHARED_PASSWORD': 'SharedTestPassword!123',
                    'SOPHON_KOPIA_PASSWORD': 'KopiaRepoPassword!123',
                    'SOPHON_NETWORK_CIDR': '10.0.60.101/24',
                    'SOPHON_NETWORK_GATEWAY': '10.0.60.1',
                    'SOPHON_BACKUP_NETWORK_CIDR': '10.0.60.100/24',
                })
                rc = secrets_iso.main([
                    '--state-dir', str(state_dir),
                    '--output-iso', str(sophon_iso),
                    '--backup-output-iso', str(backup_iso),
                ])
                self.assertEqual(rc, 0)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertTrue(sophon_iso.is_file())
            self.assertTrue(backup_iso.is_file())
            extracted = tmp_dir / 'extracted'
            extracted.mkdir()
            subprocess.run(
                ['xorriso', '-osirrox', 'on', '-indev', str(sophon_iso), '-extract', '/', str(extracted)],
                check=True, capture_output=True,
            )
            seed = json.loads((extracted / 'sophon-seed.json').read_text())
            self.assertEqual(seed['env']['SOPHON_CF_TOKEN'], 'cf-live-token-123456')
            self.assertEqual(seed['env']['SOPHON_CF_TUNNEL_ENABLED'], 'true')
            self.assertEqual(seed['env']['SOPHON_ACME_EMAIL'], 'ops@sophon.example.com')
            self.assertTrue((extracted / 'acme.json').is_file())


if __name__ == '__main__':
    unittest.main()
