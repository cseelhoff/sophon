import base64
import datetime
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import yaml

from test_deployment import ROOT, run_fixture


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required to render the LDAP stack')
class OpenLDAPTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        variables = yaml.safe_load((ROOT / 'roles/openldap/defaults/main.yml').read_text())
        variables.update(
            domain_name='site.example', openldap_admin_password='Fixture-root-password!',
            openldap_example_user_password='Fixture-user-password!$quote\'"',
            openldap_example_admin_password='Fixture-admin-password!')
        output = cls.directory / 'compose.yml'
        result = run_fixture(cls.directory, [{
            'ansible.builtin.template': {
                'src': str(ROOT / 'roles/openldap/templates/docker-compose.yml.j2'), 'dest': str(output)},
            'vars': variables,
        }])
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.compose = yaml.safe_load(output.read_text())
        cls.service = cls.compose['services']['ldap']

    def test_uses_vendor_bootstrap_without_sample_accounts_or_bitnami_paths(self):
        self.assertEqual(self.service['image'], 'docker.io/vegardit/openldap:2.6.10')
        script = self.service['command'][0]
        self.assertNotIn('/opt/bitnami', script)
        self.assertNotIn('|| true', script)
        self.assertIn('exec /bin/bash /opt/run.sh', script)
        self.assertIn('groupOfUniqueNames/groupOfNames', script)
        self.assertIn('cn=portainer-admins,ou=groups,dc=site,dc=example', script)
        self.assertIn('member: uid=john.doe.admin,ou=people,dc=site,dc=example', script)
        self.assertNotIn('Fixture-user-password!', script)
        self.assertIn('openldap_config:/etc/ldap/slapd.d', self.service['volumes'])
        self.assertIn('openldap_data:/var/lib/sophon-legacy:ro', self.service['volumes'])
        self.assertIn('--version v3', self.compose['services']['certs-dumper']['command'][0])

    @unittest.skipUnless(os.environ.get('SOPHON_CONTAINER_TESTS') == '1' and shutil.which('docker'),
                         'Set SOPHON_CONTAINER_TESTS=1 with the LDAP image preloaded for live tests')
    def test_live_tls_seeded_accounts_membership_hashes_and_restart(self):
        container = self.start_container()
        self.wait_ready(container)
        self.ldap(container, 'ldapwhoami', '-D', 'uid=john.doe,ou=people,dc=site,dc=example',
                  '-w', 'Fixture-user-password!$quote\'"')
        self.ldap(container, 'ldapwhoami', '-D', 'cn=keycloak-bind,ou=services,dc=site,dc=example',
                  '-w', 'Fixture-root-password!')
        entries = self.ldap(
            container, 'ldapsearch', '-LLL', '-o', 'ldif-wrap=no',
            '-D', 'cn=admin,dc=site,dc=example', '-w', 'Fixture-root-password!',
            '-b', 'uid=john.doe.admin,ou=people,dc=site,dc=example', 'memberOf', 'userPassword').stdout
        self.assertIn('memberOf: cn=portainer-admins,ou=groups,dc=site,dc=example', entries)
        encoded = next(line.removeprefix('userPassword:: ') for line in entries.splitlines()
                       if line.startswith('userPassword:: '))
        self.assertTrue(base64.b64decode(encoded).startswith(b'{ARGON2}'))
        wrong_password = self.ldap(
            container, 'ldapwhoami', '-D', 'uid=john.doe,ou=people,dc=site,dc=example',
            '-w', 'incorrect', check=False)
        self.assertNotEqual(wrong_password.returncode, 0)
        anonymous = self.ldap(container, 'ldapsearch', '-LLL', '-b', 'dc=site,dc=example', check=False)
        self.assertNotIn('uid=john.doe', anonymous.stdout)
        self.engine('restart', container)
        self.wait_ready(container)
        self.ldap(container, 'ldapwhoami', '-D', 'uid=john.doe,ou=people,dc=site,dc=example',
                  '-w', 'Fixture-user-password!$quote\'"')

    @unittest.skipUnless(os.environ.get('SOPHON_CONTAINER_TESTS') == '1' and shutil.which('docker'),
                         'Set SOPHON_CONTAINER_TESTS=1 with the LDAP image preloaded for live tests')
    def test_live_legacy_data_is_refused_without_modification(self):
        container = self.start_container(legacy=True)
        result = self.engine('wait', container)
        self.assertEqual(result.stdout.strip(), '1')
        logs = self.engine('logs', container)
        self.assertIn('legacy Bitnami data found', logs.stdout + logs.stderr)
        preserved = self.directory / 'preserved-data'
        self.engine('cp', container + ':/var/lib/sophon-legacy/data.mdb', str(preserved))
        self.assertEqual(preserved.read_text(), 'irreplaceable-directory-data')

    def engine(self, *args, check=True):
        return subprocess.run(['docker', *args], capture_output=True, text=True, errors='replace',
                              check=check, timeout=120)

    def start_container(self, legacy=False):
        name = 'sophon-ldap-pin-test-' + uuid.uuid4().hex[:12]
        args = ['create', '--name', name, '--volume', '/var/lib/sophon-legacy',
                '--volume', '/run/secrets/ldap']
        for key, value in self.service['environment'].items():
            args.extend(['--env', f'{key}={str(value).replace("$$", "$")}'])
        args.extend(['--entrypoint', self.service['entrypoint'][0], self.service['image'],
                     *self.service['entrypoint'][1:], self.service['command'][0].replace('$$', '$')])
        self.engine(*args)
        self.addCleanup(self.engine, 'rm', '-f', '-v', name)
        certificates = self.directory / name
        (certificates / 'certs').mkdir(parents=True)
        (certificates / 'private').mkdir()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'ldap.site.example')])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - datetime.timedelta(minutes=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                       .add_extension(x509.SubjectAlternativeName([
                           x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                       .sign(key, hashes.SHA256()))
        (certificates / 'certs/site.example.crt').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        (certificates / 'private/site.example.key').write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        store = certificates / 'acme.json'
        store.write_text(json.dumps({'letsencrypt': {'Certificates': [{
            'domain': {'main': 'site.example', 'sans': ['*.site.example']},
            'certificate': base64.b64encode((certificates / 'certs/site.example.crt').read_bytes()).decode(),
            'key': base64.b64encode((certificates / 'private/site.example.key').read_bytes()).decode(),
            'Store': 'default',
        }]}}))
        dumper = 'sophon-certs-pin-test-' + uuid.uuid4().hex[:12]
        self.engine('create', '--name', dumper, '--volume', '/data', '--volume', '/output',
                    '--entrypoint', '/bin/sh', self.compose['services']['certs-dumper']['image'], '-ec',
                    'traefik-certs-dumper file --source /data/acme.json --dest /output --version v3'
                    ' && chmod -R a+r /output/private/')
        self.addCleanup(self.engine, 'rm', '-f', '-v', dumper)
        self.engine('cp', str(store), dumper + ':/data/acme.json')
        self.engine('start', dumper)
        self.assertEqual(self.engine('wait', dumper).stdout.strip(), '0')
        self.engine('cp', dumper + ':/output/.', str(certificates))
        self.engine('cp', str(certificates) + '/.', name + ':/run/secrets/ldap')
        if legacy:
            marker = self.directory / 'legacy-data'
            marker.write_text('irreplaceable-directory-data')
            self.engine('cp', str(marker), name + ':/var/lib/sophon-legacy/data.mdb')
        self.engine('start', name)
        return name

    def ldap(self, container, command, *args, check=True):
        return self.engine('exec', '--env', 'LDAPTLS_CACERT=/run/secrets/ldap/certs/site.example.crt',
                           container, command, '-x', '-H', 'ldaps://127.0.0.1:636', *args, check=check)

    def wait_ready(self, container):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = self.ldap(container, 'ldapwhoami', '-D', 'cn=admin,dc=site,dc=example',
                              '-w', 'Fixture-root-password!', check=False)
            if ready.returncode == 0:
                return
            if self.engine('inspect', '--format', '{{.State.Running}}', container).stdout.strip() != 'true':
                break
            time.sleep(2)
        self.fail(self.engine('logs', container).stdout + self.engine('logs', container).stderr)


if __name__ == '__main__':
    unittest.main()
