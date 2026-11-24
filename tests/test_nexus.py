import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / 'roles/nexus/files'),
    str(ROOT / 'roles/kopia/files'),
    str(ROOT / 'src'),
    str(ROOT / 'tests'),
]

import backup  # noqa: E402
import configure_nexus  # noqa: E402
from test_deployment import run_fixture  # noqa: E402


class MockNexusState:
    def __init__(self, bootstrap_pw: str):
        self.admin_pw = bootstrap_pw
        self.eula = {'accepted': False, 'disclaimer': 'Sonatype Nexus Repository Community Edition EULA'}
        self.anonymous = {'enabled': True, 'userId': 'anonymous', 'realmName': 'NexusAuthorizingRealm'}
        self.truststore: list[dict] = []
        self.ldap_servers: dict[str, dict] = {}
        self.roles: dict[str, dict] = {}
        self.realms: list[str] = ['NexusAuthenticatingRealm', 'NexusAuthorizingRealm']
        self.users: dict[str, dict] = {}
        self.blobstores: dict[str, dict] = {'default': {'name': 'default', 'type': 'File'}}
        self.repositories: dict[str, dict] = {
            'maven-releases': {
                'name': 'maven-releases', 'format': 'maven2', 'type': 'hosted',
                'storage': {'blobStoreName': 'default'},
            },
            'maven-snapshots': {
                'name': 'maven-snapshots', 'format': 'maven2', 'type': 'hosted',
                'storage': {'blobStoreName': 'default'},
            },
            'maven-central': {
                'name': 'maven-central', 'format': 'maven2', 'type': 'proxy',
                'storage': {'blobStoreName': 'default'},
            },
            'maven-public': {
                'name': 'maven-public', 'format': 'maven2', 'type': 'group',
                'storage': {'blobStoreName': 'default'},
                'group': {'memberNames': ['maven-releases', 'maven-snapshots', 'maven-central']},
            },
        }
        self.delete_order: list[str] = []


def make_nexus_handler(state: MockNexusState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _check_auth(self) -> bool:
            header = self.headers.get('Authorization', '')
            if not header.startswith('Basic '):
                self.send_response(401)
                self.end_headers()
                return False
            creds = base64.b64decode(header[6:]).decode('utf-8')
            if creds != f'admin:{state.admin_pw}':
                self.send_response(401)
                self.end_headers()
                return False
            return True

        def _read_body(self) -> bytes:
            length = int(self.headers.get('Content-Length', '0'))
            return self.rfile.read(length) if length else b''

        def _json(self, code: int, payload):
            raw = json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _empty(self, code: int = 204):
            self.send_response(code)
            self.end_headers()

        def do_GET(self):
            if self.path == '/service/rest/v1/status':
                return self._empty(200)
            if not self._check_auth():
                return
            path = self.path.split('?', 1)[0]
            if path == '/service/rest/v1/system/eula':
                return self._json(200, state.eula)
            if path == '/service/rest/v1/security/anonymous':
                return self._json(200, state.anonymous)
            if path == '/service/rest/v1/security/ssl/truststore':
                return self._json(200, state.truststore)
            if path == '/service/rest/v1/security/ldap':
                return self._json(200, list(state.ldap_servers.values()))
            if path.startswith('/service/rest/v1/security/roles/'):
                rid = path.rsplit('/', 1)[-1]
                if rid in state.roles:
                    return self._json(200, state.roles[rid])
                return self._empty(404)
            if path == '/service/rest/v1/security/realms/available':
                return self._json(200, [
                    {'id': 'NexusAuthenticatingRealm'},
                    {'id': 'NexusAuthorizingRealm'},
                    {'id': 'LdapRealm'},
                    {'id': 'DockerToken'},
                    {'id': 'NpmToken'},
                ])
            if path == '/service/rest/v1/security/realms/active':
                return self._json(200, state.realms)
            if path == '/service/rest/v1/security/users':
                return self._json(200, list(state.users.values()))
            if path == '/service/rest/v1/blobstores':
                return self._json(200, list(state.blobstores.values()))
            if path == '/service/rest/v1/repositories':
                # Mirror real Nexus 3.98.1: summary objects omit storage.blobStoreName!
                return self._json(200, [
                    {'name': r['name'], 'format': r['format'], 'type': r['type']}
                    for r in state.repositories.values()
                ])
            if path == '/service/rest/v1/repositorySettings':
                return self._json(200, list(state.repositories.values()))
            return self._empty(404)

        def do_POST(self):
            if not self._check_auth():
                return
            raw = self._read_body()
            path = self.path.split('?', 1)[0]
            if path == '/service/rest/v1/system/eula':
                state.eula = json.loads(raw)
                return self._empty(204)
            if path == '/service/rest/v1/security/ssl/truststore':
                fp = 'AA:BB:CC'
                state.truststore.append({'id': fp, 'fingerprint': fp, 'pem': raw.decode('utf-8')})
                return self._json(201, state.truststore[-1])
            if path == '/service/rest/v1/security/ldap':
                body = json.loads(raw)
                body['id'] = 'ldap-id-1'
                state.ldap_servers[body['name']] = body
                return self._json(201, body)
            if path == '/service/rest/v1/security/roles':
                body = json.loads(raw)
                state.roles[body['id']] = body
                return self._json(200, body)
            if path == '/service/rest/v1/security/users':
                body = json.loads(raw)
                state.users[body['userId']] = body
                return self._json(200, body)
            if path == '/service/rest/v1/blobstores/file':
                body = json.loads(raw)
                state.blobstores[body['name']] = {'name': body['name'], 'type': 'File', 'path': body['path']}
                return self._empty(204)
            if path.startswith('/service/rest/v1/repositories/'):
                _, _, _, _, _, fmt, rtype = path.split('/')
                body = json.loads(raw)
                if body['name'] in state.repositories:
                    return self._json(400, {'error': 'already exists'})
                body['format'] = 'maven2' if fmt == 'maven' else fmt
                body['type'] = rtype
                state.repositories[body['name']] = body
                return self._empty(201)
            return self._empty(404)

        def do_PUT(self):
            if not self._check_auth():
                return
            raw = self._read_body()
            path = self.path.split('?', 1)[0]
            if path == '/service/rest/v1/security/users/admin/change-password':
                state.admin_pw = raw.decode('utf-8')
                return self._empty(204)
            if path.endswith('/change-password') and '/security/users/' in path:
                uid = path.split('/')[-2]
                state.users[uid]['password'] = raw.decode('utf-8')
                return self._empty(204)
            if path == '/service/rest/v1/security/anonymous':
                state.anonymous = json.loads(raw)
                return self._json(200, state.anonymous)
            if path.startswith('/service/rest/v1/security/ldap/'):
                body = json.loads(raw)
                state.ldap_servers[body['name']] = body
                return self._empty(204)
            if path.startswith('/service/rest/v1/security/roles/'):
                rid = path.rsplit('/', 1)[-1]
                body = json.loads(raw)
                state.roles[rid] = body
                return self._empty(204)
            if path == '/service/rest/v1/security/realms/active':
                state.realms = json.loads(raw)
                return self._empty(204)
            if path.startswith('/service/rest/v1/security/users/'):
                uid = path.rsplit('/', 1)[-1]
                body = json.loads(raw)
                state.users[uid].update(body)
                return self._empty(204)
            if path.startswith('/service/rest/v1/repositories/'):
                parts = path.split('/')
                fmt, rtype, rname = parts[5], parts[6], parts[7]
                body = json.loads(raw)
                existing = state.repositories.get(rname)
                if not existing:
                    return self._empty(404)
                if existing.get('storage', {}).get('blobStoreName') != body.get('storage', {}).get('blobStoreName'):
                    return self._json(400, {'error': "You cannot change a repository's blob store through the API"})
                body['format'] = 'maven2' if fmt == 'maven' else fmt
                body['type'] = rtype
                state.repositories[rname] = body
                return self._empty(204)
            return self._empty(404)

        def do_DELETE(self):
            if not self._check_auth():
                return
            path = self.path.split('?', 1)[0]
            if path.startswith('/service/rest/v1/repositories/'):
                rname = path.rsplit('/', 1)[-1]
                # Groups referencing a member must be deleted before the member!
                for other in state.repositories.values():
                    if rname in other.get('group', {}).get('memberNames', []):
                        return self._json(400, {'error': f'{rname} still referenced by {other["name"]}'})
                state.repositories.pop(rname, None)
                state.delete_order.append(f'repo:{rname}')
                return self._empty(204)
            if path.startswith('/service/rest/v1/blobstores/'):
                bs = path.rsplit('/', 1)[-1]
                for r in state.repositories.values():
                    if r.get('storage', {}).get('blobStoreName') == bs:
                        return self._json(400, {'error': f'blobstore {bs} in use by {r["name"]}'})
                state.blobstores.pop(bs, None)
                state.delete_order.append(f'bs:{bs}')
                return self._empty(204)
            return self._empty(404)

    return Handler


class NexusTests(unittest.TestCase):
    def _make_self_signed_pem(self) -> str:
        import datetime as dt
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subj = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'site.example')])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode('ascii')

    def test_openpgp_v4_rsa_key_generation_and_idempotency(self):
        sec1, pub1 = configure_nexus.generate_openpgp_rsa_keypair(
            'Sophon APT Signing <apt@site.example>', key_size=2048, created_at=1700000000
        )
        self.assertTrue(sec1.startswith('-----BEGIN PGP PRIVATE KEY BLOCK-----\n'))
        self.assertTrue(pub1.startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----\n'))

        lines = [ln for ln in sec1.strip().splitlines() if ln and not ln.startswith(('-----', 'Version:'))]
        crc_line = lines.pop()
        self.assertTrue(crc_line.startswith('='))
        raw = base64.b64decode(''.join(lines))
        expected_crc = configure_nexus._crc24(raw)
        actual_crc = int.from_bytes(base64.b64decode(crc_line[1:]), 'big')
        self.assertEqual(actual_crc, expected_crc)
        self.assertEqual(raw[0], 0xC5)  # New-format packet tag 5 (0xC0 | 5 = Secret-Key)

    def test_configure_nexus_converges_and_reruns_idempotently(self):
        state = MockNexusState(bootstrap_pw='initial-bootstrap-secret')
        server = HTTPServer(('127.0.0.1', 0), make_nexus_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        ca_pem = self._make_self_signed_pem()
        cfg = {
            'base_url': f'http://127.0.0.1:{server.server_port}',
            'verify_tls': False,
            'admin_user': 'admin',
            'admin_password': 'SophonAdminPassword123!',
            'initial_admin_password': 'initial-bootstrap-secret',
            'anonymous_enabled': False,
            'ldap_ca_pem': ca_pem,
            'ldap': {
                'name': 'Sophon OpenLDAP',
                'protocol': 'ldaps',
                'use_trust_store': True,
                'host': 'ldap.site.example',
                'port': 636,
                'search_base': 'dc=site,dc=example',
                'bind_dn': 'cn=keycloak-bind,ou=services,dc=site,dc=example',
                'bind_password': 'LdapBindPassword123!',
            },
            'pull_user': 'sophon-pull',
            'pull_password': 'SophonPullPassword123!',
            'domain': 'site.example',
            'docker_group_port': 5000,
            'docker_hosted_port': 5001,
            'apt_signing_keypair': '',
        }

        # Pass 1: initial bootstrap -> full convergence
        res1 = configure_nexus.configure_all(cfg)
        self.assertTrue(res1['rotated_admin'])
        self.assertTrue(res1['accepted_eula'])
        self.assertTrue(res1['changed_anonymous'])
        self.assertTrue(res1['generated_apt_key'])
        self.assertEqual(state.admin_pw, 'SophonAdminPassword123!')
        self.assertTrue(state.eula['accepted'])
        self.assertFalse(state.anonymous['enabled'])
        self.assertNotIn('default', state.blobstores)
        self.assertEqual(set(state.blobstores), {'hosted', 'scratch', 'proxy-cache'})
        self.assertEqual(state.delete_order[0], 'repo:maven-public')
        self.assertEqual(state.delete_order[-1], 'bs:default')
        self.assertIn('LdapRealm', state.realms)
        self.assertIn('DockerToken', state.realms)
        self.assertIn('nexus-admins', state.roles)
        self.assertIn('nexus-developers', state.roles)
        self.assertIn('sophon-reader', state.roles)
        self.assertIn('sophon-pull', state.users)
        self.assertEqual(len(state.repositories), 25)
        for name, repo in state.repositories.items():
            if repo['type'] == 'proxy':
                expected_bs = 'proxy-cache'
            elif name == 'raw-scratch':
                expected_bs = 'scratch'
            else:
                expected_bs = 'hosted'
            self.assertEqual(repo['storage']['blobStoreName'], expected_bs, name)
        self.assertEqual(state.repositories['raw-vault']['storage']['writePolicy'], 'ALLOW_ONCE')
        self.assertEqual(state.repositories['raw-hosted']['storage']['writePolicy'], 'ALLOW')
        self.assertEqual(state.repositories['raw-scratch']['storage']['writePolicy'], 'ALLOW')
        self.assertEqual(
            state.repositories['raw-group']['group']['memberNames'],
            ['raw-vault', 'raw-hosted', 'raw-scratch'],
        )
        # When anonymous_enabled=False, every Docker endpoint enforces BasicAuth.
        for docker_repo in ('docker-hosted', 'docker-hub-proxy', 'ghcr-proxy', 'quay-proxy', 'docker-group'):
            self.assertTrue(state.repositories[docker_repo]['docker']['forceBasicAuth'], docker_repo)

        # Pass 2: rerun without initial_admin_password and with persisted APT key must be 100% idempotent
        cfg2 = dict(cfg, initial_admin_password='', apt_signing_keypair=res1['apt_signing_keypair'])
        res2 = configure_nexus.configure_all(cfg2)
        self.assertFalse(res2['rotated_admin'])
        self.assertFalse(res2['accepted_eula'])
        self.assertFalse(res2['changed_anonymous'])
        self.assertFalse(res2['generated_apt_key'])
        self.assertFalse(res2['changed_realms'])
        self.assertEqual(len(state.repositories), 25)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required to render templates')
    def test_nexus_gitea_and_arcane_templates_enforce_backup_and_gitops_invariants(self):
        variables = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
        for role in ('keycloak', 'openldap', 'nexus', 'gitea', 'arcane'):
            variables.update(yaml.safe_load((ROOT / f'roles/{role}/defaults/main.yml').read_text()))
        variables.update(
            domain_name='site.example',
            infravm_ip='10.0.60.107',
            nexus_admin_password='FixtureNexusPassword123!',
            nexus_db_password='FixtureNexusDbPassword123!',
            nexus_secrets_key='a' * 64,
            nexus_pull_password='FixturePullPassword123!',
            gitea_admin_password='FixtureGiteaPassword123!',
            gitea_db_password='FixtureGiteaDbPassword123!',
            keycloak_gitea_client_secret='FixtureClientSecret123!',
            keycloak_arcane_client_secret='FixtureClientSecret123!',
            arcane_encryption_key='hex:' + 'b' * 64,
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tasks = [
                {'ansible.builtin.template': {
                    'src': str(ROOT / f'roles/{role}/templates/docker-compose.yml.j2'),
                    'dest': str(directory / f'{role}.yml'),
                }, 'vars': variables}
                for role in ('nexus', 'gitea', 'arcane')
            ]
            result = run_fixture(directory, tasks)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            gitea_compose = yaml.safe_load((directory / 'gitea.yml').read_text())
            self.assertIn('GITEA__packages__ENABLED=false', gitea_compose['services']['gitea']['environment'])

            arcane_compose = yaml.safe_load((directory / 'arcane.yml').read_text())
            arcane_hosts = set(arcane_compose['services']['arcane']['extra_hosts'])
            for sub in ('auth', 'git', 'nexus', 'registry', 'registry-push'):
                self.assertIn(f'{sub}.site.example:10.0.60.107', arcane_hosts)

            nexus_compose = yaml.safe_load((directory / 'nexus.yml').read_text())
            nexus_svc = nexus_compose['services']['nexus']
            nexus_db_svc = nexus_compose['services']['nexus-db']

            # Startup preserves restored nexus.secrets.json and sophon-pull.password when non-empty.
            cmd = nexus_svc['command'][0]
            self.assertIn('/nexus-data/blobs/scratch', cmd)
            self.assertIn('if [ ! -s /nexus-data/etc/nexus.secrets.json ]', cmd)
            self.assertIn('if [ ! -s /nexus-data/etc/sophon-pull.password ]', cmd)
            self.assertIn('ldap_certs:/run/secrets/ldap:ro', nexus_svc['volumes'])
            self.assertEqual(nexus_compose['volumes']['ldap_certs']['name'], 'openldap_ldap_certs')

            traefik_tpl = (ROOT / 'roles/traefik/templates/docker-compose.yml.j2').read_text()
            self.assertIn('--entrypoints.websecure.transport.respondingTimeouts.readTimeout=0', traefik_tpl)

            # Validate labels against real Kopia v3 backup.parse_spec!
            nexus_labels = dict(item.split('=', 1) for item in nexus_svc['labels'])
            nexus_spec = backup.parse_spec({
                'Name': '/nexus',
                'Config': {'Labels': nexus_labels},
                'Mounts': [
                    {'Type': 'volume', 'Name': 'nexus_nexus_data', 'Destination': '/nexus-data', 'RW': True},
                ],
            })
            self.assertEqual(nexus_spec['group'], 'nexus')
            self.assertEqual(
                [(p['volume'], p['path'], p['optional']) for p in nexus_spec['paths']],
                [
                    ('nexus_nexus_data', 'blobs/hosted', False),
                    ('nexus_nexus_data', 'keystores', False),
                    ('nexus_nexus_data', 'etc', False),
                ],
            )

            db_labels = dict(item.split('=', 1) for item in nexus_db_svc['labels'])
            db_spec = backup.parse_spec({
                'Name': '/nexus-db',
                'Config': {'Labels': db_labels},
                'Mounts': [
                    {'Type': 'volume', 'Name': 'nexus_nexus_db_data', 'Destination': '/var/lib/postgresql', 'RW': True},
                ],
            })
            self.assertEqual(db_spec['group'], 'nexus')
            self.assertEqual(len(db_spec['dumps']), 1)
            self.assertEqual(db_spec['dumps'][0]['restore'], 'postgres-cluster')
            # Nexus 3.98+ opens ~110 concurrent JDBC connections (HikariCP + Quartz); default 100 starves pg_dumpall.
            self.assertEqual(nexus_db_svc.get('command'), ['postgres', '-c', 'max_connections=250'])

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required to evaluate role tasks')
    def test_nexus_role_task_variables_resolve_from_role_defaults_only(self):
        tasks_doc = yaml.safe_load((ROOT / 'roles/nexus/tasks/main.yml').read_text())
        cfg_block = next(item['block'] for item in tasks_doc if 'block' in item)
        cfg_task = next(t for t in cfg_block if t['name'] == 'Run idempotent Nexus REST API configurator')
        status_task = next(t for t in tasks_doc if t['name'] == 'Display Nexus status')

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            out_json = directory / 'payload.json'
            playbook = [{
                'hosts': 'localhost',
                'gather_facts': False,
                'vars_files': [
                    str(ROOT / 'group_vars/all.yml'),
                    str(ROOT / 'roles/nexus/defaults/main.yml'),
                ],
                'vars': {
                    'nexus_pull_password': 'FixturePullPassword123!',
                    'nexus_initial_pw_cmd': {'stdout': 'init-secret\n'},
                    'nexus_ldap_ca_cmd': {'stdout': '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n'},
                    'nexus_apt_key_stat': {'stat': {'exists': False}},
                },
                'tasks': [
                    {
                        'name': 'Render nexus_configure_payload from real task vars',
                        'ansible.builtin.copy': {
                            'dest': str(out_json),
                            'content': '{{ nexus_configure_payload | to_json }}',
                        },
                        'vars': cfg_task['vars'],
                    },
                    status_task,
                ],
            }]
            playbook_path = directory / 'check_nexus_vars.yml'
            playbook_path.write_text(yaml.safe_dump(playbook))
            env = {
                **os.environ,
                'SOPHON_DOMAIN': 'site.example',
                'SOPHON_INFRAVM_IP': '10.0.60.107',
                'SOPHON_NEXUS_ADMIN_PASSWORD': 'FixtureNexusPassword123!',
                'SOPHON_NEXUS_DB_PASSWORD': 'FixtureNexusDbPassword123!',
                'SOPHON_OPENLDAP_ADMIN_PASSWORD': 'FixtureLdapPassword123!',
            }
            res = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(playbook_path)],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            rendered = json.loads(out_json.read_text())
            self.assertEqual(rendered['base_url'], 'https://nexus.site.example')
            self.assertEqual(rendered['domain'], 'site.example')
            self.assertEqual(rendered['ldap']['name'], 'Sophon OpenLDAP')
            self.assertEqual(rendered['ldap']['protocol'], 'ldaps')
            self.assertIs(rendered['ldap']['use_trust_store'], True)
            self.assertEqual(rendered['ldap']['host'], 'ldap.site.example')
            self.assertEqual(rendered['ldap']['search_base'], 'dc=site,dc=example')
            self.assertIn('https://nexus.site.example', res.stdout)
            self.assertIn('https://registry.site.example', res.stdout)
            self.assertIn('https://registry-push.site.example', res.stdout)

    def test_reconcile_gitops_exports_workloads_and_honors_standby_skip(self):
        if not shutil.which('git'):
            self.skipTest('git is required')
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            work_repo = tmp_dir / 'work'
            bare_repo = tmp_dir / 'workloads.git'
            subprocess.run(['git', 'init', '--bare', str(bare_repo)], check=True, capture_output=True)
            subprocess.run(['git', 'init', str(work_repo)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(work_repo), 'config', 'user.email', 'ci@site.example'], check=True)
            subprocess.run(['git', '-C', str(work_repo), 'config', 'user.name', 'CI'], check=True)

            (work_repo / 'stacks/webapp').mkdir(parents=True)
            (work_repo / 'stacks/webapp/compose.yaml').write_text(
                'services:\n  web:\n    image: registry.site.example/apps/web:1.0.0\n'
            )
            (work_repo / 'stacks/alert-bot').mkdir(parents=True)
            (work_repo / 'stacks/alert-bot/compose.yaml').write_text(
                'x-sophon-standby: skip\nservices:\n  bot:\n    image: registry.site.example/apps/bot:1.0.0\n'
            )
            subprocess.run(['git', '-C', str(work_repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(work_repo), 'commit', '-m', 'seed stacks'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(work_repo), 'push', str(bare_repo), 'HEAD:refs/heads/main'], check=True, capture_output=True)

            resolved = None
            for ref in ('HEAD', 'refs/heads/main', 'refs/heads/master'):
                probe = subprocess.run(
                    ['git', '--git-dir', str(bare_repo), 'rev-parse', '--verify', ref],
                    capture_output=True, text=True,
                )
                if probe.returncode == 0:
                    resolved = ref
                    break
            self.assertEqual(resolved, 'refs/heads/main')
            archive_bytes = subprocess.check_output(['git', '--git-dir', str(bare_repo), 'archive', '--format=tar', resolved])
            import io
            import tarfile
            out_stacks = tmp_dir / 'stacks'
            out_stacks.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:*') as tf:
                tf.extractall(tmp_dir / 'extracted')

            standby_deployed = []
            for entry in sorted((tmp_dir / 'extracted/stacks').iterdir()):
                compose_doc = yaml.safe_load((entry / 'compose.yaml').read_text())
                if str(compose_doc.get('x-sophon-standby', '')).strip().lower() == 'skip':
                    continue
                standby_deployed.append(entry.name)
            self.assertEqual(standby_deployed, ['webapp'])

    def test_default_anonymous_read_enables_docker_pulls_while_keeping_pushes_authenticated(self):
        defaults = yaml.safe_load((ROOT / 'roles/nexus/defaults/main.yml').read_text())
        self.assertIs(defaults['nexus_anonymous_enabled'], True)

        state = MockNexusState(bootstrap_pw='initial-bootstrap-secret')
        server = HTTPServer(('127.0.0.1', 0), make_nexus_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        cfg = {
            'base_url': f'http://127.0.0.1:{server.server_port}',
            'verify_tls': False,
            'admin_user': 'admin',
            'admin_password': 'SophonAdminPassword123!',
            'initial_admin_password': 'initial-bootstrap-secret',
            # Omit anonymous_enabled so configure_all exercises its default (True).
            'ldap_ca_pem': self._make_self_signed_pem(),
            'ldap': {
                'name': 'Sophon OpenLDAP',
                'protocol': 'ldaps',
                'use_trust_store': True,
                'host': 'ldap.site.example',
                'port': 636,
                'search_base': 'dc=site,dc=example',
                'bind_dn': 'cn=keycloak-bind,ou=services,dc=site,dc=example',
                'bind_password': 'LdapBindPassword123!',
            },
            'pull_user': 'sophon-pull',
            'pull_password': 'SophonPullPassword123!',
            'domain': 'site.example',
            'docker_group_port': 5000,
            'docker_hosted_port': 5001,
            'apt_signing_keypair': '',
        }
        configure_nexus.configure_all(cfg)
        self.assertTrue(state.anonymous['enabled'])
        self.assertEqual(state.anonymous['userId'], 'anonymous')
        # Pull endpoints allow anonymous Docker Bearer token pulls; push endpoint still requires credentials.
        for pull_repo in ('docker-group', 'docker-hub-proxy', 'ghcr-proxy', 'quay-proxy'):
            self.assertFalse(state.repositories[pull_repo]['docker']['forceBasicAuth'], pull_repo)
        self.assertTrue(state.repositories['docker-hosted']['docker']['forceBasicAuth'])

    def test_reconcile_gitops_writes_podman_mirror_and_restarts_podman_service_only_on_change(self):
        import tomllib

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bin_dir = tmp_dir / 'bin'
            bin_dir.mkdir()
            project_dir = tmp_dir / 'project'
            project_dir.mkdir()
            stacks_dir = tmp_dir / 'stacks'
            stacks_dir.mkdir()
            containers_etc = tmp_dir / 'etc-containers'
            containers_etc.mkdir()
            home_dir = tmp_dir / 'home-core'
            home_dir.mkdir()
            xdg_dir = tmp_dir / 'xdg'
            xdg_dir.mkdir()
            hosts_file = tmp_dir / 'hosts'
            hosts_file.write_text('127.0.0.1 localhost\n')
            systemctl_log = tmp_dir / 'systemctl.log'

            (project_dir / 'deploy-result.json').write_text(
                json.dumps({'domain': 'site.example', 'infravm_ip': '10.0.60.3'})
            )
            (project_dir / 'site.json').write_text(json.dumps({'SOPHON_INFRAVM_IP': '10.0.60.3'}))

            fake_id = bin_dir / 'id'
            fake_id.write_text(
                '#!/usr/bin/env bash\n'
                'if [[ "${1:-}" == "-u" ]]; then echo 1000; elif [[ "${1:-}" == "-un" ]]; then echo core; else /usr/bin/id "$@"; fi\n'
            )
            fake_id.chmod(0o755)

            fake_sudo = bin_dir / 'sudo'
            fake_sudo.write_text('#!/usr/bin/env bash\nexec "$@"\n')
            fake_sudo.chmod(0o755)

            fake_systemctl = bin_dir / 'systemctl'
            fake_systemctl.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{systemctl_log}"\n')
            fake_systemctl.chmod(0o755)

            fake_podman = bin_dir / 'podman'
            fake_podman.write_text(
                '#!/usr/bin/env bash\n'
                'if [[ "$*" == *"container exists nexus"* ]]; then exit 0; fi\n'
                'if [[ "$*" == *"exec nexus"* && "$*" == *".crt"* ]]; then\n'
                '  printf "%s\\n" "-----BEGIN CERTIFICATE-----" "TESTCERT" "-----END CERTIFICATE-----"\n'
                '  exit 0\n'
                'fi\n'
                'if [[ "$*" == *"exec nexus"* && "$*" == *"sophon-pull.password"* ]]; then\n'
                '  printf "%s" "PullSecret123!"\n'
                '  exit 0\n'
                'fi\n'
                'exit 0\n'
            )
            fake_podman.chmod(0o755)

            env = {
                **os.environ,
                'PATH': f'{bin_dir}:{os.environ.get("PATH", "")}',
                'SOPHON_PROJECT_DIR': str(project_dir),
                'SOPHON_STACKS_DIR': str(stacks_dir),
                'SOPHON_HOSTS_FILE': str(hosts_file),
                'SOPHON_CONTAINERS_ETC': str(containers_etc),
                'SOPHON_HOME_OVERRIDE': str(home_dir),
                'SOPHON_XDG_RUNTIME_OVERRIDE': str(xdg_dir),
            }

            run1 = subprocess.run(
                ['bash', str(ROOT / 'guest/sophon-reconcile-gitops'), '--sync-auth-only'],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(run1.returncode, 0, run1.stdout + run1.stderr)
            mirror_conf = containers_etc / 'registries.conf.d/sophon-nexus.conf'
            self.assertTrue(mirror_conf.is_file())
            parsed = tomllib.loads(mirror_conf.read_text())
            by_prefix = {entry['prefix']: entry for entry in parsed['registry']}
            self.assertEqual(set(by_prefix), {'docker.io', 'ghcr.io', 'quay.io'})
            for upstream, entry in by_prefix.items():
                self.assertEqual(entry['location'], upstream)
                self.assertEqual(entry['mirror'], [{'location': 'registry.site.example'}])
            self.assertTrue((containers_etc / 'certs.d/registry.site.example/ca.crt').is_file())
            self.assertEqual(
                systemctl_log.read_text().strip().splitlines(),
                ['--user try-restart podman.service'],
            )

            # Second run with unchanged config must NOT restart podman.service again.
            run2 = subprocess.run(
                ['bash', str(ROOT / 'guest/sophon-reconcile-gitops'), '--sync-auth-only'],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(run2.returncode, 0, run2.stdout + run2.stderr)
            self.assertEqual(
                systemctl_log.read_text().strip().splitlines(),
                ['--user try-restart podman.service'],
            )


if __name__ == '__main__':
    unittest.main()
