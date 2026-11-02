import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import subprocess
import sys
import threading
from unittest.mock import patch

import yaml

from test_deployment import ROOT, run_fixture
import bundle
import deployment


def test_kopia_is_packaged_and_deployed_before_gitea():
    play = yaml.safe_load((ROOT / '4-configure.yml').read_text())[0]
    roles = [task['ansible.builtin.include_role']['name'] for task in play['tasks']
             if 'ansible.builtin.include_role' in task]
    assert 'kopia' in roles
    assert roles.index('traefik') < roles.index('kopia') < roles.index('gitea')
    assert 'roles/kopia' in bundle.PROJECT


def test_kopia_configuration_is_required_and_fails_closed():
    fields = deployment.step_fields('configure')
    required = {'SOPHON_KOPIA_PASSWORD', 'SOPHON_KOPIA_SERVER_PASSWORD', 'SOPHON_KOPIA_SFTP_HOST',
                'SOPHON_KOPIA_SFTP_USERNAME', 'SOPHON_KOPIA_SFTP_PATH',
                'SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64'}
    assert required <= fields.keys()
    from test_console import service_values
    values = service_values()
    valid, error = deployment.validate_settings(values, {}, 'configure')
    assert error is None
    assert valid['SOPHON_KOPIA_INIT_REPOSITORY'] == 'false'
    assert valid['SOPHON_KOPIA_BACKUPS_ENABLED'] == 'false'
    for name in required:
        _, error = deployment.validate_settings(values, {name: ''}, 'configure')
        assert name in error
    for name, value in (
            ('SOPHON_KOPIA_SFTP_PORT', '65536'),
            ('SOPHON_KOPIA_SFTP_PATH', '/backup/../other'),
            ('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', 'not-base64!'),
            ('SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64', base64.b64encode(b'\xff').decode()),
            ('SOPHON_KOPIA_INIT_REPOSITORY', 'yes'),
            ('SOPHON_KOPIA_BACKUPS_ENABLED', 'yes')):
        _, error = deployment.validate_settings(values, {name: value}, 'configure')
        assert error is not None
        assert value not in error


def test_existing_backup_and_restore_regressions():
    for name in ('backup_checks.py', 'restore_checks.py'):
        result = subprocess.run([sys.executable, str(ROOT / 'tests' / name)],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr


def test_core_deployment_creates_then_reuses_collector_key_and_preserves_environment(tmp_path):
    from test_console import service_values
    requests = []
    stack = None
    key = 'FIXTURE_COLLECTOR_PRIVATE_KEY'
    archive = tmp_path / 'sophon-kopia_0.23.1.tar'
    archive.write_bytes(b'fixture-image-archive')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(('GET', self.path))
            if self.path == '/api/stacks':
                self.respond(200, [stack] if stack else [])
            elif self.path == '/api/users':
                self.respond(200, [{'Id': 7, 'Username': 'admin'}])
            elif self.path == '/api/endpoints':
                self.respond(200, [{'Id': 1, 'Type': 1}])
            elif self.path.endswith('/docker/info'):
                self.respond(200 if self.headers.get('X-API-Key') == key else 403, {})
            elif self.path.endswith('/containers/kopia/json'):
                self.respond(200, {'State': {'Health': {'Status': 'healthy'}}})
            else:
                self.respond(404, {})

        def do_POST(self):
            nonlocal stack
            data = self.rfile.read(int(self.headers['Content-Length']))
            requests.append(('POST', self.path))
            if self.path == '/api/auth':
                self.respond(200, {'jwt': 'fixture-jwt'})
            elif self.path == '/api/users/7/tokens':
                self.respond(200, {'rawAPIKey': key})
            elif self.path.endswith('/docker/images/load'):
                assert data == b'fixture-image-archive'
                self.respond(200, {})
            elif self.path == '/api/stacks/create/standalone/string?endpointId=1':
                body = json.loads(data)
                stack = {'Id': 8, 'Name': body['name'], 'EndpointId': 1, 'Env': body['env']}
                self.check_compose(body['stackFileContent'])
                self.respond(200, stack)
            else:
                self.respond(404, {})

        def do_PUT(self):
            requests.append(('PUT', self.path))
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/api/stacks/8?endpointId=1':
                stack['Env'] = body['env']
                self.check_compose(body['stackFileContent'])
                self.respond(200, stack)
            else:
                self.respond(404, {})

        def check_compose(self, text):
            compose = yaml.safe_load(text)
            service = compose['services']['kopia']
            assert service['image'] == 'localhost/sophon-kopia:0.23.1'
            assert service['pull_policy'] == 'never'
            assert service['labels']['traefik.http.routers.kopia.rule'] == 'Host(`kopia.example.test`)'
            assert service['labels']['sophon.backup.version'] == '2'
            assert not service.get('ports') and not service.get('privileged')
            assert set(compose['volumes']) == {'config', 'cache', 'logs', 'source', 'restore'}

        def respond(self, status, value):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            variables = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
            variables.update({name.removeprefix('SOPHON_').lower(): value
                              for name, value in service_values().items() if name.startswith('SOPHON_KOPIA_')})
            variables.update(
                domain_name='example.test', portainer_auth={'json': {'jwt': 'fixture-jwt'}},
                portainer_admin_password='FIXTURE_ADMIN_PASSWORD', portainer_local_endpoint_id=1,
                infravm_portainer_url=f'http://127.0.0.1:{server.server_port}',
                kopia_image_tars=[str(archive)])
            for attempt in range(3):
                if attempt == 1:
                    stack['Env'].append({'name': 'SOPHON_PROJECT_PATH', 'value': '/existing/project'})
                if attempt == 2:
                    next(e for e in stack['Env'] if e['name'] == 'BACKUP_PORTAINER_API_KEY')['value'] = 'revoked'
                with patch.dict(os.environ, {'ANSIBLE_ROLES_PATH': str(ROOT / 'roles')}):
                    result = run_fixture(tmp_path, [{
                        'ansible.builtin.include_role': {'name': str(ROOT / 'roles/kopia')},
                        'vars': variables,
                    }])
                assert (result.returncode == 0) == (attempt < 2), result.stdout + result.stderr
                assert key not in result.stdout + result.stderr
                assert 'FIXTURE_ADMIN_PASSWORD' not in result.stdout + result.stderr
                assert requests.count(('POST', '/api/users/7/tokens')) == 1
                if attempt < 2:
                    env = {e['name']: e['value'] for e in stack['Env']}
                    assert env['BACKUP_PORTAINER_API_KEY'] == key
                    assert env['KOPIA_INIT_REPOSITORY'] == 'false'
                    assert env['KOPIA_BACKUPS_ENABLED'] == 'false'
                    if attempt == 1:
                        assert env['SOPHON_PROJECT_PATH'] == '/existing/project'
            assert requests.count(('PUT', '/api/stacks/8?endpointId=1')) == 1
        finally:
            server.shutdown()
            worker.join()
