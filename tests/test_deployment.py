import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from deployment import step_fields


def run_fixture(directory, tasks):
  playbook = directory / 'fixture.yml'
  playbook.write_text(json.dumps([{'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks}]))
  return subprocess.run(
      ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(playbook)],
      env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default'), cwd=directory,
      capture_output=True, text=True, timeout=30)


def test_vm_network_config():
  remaining = list(yaml.safe_load((ROOT / 'roles/proxmox/tasks/vm.yml').read_text()))
  networks = []
  while remaining:
    task = remaining.pop()
    for section in ('block', 'rescue', 'always'):
      remaining.extend(task.get(section, []))
    network = task.get('community.proxmox.proxmox_kvm', {}).get('net', {}).get('net0')
    if network:
      networks.append(network)
  assert len(networks) == 1, networks
  tasks = []
  for vlan in ('', '300', 300, 1, 4094):
    expected = 'model=virtio,bridge=vmbr0' + (f',tag={vlan}' if vlan else '')
    tasks.extend([
      {'ansible.builtin.set_fact': {'vnet_interface': 'vmbr0', 'vnet_vlan': vlan}},
      {'name': 'Render InfraVM network device', 'ansible.builtin.set_fact': {'rendered_network': networks[0]}},
      {'ansible.builtin.assert': {'that': [f'rendered_network == {expected!r}']}},
    ])
  with tempfile.TemporaryDirectory() as temporary:
    result = run_fixture(Path(temporary), tasks)
    assert result.returncode == 0, result.stdout + result.stderr


def test_credential_inputs():
  required = [
    'portainer_admin_password', 'keycloak_admin_password',
    'keycloak_db_password', 'keycloak_portainer_client_secret', 'gitea_admin_password',
    'openldap_admin_password', 'openldap_example_user_password', 'openldap_example_admin_password',
  ]
  shared = ['gitea_db_password']
  inputs = required + shared
  preflight = yaml.safe_load((ROOT / '4-configure.yml').read_text())[0]['pre_tasks']
  assert preflight[1]['loop'] == inputs
  preflight = preflight[:3]
  assert {name for name, field in step_fields('configure').items() if field[2] == 'password' and field[4]} == {
    'SOPHON_' + name.upper() for name in required}
  sources = [ROOT / '4-configure.yml', ROOT / 'group_vars/all.yml',
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
      'hosts': 'localhost', 'gather_facts': False, 'pre_tasks': [
        dict(task, tags=['always']) for task in preflight],
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
    environment.update(ANSIBLE_CONFIG=str(config), SOPHON_DEPLOYMENT_STEP='configure')
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


def run_launcher(arguments, extra=None, path=None):
  """Run src/deployment.py with a stand-in ansible-playbook that records what it receives."""
  with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    recorder = directory / 'ansible-playbook'
    recorder.write_text('\n'.join([
      f'#!{sys.executable}', 'import json, os, sys',
      f'with open({str(directory / "received.json")!r}, "w") as output:',
      '    json.dump({"environment": dict(os.environ), "args": sys.argv[1:]}, output)',
    ]))
    recorder.chmod(0o700)
    environment = {name: value for name, value in os.environ.items() if not name.startswith('SOPHON_')}
    environment['PATH'] = path if path is not None else str(directory) + os.pathsep + os.environ['PATH']
    environment.update(extra or {})
    result = subprocess.run([sys.executable, str(ROOT / 'src/deployment.py'), *arguments], env=environment,
                            capture_output=True, text=True, timeout=30, cwd=directory)
    received = directory / 'received.json'
    return result, json.loads(received.read_text()) if received.exists() else None


def test_launcher_passes_only_the_steps_settings():
  result, received = run_launcher(['--step', 'provision', '--', '2-provision.yml', '-e', 'prepare_force=true'], {
    'SOPHON_PROXMOX_HOST': '192.0.2.10', 'SOPHON_PROXMOX_PASSWORD': 'PRIVATE_TEST_PASSWORD',
    'SOPHON_DOMAIN': 'service-setting.example', 'SOPHON_PORTAINER_ADMIN_PASSWORD': 'x' * 16})
  assert result.returncode == 0, result.stderr
  assert received['args'] == ['2-provision.yml', '-e', 'prepare_force=true']
  environment = received['environment']
  assert environment['SOPHON_PROXMOX_PASSWORD'] == 'PRIVATE_TEST_PASSWORD'
  assert environment['SOPHON_DEPLOYMENT_STEP'] == 'provision'
  assert 'SOPHON_DOMAIN' not in environment and 'SOPHON_PORTAINER_ADMIN_PASSWORD' not in environment
  assert 'PRIVATE_TEST_PASSWORD' not in result.stdout + result.stderr

  result, received = run_launcher(['--step', 'configure', '--', '4-configure.yml'], {
    'SOPHON_DEPLOYMENT_STEP': 'configure', 'SOPHON_PROXMOX_PASSWORD': 'DO_NOT_FORWARD',
    'SOPHON_ACME_FILE': '/workspace/imports/acme.json', 'SOPHON_DOMAIN': 'example.test'})
  assert result.returncode == 0, result.stderr
  environment = received['environment']
  assert environment['SOPHON_DOMAIN'] == 'example.test'
  assert 'SOPHON_PROXMOX_PASSWORD' not in environment and 'SOPHON_ACME_FILE' not in environment


def test_launcher_rejects_wrong_step_and_hypervisor():
  for arguments, extra, message in (
      (['--step', 'provision', '--', '4-configure.yml'], {}, 'provision requires 2-provision.yml'),
      (['--step', 'configure', '--', '4-configure.yml'], {}, 'sophon-deploy'),
      (['--step', 'provision', '--', '2-provision.yml'], {'SOPHON_HYPERVISOR': 'xen'}, 'proxmox, vcenter or hyperv')):
    result, received = run_launcher(arguments, extra)
    assert result.returncode == 2 and message in result.stderr, (arguments, result.stderr)
    assert received is None
  result, received = run_launcher(['--step', 'provision'])
  assert result.returncode == 2 and received is None


def test_startup_failure():
  result, received = run_launcher(['--step', 'provision', '--', '2-provision.yml'],
                                  {'SOPHON_PROXMOX_PASSWORD': 'PRIVATE_STARTUP_PASSWORD'}, path='/nonexistent')
  assert result.returncode == 127
  assert 'sophon-tools' in result.stderr
  assert 'PRIVATE_STARTUP_PASSWORD' not in result.stdout + result.stderr


if __name__ == '__main__':
  test_vm_network_config()
  test_credential_inputs()
  test_launcher_passes_only_the_steps_settings()
  test_launcher_rejects_wrong_step_and_hypervisor()
  test_startup_failure()
