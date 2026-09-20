import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from wsgiref.simple_server import make_server

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deployment import STAGES, FIELDS, DashboardServer, application, new_control, shell_command, trace_environment


def run_fixture(directory, tasks, roles=None):
    for name, content in (roles or {}).items():
        role_file = directory / 'roles' / name / 'tasks' / 'main.yml'
        role_file.parent.mkdir(parents=True, exist_ok=True)
        role_file.write_text(content)
    playbook = directory / 'fixture.yml'
    playbook.write_text(
      '- hosts: localhost\n  gather_facts: false\n  tasks:\n' + tasks
      if isinstance(tasks, str) else json.dumps([
        {'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks}]))
    status_file = directory / 'status.json'
    environment = dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default',
                       ANSIBLE_CALLBACK_PLUGINS=str(ROOT / 'callback_plugins'),
                       ANSIBLE_CALLBACKS_ENABLED='sophon_progress',
                       SOPHON_PROGRESS_FILE=str(status_file))
    result = subprocess.run(
        ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(playbook)],
        env=environment, cwd=directory, capture_output=True, text=True, timeout=30)
    assert status_file.exists(), result.stdout + result.stderr
    return result, json.loads(status_file.read_text())


def test_nested_stage():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        result, report = run_fixture(directory, '''
    - name: Start service
      ansible.builtin.include_role:
        name: service
      tags: [sophon_stage_gitea]
''', {
            'service': '''
- ansible.builtin.include_role:
    name: helper
- ansible.builtin.slurp:
    src: "{{ lookup('env', 'SOPHON_PROGRESS_FILE') }}"
  register: progress
- ansible.builtin.assert:
    that:
      - (progress.content | b64decode | from_json).stages.gitea.state == 'running'
- ansible.builtin.debug:
    msg: SECRET_MUST_NOT_REACH_BROWSER
  no_log: true
''',
            'helper': '- ansible.builtin.debug:\n    msg: Nested deployment work\n',
        })
        assert result.returncode == 0, result.stdout + result.stderr
        assert report['state'] == 'completed', report
        assert report['stages']['gitea']['state'] == 'completed', report
        assert 'SECRET_MUST_NOT_REACH_BROWSER' not in json.dumps(report)


def test_task_details():
  with tempfile.TemporaryDirectory() as temporary:
    result, report = run_fixture(Path(temporary), [
      {'ansible.builtin.include_role': {'name': 'service'}, 'tags': ['sophon_stage_gitea']},
    ], {'service': json.dumps([
      {'name': 'Show generated credential', 'ansible.builtin.debug': {'msg': 'PRIVATE_DEBUG_OUTPUT'}},
      {'name': 'PRIVATE_NO_LOG_NAME', 'ansible.builtin.debug': {'msg': 'PRIVATE_NO_LOG_OUTPUT'}, 'no_log': True},
      {'name': 'Remember API credential', 'ansible.builtin.set_fact': {'api_token': 'PRIVATE_FACT_TOKEN'}},
      {'name': 'Retry transient task', 'ansible.builtin.command': {'argv': [
        sys.executable, '-c',
        'from pathlib import Path; import sys; '
        f'path = Path({str(Path(temporary) / "retried")!r}); '
        'ready = path.exists(); path.touch(); '
        'print("" if ready else "Temporary failure", file=sys.stderr); sys.exit(0 if ready else 1)',
      ]}, 'register': 'retry_result', 'until': 'retry_result.rc == 0', 'retries': 2, 'delay': 0},
      {'name': 'Connect to service', 'ansible.builtin.command': {'argv': [
        sys.executable, '-c',
        'import sys; print("Connection refused; token=PRIVATE_TOKEN_VALUE; PRIVATE_FACT_TOKEN", file=sys.stderr); sys.exit(7)',
      ]}},
    ])})
    assert result.returncode != 0
    tasks = report['stages']['gitea']['tasks']
    assert len(tasks) == 5, tasks
    retried = tasks[-2]
    assert retried['state'] == 'changed' and retried['attempts'] == 2, retried
    assert 'stderr' not in retried and 'message' not in retried, retried
    failed = tasks[-1]
    assert failed['name'].endswith('Connect to service'), failed
    assert failed['state'] == 'failed' and failed['rc'] == 7, failed
    assert 'Connection refused' in failed['stderr'], failed
    assert 'main.yml' in failed['source'], failed
    encoded = json.dumps(report)
    for secret in ('PRIVATE_DEBUG_OUTPUT', 'PRIVATE_NO_LOG_NAME', 'PRIVATE_NO_LOG_OUTPUT', 'PRIVATE_TOKEN_VALUE', 'PRIVATE_FACT_TOKEN'):
      assert secret not in encoded, secret


def test_outcomes_and_reset():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        result, report = run_fixture(directory, '''
    - ansible.builtin.include_role:
        name: service
      tags: [sophon_stage_keycloak]
    - ansible.builtin.include_role:
        name: absent
      when: false
      tags: [sophon_stage_gitea]
    - ansible.builtin.debug:
        msg: End
      tags: [sophon_stage_end]
''', {'service': '''
- block:
    - ansible.builtin.fail:
        msg: Recoverable
  rescue:
    - ansible.builtin.debug:
        msg: Recovered
- ansible.builtin.fail:
    msg: Ignored
  ignore_errors: true
'''})
        assert result.returncode == 0, result.stdout + result.stderr
        assert report['state'] == 'completed', report
        assert report['stages']['keycloak']['state'] == 'completed', report
        assert report['stages']['keycloak']['rescued'] == 1, report
        assert report['stages']['keycloak']['ignored'] == 1, report
        assert any(task['state'] == 'rescued' for task in report['stages']['keycloak']['tasks']), report
        assert any(task['state'] == 'ignored' for task in report['stages']['keycloak']['tasks']), report
        assert report['stages']['gitea']['state'] == 'skipped', report
        previous_run = report['run_id']
        result, report = run_fixture(directory, '''
    - ansible.builtin.include_role:
        name: broken
      tags: [sophon_stage_nfs]
''', {'broken': '- ansible.builtin.fail:\n    msg: SECRET_FAILURE_DETAILS\n  no_log: true\n'})
        assert result.returncode != 0
        assert report['run_id'] != previous_run
        assert report['state'] == 'failed', report
        assert report['stages']['nfs']['state'] == 'failed', report
        assert report['stages']['keycloak']['state'] == 'pending', report
        assert 'SECRET_FAILURE_DETAILS' not in json.dumps(report)


def test_failure_edges():
  cases = [
    ([{'ansible.builtin.fail': {'msg': 'Ignored'}, 'ignore_errors': True}], 'completed'),
    ([{'block': [{'ansible.builtin.fail': {'msg': 'Recovered'}}],
       'rescue': [{'ansible.builtin.debug': {'msg': 'Not needed'}, 'when': False}]}], 'completed'),
    ([{'block': [{'ansible.builtin.fail': {'msg': 'Initial failure'}}],
       'rescue': [{'ansible.builtin.fail': {'msg': 'Recovery failed'}}]}], 'failed'),
  ]
  for tasks, expected in cases:
    with tempfile.TemporaryDirectory() as temporary:
      result, report = run_fixture(Path(temporary), [
        {'ansible.builtin.include_role': {'name': 'service'}, 'tags': ['sophon_stage_gitea']},
      ], {'service': json.dumps(tasks)})
      assert report['state'] == expected, result.stdout + result.stderr
      assert report['stages']['gitea']['state'] == expected, report


def test_launcher():
  with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    playbook = directory / 'failure.yml'
    playbook.write_text(json.dumps([{
      'hosts': 'localhost', 'gather_facts': False,
      'tasks': [{'ansible.builtin.fail': {'msg': 'SECRET_TERMINAL_ONLY'}}],
    }]))
    state = directory / 'state'
    server_process = subprocess.Popen(
      [sys.executable, str(ROOT / 'deployment.py'), '--state-dir', str(state), '--serve'],
      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
      server = {'url': server_process.stdout.readline().strip(), 'pid': server_process.pid}
      result = subprocess.run(
        [sys.executable, str(ROOT / 'deployment.py'), '--state-dir', str(state),
         '--', str(playbook), '-i', 'localhost,', '-c', 'local'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
      assert result.returncode == 2, result.stdout + result.stderr
      assert 'SECRET_TERMINAL_ONLY' in result.stdout
      with urlopen(server['url'] + 'status.json', timeout=5) as response:
        report = json.load(response)
      assert report['state'] == 'failed', report
      assert 'SECRET_TERMINAL_ONLY' not in json.dumps(report)
      with urlopen(server['url'], timeout=5) as response:
        assert 'Sophon' in response.read().decode()
      for path, headers, expected in [
        ('artifacts/secrets/example', {}, 404),
        ('server.json', {}, 404),
        ('status.json', {'Host': 'untrusted.example'}, 403),
      ]:
        try:
          urlopen(Request(server['url'] + path, headers=headers), timeout=5)
        except HTTPError as error:
          assert error.code == expected
        else:
          raise AssertionError('Unexpected route access: ' + path)
      previous_run = report['run_id']
      binary = directory / 'ansible-playbook'
      binary.write_text(f'#!{sys.executable}\nimport os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n')
      binary.chmod(0o700)
      result = subprocess.run(
        [sys.executable, str(ROOT / 'deployment.py'), '--state-dir', str(state), '--', 'fixture.yml'],
        env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH']),
        cwd=ROOT, capture_output=True, text=True, timeout=30)
      assert result.returncode == 143, result.stdout + result.stderr
      assert server_process.poll() is None
      assert not list(state.glob('*.lock'))
      assert not (state / 'server.json').exists()
      with urlopen(server['url'] + 'status.json', timeout=5) as response:
        report = json.load(response)
      assert report['state'] == 'interrupted', report
      assert report['run_id'] != previous_run
      report['state'] = 'running'
      report['current'] = 'gitea'
      report['stages']['gitea']['state'] = 'running'
      report.pop('pid', None)
      (state / 'status.json').write_text(json.dumps(report))
      with urlopen(server['url'] + 'status.json', timeout=5) as response:
        report = json.load(response)
      assert report['state'] == 'unknown', report
      assert report['stages']['gitea']['state'] == 'unknown', report
    finally:
      server_process.terminate()
      server_process.wait(timeout=5)
      server_process.stdout.close()


def test_gui_settings():
  with tempfile.TemporaryDirectory() as temporary:
    environment = {name: value for name, value in os.environ.items() if not name.startswith('SOPHON_')}
    environment.update(SOPHON_PROXMOX_HOST='192.0.2.10', SOPHON_PROXMOX_PASSWORD='PRIVATE_TEST_PASSWORD')
    server = subprocess.Popen(
      [sys.executable, str(ROOT / 'deployment.py'), '--state-dir', temporary, '--serve'],
      env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
      url = server.stdout.readline().strip()
      with urlopen(url + 'config.json', timeout=5) as response:
        token = json.load(response)['token']
      try:
        urlopen(url + 'settings.json', timeout=5)
      except HTTPError as error:
        assert error.code == 403
      else:
        raise AssertionError('Settings must require a same-origin token')
      with urlopen(Request(url + 'settings.json', headers={'X-Sophon-Token': token}), timeout=5) as response:
        assert response.headers['Cache-Control'] == 'no-store'
        settings = json.load(response)
      fields = {field['name']: field for field in settings['fields']}
      for name, definition in FIELDS.items():
        assert fields[name]['required'] == definition[4]
        if definition[2] == 'password' and name != 'SOPHON_PROXMOX_PASSWORD':
          assert fields[name]['value'] == ''
      assert fields['SOPHON_PROXMOX_HOST']['value'] == '192.0.2.10'
      assert fields['SOPHON_PROXMOX_PASSWORD']['value'] == 'PRIVATE_TEST_PASSWORD'
      assert fields['SOPHON_PROXMOX_PASSWORD']['configured'] is True
      with urlopen(url + 'status.json', timeout=5) as response:
        assert b'PRIVATE_TEST_PASSWORD' not in response.read()
    finally:
      server.terminate()
      server.wait(timeout=5)
      server.stdout.close()


def test_gui_deploy():
  with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    release = directory / 'release'
    os.mkfifo(release)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    binary = directory / 'ansible-playbook'
    binary.write_text('\n'.join([
      f'#!{sys.executable}', 'import json, os, sys',
      f'with open({str(directory / "received.json")!r}, "w") as output:',
      '    json.dump({"host": os.environ["SOPHON_PROXMOX_HOST"],',
      '               "password_matches": os.environ["SOPHON_PROXMOX_PASSWORD"] == "FORM_PRIVATE_PASSWORD",',
      '               "credentials": {name: value for name, value in os.environ.items()',
      '                               if name.startswith("SOPHON_") and ("PASSWORD" in name or "SECRET" in name)},',
      '               "args": sys.argv[1:]}, output)',
      'print("PRIVATE_ANSIBLE_OUTPUT", flush=True)',
      f'with open({str(release)!r}) as gate: gate.read(1)',
    ]))
    binary.chmod(0o700)
    environment = {name: value for name, value in os.environ.items() if not name.startswith('SOPHON_')}
    environment.update({name: 'TEST_' + name for name, field in FIELDS.items()
                        if field[2] == 'password' and field[4]})
    environment.update(PATH=str(directory) + os.pathsep + os.environ['PATH'],
      SOPHON_PROXMOX_HOST='192.0.2.10', SOPHON_PROXMOX_PASSWORD='INHERITED_PRIVATE_PASSWORD',
      SOPHON_DOMAIN='example.test', SOPHON_VNET_GATEWAY='192.0.2.1', SOPHON_VNET_CIDR='24',
      SOPHON_NFS_IP='192.0.2.2', SOPHON_INFRAVM_IP='192.0.2.3')
    control = new_control(environment)

    def request(path, values=None, **overrides):
      data = json.dumps(values or {}).encode()
      request_environment = {'SERVER_PORT': '12345', 'HTTP_HOST': '127.0.0.1:12345',
        'REQUEST_METHOD': 'POST' if path in ('/run', '/command') else 'GET', 'PATH_INFO': path,
        'HTTP_ORIGIN': 'http://127.0.0.1:12345', 'HTTP_X_SOPHON_TOKEN': control['token'],
        'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(data)),
        'wsgi.input': io.BytesIO(data)}
      request_environment.update(overrides)
      response = {}
      def start_response(status, headers):
        response['status'] = int(status.split()[0])
      body = b''.join(application(directory, {}, control, request_environment, start_response))
      return response['status'], body

    try:
      for endpoint in ('/run', '/command'):
        assert request(endpoint, HTTP_ORIGIN='https://untrusted.example')[0] == 403
        assert request(endpoint, HTTP_X_SOPHON_TOKEN='incorrect')[0] == 403
        assert request(endpoint, {'PATH': '/untrusted'})[0] == 400
        assert request(endpoint, {'SOPHON_NFS_IP': 'invalid'})[0] == 400
        assert request(endpoint, {'SOPHON_NFS_IP': '192.0.2.3'})[0] == 400
        assert request(endpoint, {'SOPHON_PROXMOX_PASSWORD': ''})[0] == 400
        assert request(endpoint, CONTENT_LENGTH='999999')[0] == 413
        for name, field in FIELDS.items():
          if field[2] == 'password' and field[4]:
            for value in ('', '   '):
              status, body = request(endpoint, {name: value})
              assert status == 400 and name.encode() in body, body
        status, body = request(endpoint, {'SOPHON_PORTAINER_ADMIN_PASSWORD': 'x' * 11})
        assert status == 400 and b'at least 12' in body
        for name in ('SOPHON_GITEA_DB_PASSWORD', 'SOPHON_KOPIA_ADMIN_PASSWORD', 'SOPHON_KOPIA_REPOSITORY_PASSWORD'):
          assert request(endpoint, {name: '   '})[0] == 400
      assert not control['processes'] and not (directory / 'deploy.log').exists()
      assert request('/command', {'SOPHON_PORTAINER_ADMIN_PASSWORD': 'x' * 12})[0] == 200
      status, body = request('/command')
      assert status == 200 and b'INHERITED_PRIVATE_PASSWORD' in body
      assert not control['processes'] and not (directory / 'status.json').exists()
      status, body = request('/run', {'SOPHON_PROXMOX_HOST': '192.0.2.20',
                                        'SOPHON_PROXMOX_PASSWORD': 'FORM_PRIVATE_PASSWORD'})
      assert status == 202, body
      os.write(release_fd, b'1')
      control['processes'][0].wait(timeout=30)
      received = json.loads((directory / 'received.json').read_text())
      credentials = {name: value for name, value in control['values'].items()
                     if 'PASSWORD' in name or 'SECRET' in name}
      credentials['SOPHON_PROXMOX_PASSWORD'] = 'FORM_PRIVATE_PASSWORD'
      assert received == {'host': '192.0.2.20', 'password_matches': True,
                          'credentials': credentials, 'args': ['site.yml']}, received
      status, body = request('/status.json')
      assert status == 200
      assert b'PRIVATE_PASSWORD' not in body and b'PRIVATE_ANSIBLE_OUTPUT' not in body
      assert json.loads(body)['state'] == 'unknown'  # Stub emitted no callback events.
      assert control['values']['SOPHON_PROXMOX_PASSWORD'] == 'INHERITED_PRIVATE_PASSWORD'
      assert not list(directory.glob('*.lock'))
      assert (directory / 'deploy.log').stat().st_mode & 0o777 == 0o600
      assert request('/deploy.log')[0] == 404
    finally:
      os.close(release_fd)
      for worker in control['processes']:
        if worker.poll() is None:
          os.killpg(worker.pid, signal.SIGTERM)
          worker.wait(timeout=5)


def test_site_markers():
  tasks = yaml.safe_load((ROOT / 'site.yml').read_text())[0]['tasks']
  markers = [tag.removeprefix('sophon_stage_') for task in tasks
             for tag in task.get('tags', []) if tag.startswith('sophon_stage_')]
  assert markers == [*STAGES, 'end'], markers


def test_credential_inputs():
  required = [
    'proxmox_password', 'portainer_admin_password', 'keycloak_admin_password',
    'keycloak_db_password', 'keycloak_portainer_client_secret', 'gitea_admin_password',
    'openldap_admin_password', 'openldap_example_user_password', 'openldap_example_admin_password',
  ]
  shared = ['gitea_db_password', 'kopia_admin_password', 'kopia_repository_password']
  inputs = required + shared
  preflight = yaml.safe_load((ROOT / 'site.yml').read_text())[0]['pre_tasks']
  assert preflight[0]['loop'] == inputs
  assert {name for name, field in FIELDS.items() if field[2] == 'password' and field[4]} == {
    'SOPHON_' + name.upper() for name in required}
  sources = [ROOT / 'site.yml', ROOT / 'group_vars/all.yml', ROOT / 'tests/test.yml',
             *sorted((ROOT / 'roles').rglob('*.yml'))]
  for source in sources:
    text = source.read_text()
    assert "lookup('password'" not in text and 'lookup("password"' not in text, source
    assert 'artifacts/secrets/' not in text, source

  with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    (directory / 'group_vars').mkdir()
    (directory / 'group_vars/all.yml').write_text((ROOT / 'group_vars/all.yml').read_text())
    config = directory / 'ansible.cfg'
    config.write_text('[defaults]\nstdout_callback=default\n')
    playbook = directory / 'credentials.yml'
    playbook.write_text(json.dumps([{
      'hosts': 'localhost', 'gather_facts': False, 'pre_tasks': preflight,
      'tasks': [
        {'name': 'Check seed usernames remain available outside role scope',
         'ansible.builtin.assert': {'that': [
           "openldap_example_user_uid == 'john.doe'",
           "openldap_example_admin_uid == 'john.doe.admin'"], 'quiet': True},
         'tags': ['credential_test']},
        {'name': 'Check supplied credentials without displaying them',
         'ansible.builtin.assert': {'that': [
           "lookup('vars', item) == expected[item]"], 'quiet': True},
         'loop': inputs, 'no_log': True, 'tags': ['credential_test']},
        {'ansible.builtin.debug': {'msg': 'CREDENTIAL_PREFLIGHT_PASSED'}, 'tags': ['credential_test']},
      ],
    }]))
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(('SOPHON_', 'ANSIBLE_'))}
    environment['ANSIBLE_CONFIG'] = str(config)
    supplied = {name: 'PRIVATE_TEST_' + name for name in required}
    expected = supplied | {name: supplied['portainer_admin_password'] for name in shared}
    exported = {'SOPHON_' + name.upper(): value for name, value in supplied.items()}

    def run(values, expected_values, overrides=None, error=None):
      result = subprocess.run(
        ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(playbook),
         '--tags', 'credential_test', '-e', json.dumps({'expected': expected_values} | (overrides or {}))],
        env=environment | values, cwd=directory, capture_output=True, text=True, timeout=30)
      output = result.stdout + result.stderr
      assert (result.returncode == 0) == (error is None), output
      assert ('CREDENTIAL_PREFLIGHT_PASSED' in output) == (error is None), output
      if error:
        assert error in output, output
      assert 'PRIVATE_TEST_' not in output and 'OLD_SAVED_PASSWORD' not in output, output
      assert not list(directory.rglob('*.ansible_lockfile'))

    run(exported, expected)
    assert not (directory / 'artifacts').exists()
    for name in required:
      missing = dict(exported)
      del missing['SOPHON_' + name.upper()]
      run(missing, expected, error='SOPHON_' + name.upper())
    run(exported | {'SOPHON_GITEA_ADMIN_PASSWORD': '   '}, expected,
        error='SOPHON_GITEA_ADMIN_PASSWORD')
    run(exported | {'SOPHON_PORTAINER_ADMIN_PASSWORD': 'x' * 11}, expected,
        error='at least 12 characters')
    minimum = expected | {name: 'x' * 12 for name in ['portainer_admin_password', *shared]}
    run(exported | {'SOPHON_PORTAINER_ADMIN_PASSWORD': 'x' * 12}, minimum)
    run({}, expected, overrides=supplied)
    run(exported, expected | {'gitea_admin_password': 'PRIVATE_TEST_OVERRIDE'},
        overrides={'gitea_admin_password': 'PRIVATE_TEST_OVERRIDE'})
    separate = {name: 'PRIVATE_TEST_' + name for name in shared}
    run(exported | {'SOPHON_' + name.upper(): value for name, value in separate.items()},
        expected | separate)
    run(exported | {'SOPHON_' + name.upper(): '' for name in shared}, expected)
    assert not (directory / 'artifacts').exists()

    legacy = directory / 'artifacts/secrets'
    legacy.mkdir(parents=True)
    old_password = legacy / 'gitea_admin_password'
    old_password.write_text('OLD_SAVED_PASSWORD')
    legacy.chmod(0o000)
    try:
      run(exported, expected)
      run(exported, expected)
    finally:
      legacy.chmod(0o700)
    assert old_password.read_text() == 'OLD_SAVED_PASSWORD'
    assert sorted(path.name for path in legacy.iterdir()) == ['gitea_admin_password']


def test_shell_command():
  with tempfile.TemporaryDirectory(prefix="sophon shell ' ") as temporary:
    directory = Path(temporary)
    state = directory / 'trace'
    marker = directory / 'must-not-exist'
    values = new_control({})['values']
    values['SOPHON_PROXMOX_PASSWORD'] = f"apostrophe' spaces $HOME $(touch {marker}) `id` ; & \\"
    values['SOPHON_CF_TOKEN'] = ''
    environment = dict(os.environ, ANSIBLE_RUN_TAGS='wrong', ANSIBLE_SKIP_TAGS='all', SOPHON_CF_TOKEN='stale')
    binary = directory / 'ansible-playbook'
    binary.write_text('\n'.join([
      f'#!{sys.executable}', 'import json, os, sys',
      f'with open({str(directory / "received.json")!r}, "w") as output:',
      '    json.dump({"environment": dict(os.environ), "args": sys.argv[1:]}, output)',
    ]))
    binary.chmod(0o700)
    environment['PATH'] = str(directory) + os.pathsep + os.environ['PATH']
    command = shell_command(state, values, environment)
    assert 'ansible-playbook site.yml' in command
    assert not state.exists(), 'Generating a command must not create files or launch a process'
    for shell in ('sh', 'bash', 'zsh'):
      result = subprocess.run([shell], input=command, env=environment, capture_output=True, text=True, timeout=10)
      assert result.returncode == 0, result.stderr
      received = json.loads((directory / 'received.json').read_text())
      assert received['args'] == ['site.yml']
      assert {name: received['environment'][name] for name in FIELDS} == values
      traced = trace_environment(state, environment)
      for name in ('ANSIBLE_CALLBACK_PLUGINS', 'ANSIBLE_CALLBACKS_ENABLED', 'SOPHON_PROGRESS_FILE', 'ANSIBLE_LOG_PATH'):
        assert received['environment'][name] == traced[name]
      assert 'ANSIBLE_RUN_TAGS' not in received['environment']
      assert 'ANSIBLE_SKIP_TAGS' not in received['environment']
      assert not marker.exists()
      assert (state / 'ansible.log').stat().st_mode & 0o777 == 0o600


def test_shell_trace():
  with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    (directory / 'callback_plugins').symlink_to(ROOT / 'callback_plugins', target_is_directory=True)
    role = directory / 'roles' / 'fixture' / 'tasks'
    role.mkdir(parents=True)
    (role / 'main.yml').write_text('- ansible.builtin.debug:\n    msg: Local fixture only\n')
    (directory / 'site.yml').write_text(json.dumps([{
      'hosts': 'localhost', 'gather_facts': False,
      'tasks': [{'ansible.builtin.include_role': {'name': 'fixture'}, 'tags': ['sophon_stage_gitea']}],
    }]))
    state = directory / 'trace'
    with patch('deployment.ROOT', directory):
      command = shell_command(state, new_control({})['values'], {})
    previous_run = None
    for expected in ('completed', 'failed'):
      result = subprocess.run(['sh'], input=command, env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default'),
                              capture_output=True, text=True, timeout=30)
      report = json.loads((state / 'status.json').read_text())
      assert report['state'] == expected, result.stdout + result.stderr
      assert (result.returncode == 0) == (expected == 'completed')
      assert report['stages']['gitea']['state'] == expected
      assert report['run_id'] != previous_run
      previous_run = report['run_id']
      assert report['pid'] > 0
      assert (state / 'status.json').stat().st_mode & 0o777 == 0o600
      assert not list(state.glob('*.lock'))
      (role / 'main.yml').write_text('- ansible.builtin.fail:\n    msg: Local fixture failure\n')


def test_idle_browser_connection():
  with tempfile.TemporaryDirectory() as temporary:
    control = new_control({})
    receiving = threading.Event()

    def handler(environment, start_response):
      if environment['PATH_INFO'] == '/run':
        receiving.set()
      return application(Path(temporary), {}, control, environment, start_response)

    with make_server('127.0.0.1', 0, handler, server_class=DashboardServer) as server:
      accepted = threading.Event()
      original = server.get_request

      def get_request():
        result = original()
        accepted.set()
        return result

      server.get_request = get_request
      worker = threading.Thread(target=server.serve_forever, daemon=True)
      worker.start()
      idle = socket.create_connection(server.server_address, timeout=2)
      try:
        assert accepted.wait(2), 'Idle connection was not accepted'
        base = f'http://127.0.0.1:{server.server_port}'
        with urlopen(base + '/status.json', timeout=2) as response:
          assert json.load(response)['state'] == 'idle'
        with urlopen(Request(base + '/settings.json', headers={'X-Sophon-Token': control['token']}), timeout=2) as response:
          assert json.load(response)['fields']
        with socket.create_connection(server.server_address, timeout=2) as partial_request:
          partial_request.sendall((
            f'POST /run HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'
            f'Origin: {base}\r\nX-Sophon-Token: {control["token"]}\r\n'
            'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n'
          ).encode())
          assert receiving.wait(2), 'Deployment request was not accepted'
          with urlopen(base + '/status.json', timeout=2) as response:
            assert json.load(response)['state'] == 'idle'
          partial_request.sendall(b'{}')
          assert b'400 Bad Request' in partial_request.recv(4096)
      finally:
        idle.close()
        server.shutdown()
        worker.join(timeout=2)


def test_nfs_recovery():
  role = yaml.safe_load((ROOT / 'roles/nfs/tasks/main.yml').read_text())
  named = {task['name']: task for task in role}
  provision = named['Continue deployment if NFS is not already reachable']
  registration = next(task for task in provision['block']
            if task['name'] == 'Add NFS storage to Proxmox (retries until NFS is online)')
  tasks = []
  for registered, active, existing in [(False, False, False), (True, False, False),
                     (True, True, False), (True, False, True),
                     (True, True, True)]:
    tasks.append({'ansible.builtin.set_fact': {
      'nfs_storage_name': 'sophon-nfs', 'nfs_vm_name': 'sophon-nfs',
      'nfs_storage_check': {'json': {'data': [{'storage': 'sophon-nfs'}] if registered else []}},
      'nfs_storage_status': {'json': {'data': [{'storage': 'sophon-nfs', 'active': int(active)}]
                          if registered else []}},
      'proxmox_existing_vms': {'json': {'data': [{'name': 'sophon-nfs', 'vmid': 100}]
                          if existing else []}},
    }})
    tasks.extend(named[name] for name in ['Set NFS storage registration fact',
                        'Set NFS reachable fact', 'Discover NFS VM in Proxmox'])
    tasks.append({'ansible.builtin.assert': {'that': [
      f'nfs_storage_registered == {registered}',
      f'nfs_already_reachable == {active}',
      f"({provision['when']}) == {not active or not existing}",
      f"({registration['when']}) == {not registered}",
    ]}})
  with tempfile.TemporaryDirectory() as temporary:
    result, report = run_fixture(Path(temporary), [
      {'ansible.builtin.include_role': {'name': 'nfs_recovery'}, 'tags': ['sophon_stage_nfs']},
    ], {'nfs_recovery': json.dumps(tasks)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert report['stages']['nfs']['state'] == 'completed', report


if __name__ == '__main__':
  test_credential_inputs()
  test_shell_command()
  test_shell_trace()
  test_idle_browser_connection()
  test_nfs_recovery()
  test_nested_stage()
  test_task_details()
  test_outcomes_and_reset()
  test_failure_edges()
  test_launcher()
  test_site_markers()
  test_gui_settings()
  test_gui_deploy()
  print('Deployment integration checks passed')