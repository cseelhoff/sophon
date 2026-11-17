import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from deployment import new_control, step_fields, validate_settings
from test_deployment import run_fixture as _run_fixture


def run_fixture(directory, tasks):
    return _run_fixture(directory, tasks), None


def test_step_settings():
    secrets = {
        'SOPHON_PROXMOX_PASSWORD': 'MANAGEMENT_SECRET',
        'SOPHON_VCENTER_PASSWORD': 'VCENTER_SECRET',
        'SOPHON_VCENTER_URL': 'https://management.example',
        'SOPHON_HYPERV_PASSWORD': 'HYPERV_SECRET',
        'SOPHON_CONTROLLER_PASSWORD': 'LOGIN_SECRET',
        'SOPHON_CF_TOKEN': 'SERVICE_SECRET',
        'SOPHON_DOMAIN': 'configure.example',
    }
    provision = new_control(secrets, 'provision')
    configure = new_control(secrets, 'configure')
    assert 'SERVICE_SECRET' not in json.dumps(provision)
    assert 'MANAGEMENT_SECRET' not in json.dumps(configure)
    assert 'VCENTER_SECRET' not in json.dumps(configure)
    assert 'HYPERV_SECRET' not in json.dumps(configure)
    assert 'management.example' not in json.dumps(configure)
    assert 'LOGIN_SECRET' not in json.dumps(configure)
    assert 'SOPHON_PROXMOX_HOST' not in step_fields('configure')
    assert 'SOPHON_ARCANE_ADMIN_PASSWORD' not in step_fields('provision')
    assert 'SOPHON_DOMAIN' not in step_fields('provision')
    assert 'configure.example' not in json.dumps(provision)
    assert step_fields('configure')['SOPHON_DOMAIN'][4]
    for step, field in (('provision', 'SOPHON_CF_TOKEN'), ('configure', 'SOPHON_PROXMOX_PASSWORD')):
        values, error = validate_settings(new_control({}, step)['values'], {field: 'forbidden'}, step)
        assert values is None and error == 'Unexpected deployment setting.'


def test_service_task_graph():
    visited = set()

    def walk(path):
        if path in visited:
            return
        visited.add(path)
        data = yaml.safe_load(path.read_text()) or []
        if isinstance(data, dict):
            assert not data.get('dependencies'), path
            return
        for task in data:
            inspect_task(task, path)

    def inspect_task(task, path):
        for module, value in task.items():
            if module in ('pre_tasks', 'tasks', 'post_tasks', 'block', 'rescue', 'always'):
                for entry in value:
                    inspect_task(entry, path)
            assert 'proxmox' not in module.lower(), (task, module)
            if module in ('ansible.builtin.include_role', 'ansible.builtin.import_role'):
                role = value['name']
                assert role not in ('proxmox', 'vcenter', 'hyperv', 'prepare'), role
                metadata = ROOT / 'roles' / role / 'meta/main.yml'
                if metadata.exists():
                    walk(metadata)
                walk(ROOT / 'roles' / role / 'tasks' / (value.get('tasks_from', 'main') + '.yml'))
            if module in ('ansible.builtin.include_tasks', 'ansible.builtin.import_tasks'):
                filename = value if isinstance(value, str) else value['file']
                assert '{{' not in filename, filename
                walk(path.parent / filename)

    walk(ROOT / '4-configure.yml')
    assert ROOT / 'roles/arcane/tasks/main.yml' in visited
    assert ROOT / 'roles/compose_stack/tasks/main.yml' in visited
    assert ROOT / 'roles/proxmox/tasks/main.yml' not in visited


def test_site_configuration_and_secret_boundary():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / 'src').mkdir()
        shutil.copyfile(ROOT / 'src/deployment.py', directory / 'src/deployment.py')
        binary = directory / 'ansible-playbook'
        binary.write_text(
            f'#!{sys.executable}\nimport json, os\n'
            f'open({str(directory / "received.json")!r}, "w").write(json.dumps(dict(os.environ)))\n')
        binary.chmod(0o700)
        site = directory / 'site.json'
        site.write_text(json.dumps({'SOPHON_INFRAVM_IP': '192.0.2.3'}))
        environment = {key: value for key, value in os.environ.items() if not key.startswith('SOPHON_')}
        environment.update(
            PATH=str(directory), SOPHON_DEPLOYMENT_STEP='configure',
            SOPHON_PROXMOX_PASSWORD='DO_NOT_FORWARD', SOPHON_CONTROLLER_PASSWORD='DO_NOT_FORWARD',
            SOPHON_DOMAIN='override.example')
        command = [sys.executable, str(directory / 'src/deployment.py'), '--step', 'configure',
                   '--', '4-configure.yml']
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        received = json.loads((directory / 'received.json').read_text())
        assert received['SOPHON_DOMAIN'] == 'override.example'
        assert received['SOPHON_INFRAVM_IP'] == '192.0.2.3'
        assert not any(key.startswith(('SOPHON_PROXMOX_', 'SOPHON_CONTROLLER_')) for key in received)
        for key in ('SOPHON_PROXMOX_PASSWORD', 'SOPHON_CF_TOKEN', 'SOPHON_DOMAIN', 'PATH'):
            site.write_text(json.dumps({key: 'rejected'}))
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            assert result.returncode == 2 and 'supported non-secret site settings' in result.stderr
        site.unlink()
        environment.pop('SOPHON_DEPLOYMENT_STEP')
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 2 and 'sophon-deploy' in result.stderr


def test_provision_preservation():
    guard = yaml.safe_load((ROOT / 'roles/proxmox/tasks/vm.yml').read_text())
    infravm = {'name': 'sophon-infravm', 'vmid': 101, 'type': 'qemu', 'node': 'pve'}
    marker = 'sophon-infravm-two-iso-v1'
    cases = [
        ([], '', True),
        ([{'name': 'unrelated-guest'}], '', True),
        ([infravm], marker, True),
        ([infravm], 'sophon-infravm-v1', False),
        ([infravm], '', False),
        ([infravm | {'template': 1}], marker, False),
        ([infravm | {'type': 'lxc'}], marker, False),
        ([infravm | {'node': 'other-node'}], marker, False),
        ([infravm, infravm | {'vmid': 102}], marker, False),
    ]
    for vms, description, succeeds in cases:
        tasks = [
            {'ansible.builtin.set_fact': {'proxmox_vms': {'json': {'data': vms}},
                                         'proxmox_node': 'pve', 'infravm_vm_name': 'sophon-infravm',
                                         'infravm_marker': marker,
                                         'infravm_existing_config': {'json': {'data': {'description': description}}}}},
            *guard[:2],
            {'when': 'infravm_named_guests | length == 1',
             'block': [guard[2]['block'][0], guard[2]['block'][2]]},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = run_fixture(Path(temporary), tasks)
            assert (result.returncode == 0) == succeeds, result.stdout + result.stderr


def test_domain_deferred_until_services():
    required = yaml.safe_load((ROOT / '2-provision.yml').read_text())[0]['tasks'][1]['loop']
    assert not any(name in required for name in (
        'SOPHON_DOMAIN', 'SOPHON_NFS_IP', 'SOPHON_INFRAVM_IP',
        'SOPHON_CONTROLLER_PASSWORD', 'SOPHON_CONTROLLER_SSH_PUBLIC_KEY'))
    tasks = yaml.safe_load((ROOT / 'roles/proxmox/tasks/vm.yml').read_text())
    config = tasks[3]['block'][-1]['community.proxmox.proxmox_kvm']
    assert not any(name in config for name in ('args', 'ipconfig', 'sshkeys', 'cicustom', 'sata', 'scsi'))
    with tempfile.TemporaryDirectory() as temporary:
        result, _ = run_fixture(Path(temporary), [
            {'ansible.builtin.set_fact': {
                'proxmox_iso_storage_id': 'isos', 'proxmox_disk_storage_id': 'disks',
                'infravm_disk_gb': 100, 'vnet_interface': 'vmbr0', 'vnet_vlan': '',
                'infravm_isos': [{'filename': 'sophon-coreos-digest.iso'},
                                 {'filename': 'sophon-deployment-digest.iso'}],
            }},
            {'ansible.builtin.set_fact': {'devices': {key: config[key] for key in ('virtio', 'ide', 'boot', 'net', 'serial')}}},
            {'ansible.builtin.assert': {'that': [
                "devices.virtio.virtio0 == 'disks:100'",
                "devices.ide.ide2 == 'isos:iso/sophon-coreos-digest.iso,media=cdrom'",
                "devices.ide.ide3 == 'isos:iso/sophon-deployment-digest.iso,media=cdrom'",
                "devices.boot == 'order=virtio0;ide2'",
                "devices.net.net0 == 'model=virtio,bridge=vmbr0'",
                "devices.serial.serial0 == 'socket'",
            ]}},
        ])
        assert result.returncode == 0, result.stdout + result.stderr


def test_existing_immutable_media_are_reused_and_size_checked():
    tasks = yaml.safe_load((ROOT / 'roles/proxmox/tasks/vm.yml').read_text())[3]['block']
    validation = next(task for task in tasks if task['name'] == 'Validate already uploaded immutable media sizes')
    upload = next(task for task in tasks if task['name'] == 'Upload the CoreOS live ISO and Sophon artifact ISO')
    coreos = {'volid': 'local:iso/coreos.iso', 'size': 4}
    bundle = {'volid': 'local:iso/bundle.iso', 'size': 8}
    for contents, expected, needs_upload in (
        ([], True, True), ([coreos, bundle], True, False),
        ([coreos | {'size': 5}, bundle], False, False),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = run_fixture(Path(temporary), [
                {'ansible.builtin.set_fact': {
                    'proxmox_iso_storage_id': 'local',
                    'infravm_isos': [{'filename': 'coreos.iso', 'size': 4}, {'filename': 'bundle.iso', 'size': 8}],
                    'proxmox_iso_content': {'json': {'data': contents}},
                    'item': {'filename': 'coreos.iso'},
                }},
                validation,
                {'ansible.builtin.assert': {'that': [
                    f"({upload['when']}) == {'true' if needs_upload else 'false'}",
                ]}},
            ])
            assert (result.returncode == 0) == expected, result.stdout + result.stderr


def test_service_resolution_follows_entered_domain():
    preflight = yaml.safe_load((ROOT / '4-configure.yml').read_text())[0]['pre_tasks']
    validation, hosts_task = preflight[-2:]
    assert hosts_task['ansible.builtin.blockinfile']['path'] == '/etc/hosts'
    assert hosts_task['ansible.builtin.blockinfile']['unsafe_writes']
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        hosts = directory / 'hosts'
        original = '127.0.0.1 localhost\n192.0.2.20 unrelated.example\n'
        hosts.write_text(original)
        hosts_task['ansible.builtin.blockinfile']['path'] = str(hosts)
        for domain, ip in [('First.Example.', '192.0.2.3'), ('second.example', '192.0.2.4')]:
            result, _ = run_fixture(directory, [
                {'ansible.builtin.set_fact': {'domain_name': domain, 'infravm_ip': ip}},
                validation, hosts_task,
            ])
            assert result.returncode == 0, result.stdout + result.stderr
            content = hosts.read_text()
            assert content.startswith(original)
            canonical = domain.lower().rstrip('.')
            entries = [line for line in content.splitlines() if line.startswith(ip + ' ')]
            assert entries == [f'{ip} {name + "." if name else ""}{canonical}'
                               for name in ('', 'traefik', 'auth', 'ldap', 'git', 'arcane', 'dns', 'kopia')]
            if canonical == 'second.example':
                assert 'first.example' not in content and '192.0.2.3' not in content
        unchanged = hosts.read_text()
        for domain, ip in [('', '192.0.2.3'), ('bad.*.example', '192.0.2.3'),
                           ('bad.example\ninjected', '192.0.2.3'), ('valid.example', '999.2.3.4'),
                           ('valid.example', '192.0.2.3\ninjected')]:
            result, _ = run_fixture(directory, [
                {'ansible.builtin.set_fact': {'domain_name': domain, 'infravm_ip': ip}},
                validation, hosts_task,
            ])
            assert result.returncode != 0, result.stdout + result.stderr
            assert hosts.read_text() == unchanged


def test_service_resolution_probe_uses_python_and_exact_addresses():
    tasks = yaml.safe_load((ROOT / 'roles/coredns/tasks/main.yml').read_text())
    block = next(task['block'] for task in tasks if task['name'] == 'Deploy CoreDNS')
    probe = next(task for task in block if task['name'] == 'Preflight resolution of the service domain')
    guard = next(task for task in block
                 if task['name'] == 'Fail if the runner does not resolve the service domain to InfraVM')
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        result, _ = run_fixture(directory, [
            {'ansible.builtin.set_fact': {'coredns_preflight_fqdn': 'localhost',
                                         'coredns_domain_name': 'localhost', 'infravm_ip': '127.0.0.1'}},
            probe, guard,
        ])
        assert result.returncode == 0, result.stdout + result.stderr
        result, _ = run_fixture(directory, [
            {'ansible.builtin.set_fact': {
                'coredns_preflight_fqdn': 'test.example', 'coredns_domain_name': 'example',
                'infravm_ip': '127.0.0.1',
                'coredns_preflight': {'stdout': '127.0.0.10', 'stdout_lines': ['127.0.0.10'], 'stderr': ''},
            }}, guard,
        ])
        assert result.returncode != 0, result.stdout + result.stderr


def test_cloudflared_connects_only_when_a_tunnel_was_found():
    defaults = yaml.safe_load((ROOT / 'roles/cloudflared/defaults/main.yml').read_text())
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        compose = directory / 'compose.yml'
        result, _ = run_fixture(directory, [{
            'ansible.builtin.template': {
                'src': str(ROOT / 'roles/cloudflared/templates/docker-compose.yml.j2'), 'dest': str(compose)},
            'vars': dict(defaults, cloudflared_token='CONNECTOR_TOKEN'),
        }])
        assert result.returncode == 0, result.stdout + result.stderr
        service = yaml.safe_load(compose.read_text())['services']['cloudflared']
        assert service['image'] == 'docker.io/cloudflare/cloudflared:' + defaults['cloudflared_image_tag']
        assert service['pull_policy'] == 'never' and service['networks'] == ['proxy']
        assert service['environment'] == ['TUNNEL_TOKEN=CONNECTOR_TOKEN']
        assert 'ports' not in service
        assert set(service['labels']) == {
            'sophon.backup.version=3', 'sophon.backup.group=cloudflared', 'sophon.backup.stop=false'}
        # No tunnel (no token or no egress): the role must not touch the network or engine.
        result, _ = run_fixture(directory, [{
            'ansible.builtin.include_tasks': str(ROOT / 'roles/cloudflared/tasks/main.yml'),
            'vars': {'coredns_cf_tunnel_id': '', 'coredns_cf_account_id': '', 'coredns_cf_token': 'unused'},
        }])
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'changed=0' in result.stdout and 'failed=0' in result.stdout and 'skipped=' in result.stdout
    tasks = (ROOT / 'roles/cloudflared/tasks/main.yml').read_text()
    assert 'no_log: true' in tasks.split('Read the tunnel connector token')[1].split('- name:')[0]
    order = [task.get('name') for task in yaml.safe_load((ROOT / '4-configure.yml').read_text())[0]['tasks']]
    assert order.index('Deploy Traefik') < order.index('Connect the Cloudflare tunnel')


def test_coredns_alternate_host_port_is_loopback_only():
    defaults = yaml.safe_load((ROOT / 'roles/coredns/defaults/main.yml').read_text())
    defaults.update(coredns_image_tag='fixture', coredns_domain_name='site.example',
                    infravm_ip='192.0.2.3', vnet_dns='9.9.9.9')
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        compose = directory / 'compose.yml'
        for port in (53, 1053):
            result, _ = run_fixture(directory, [{
                'ansible.builtin.template': {
                    'src': str(ROOT / 'roles/coredns/templates/docker-compose.yml.j2'),
                    'dest': str(compose)},
                'vars': dict(defaults, coredns_dns_port=port),
            }])
            assert result.returncode == 0, result.stdout + result.stderr
            service = yaml.safe_load(compose.read_text())['services']['coredns']
            expected = [f'127.0.0.1:{port}:53/{protocol}' for protocol in ('udp', 'tcp')]
            if port == 53:
                expected += [f'192.0.2.3:53:53/{protocol}' for protocol in ('udp', 'tcp')]
            assert service['ports'] == expected
            assert service['networks'] == ['proxy']


def test_missing_tunnel_is_created_only_after_a_successful_empty_lookup():
    tasks = yaml.safe_load((ROOT / 'roles/coredns/tasks/main.yml').read_text())
    preflight = tasks[0]['block'][0]['block']
    names = [task['name'] for task in preflight]
    create = preflight[names.index('Create the Cloudflare tunnel when none has that name')]
    assert names.index('Look up the Cloudflare tunnel by name') < names.index(create['name']) \
        < names.index('Adopt the tunnel and account ids only as a matched pair')
    call = create['ansible.builtin.uri']
    assert call['method'] == 'POST' and call['url'].endswith('/cfd_tunnel')
    assert call['headers'] == {'Authorization': 'Bearer {{ coredns_cf_token }}'}
    assert call['body'] == {'name': '{{ coredns_cf_tunnel_name }}', 'config_src': 'cloudflare'}
    # An API error on the lookup must never be mistaken for "no such tunnel".
    assert any('tunnels.json.success' in condition for condition in create['when'])
    assert any('length == 0' in condition for condition in create['when'])


def test_stale_tunnel_cnames_are_repointed_but_other_records_are_not():
    tasks = yaml.safe_load((ROOT / 'roles/coredns/tasks/main.yml').read_text())
    preflight = tasks[0]['block'][0]['block']
    names = [task['name'] for task in preflight]
    repoint = preflight[names.index('Repoint CNAMEs that still target another tunnel')]
    assert names.index('Adopt the tunnel and account ids only as a matched pair') \
        < names.index('Look up the CNAMEs already on the service names') < names.index(repoint['name']) \
        < names.index('Display the Cloudflare coordinates in use')
    call = repoint['ansible.builtin.uri']
    assert call['method'] == 'PATCH' and call['headers'] == {'Authorization': 'Bearer {{ coredns_cf_token }}'}
    assert call['body']['type'] == 'CNAME' and call['body']['proxied'] is True
    assert call['body']['comment'] == '{{ coredns_cf_owner_comment }}'
    conditions = ' '.join(repoint['when'])
    assert 'cfargotunnel' in conditions and '!=' in conditions
    defaults = yaml.safe_load((ROOT / 'roles/coredns/defaults/main.yml').read_text())
    assert defaults['coredns_cf_owner_comment'] == 'managed by coredns-dockerdiscovery'
    assert {'traefik', 'auth', 'ldap', 'git', 'arcane', 'dns'} <= set(defaults['coredns_published_hosts'])


def test_gitea_initializer_reports_cli_failures():
    defaults = yaml.safe_load((ROOT / 'roles/gitea/defaults/main.yml').read_text())
    defaults.update(
        domain_name='site.example', arcane_admin_password='fixture',
        gitea_admin_password='fixture', gitea_db_password='fixture', keycloak_hostname='auth', keycloak_domain='site.example',
        keycloak_realm='sophon', keycloak_gitea_client_secret='fixture')
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        compose = directory / 'compose.yml'
        result, _ = run_fixture(directory, [{
            'ansible.builtin.template': {
                'src': str(ROOT / 'roles/gitea/templates/docker-compose.yml.j2'), 'dest': str(compose)},
            'vars': defaults,
        }])
        assert result.returncode == 0, result.stdout + result.stderr
        script = yaml.safe_load(compose.read_text())['services']['gitea-init']['command'][0]
        config = directory / 'app.ini'
        config.touch()
        script = script.replace('$$', '$').replace('/data/gitea/conf/app.ini', str(config))
        binary = directory / 'gitea'
        binary.write_text(
            f'#!{sys.executable}\nimport os, sys\n'
            'mode = os.environ["FAIL_STAGE"]\n'
            'args = sys.argv[1:]\n'
            'if args[:3] == ["admin", "user", "create"] and mode == "user": sys.exit(23)\n'
            'if args[:3] == ["admin", "auth", "list"] and mode == "update": print("ID Name Type Enabled\\n1 Keycloak OAuth2 true")\n'
            'if args[:3] == ["admin", "auth", "add-oauth"] and mode == "oauth": sys.exit(24)\n'
            'if args[:3] == ["admin", "auth", "update-oauth"] and mode == "update": sys.exit(25)\n')
        binary.chmod(0o700)
        for stage, expected in [('user', 23), ('oauth', 24), ('update', 25), ('none', 0)]:
            result = subprocess.run(
                ['bash', '-c', script], capture_output=True, text=True,
                env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH'],
                         FAIL_STAGE=stage))
            assert result.returncode == expected, (stage, result.stdout, result.stderr)
            assert ('gitea-init: done.' in result.stdout) == (expected == 0)
        tasks = yaml.safe_load((ROOT / 'roles/gitea/tasks/main.yml').read_text())
        guard = next(task for task in tasks if task['name'] == 'Require successful Gitea initialization')
        for state, exit_code in [('exited', 0), ('exited', 24), ('dead', 0)]:
            result, _ = run_fixture(directory, [
                {'ansible.builtin.set_fact': {
                    'gitea_init_inspect': {'rc': 0, 'stdout': f'{state} {exit_code}'}}},
                guard,
            ])
            assert (result.returncode == 0) == (state == 'exited' and exit_code == 0)


def test_acme_backup_retries_container_replacement_race():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        counter = directory / 'attempts'
        docker = directory / 'bin/docker'
        docker.parent.mkdir()
        docker.write_text(
            f'#!{sys.executable}\n'
            'import pathlib, sys\n'
            f'count_file = pathlib.Path({str(counter)!r})\n'
            'attempts = int(count_file.read_text()) + 1 if count_file.exists() else 1\n'
            'count_file.write_text(str(attempts))\n'
            'if attempts == 1:\n'
            '    sys.stderr.write("Error: No such container: traefik\\n")\n'
            '    sys.exit(1)\n'
            'pathlib.Path(sys.argv[-1]).write_bytes(b"fixture acme store")\n'
        )
        docker.chmod(0o755)
        artifacts = directory / 'artifacts'
        backup = artifacts / 'acme.json'
        tasks = yaml.safe_load((ROOT / 'roles/traefik/tasks/backup_acme.yml').read_text())
        for task in tasks:
            for item in task.get('block', []):
                if item.get('name') == 'Copy acme.json from traefik container':
                    item['delay'] = 0
        env = dict(os.environ, PATH=str(docker.parent) + os.pathsep + os.environ['PATH'],
                   ANSIBLE_STDOUT_CALLBACK='default')
        playbook = directory / 'fixture.yml'
        playbook.write_text(json.dumps([{'hosts': 'localhost', 'gather_facts': False, 'tasks': [
            {'ansible.builtin.set_fact': {
                'compose_engine_host': 'unix:///fixture/podman.sock',
                'traefik_acme_artifacts_dir': str(artifacts),
                'traefik_acme_backup_path': str(backup)}},
            *tasks,
        ]}]))
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(playbook)],
                                env=env, cwd=directory, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert backup.read_bytes() == b'fixture acme store'
        assert int(counter.read_text()) == 2


def test_arcane_routing_configured_with_traefik():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        config = directory / 'traefik'
        config.mkdir()
        traefik_template = ROOT / 'roles/traefik/templates/docker-compose.yml.j2'
        arcane_template = ROOT / 'roles/arcane/templates/docker-compose.yml.j2'
        result, _ = run_fixture(directory, [
            {'ansible.builtin.set_fact': {
                'domain_name': 'Step4.Example.', 'traefik_fqdn': 'traefik.step4.example',
                'traefik_image_tag': 'v3.7.16', 'coredns_cf_token': '', 'traefik_port_http': 80,
                'traefik_port_https': 443, 'traefik_acme_email': 'admin@example.com',
                'traefik_acme_server': 'https://acme.invalid', 'traefik_log_level': 'INFO',
                'arcane_image': 'ghcr.io/getarcaneapp/manager:v3.0.1',
                'arcane_fqdn': 'arcane.step4.example', 'arcane_port': 3552,
                'arcane_stacks_dir': '/var/lib/sophon/stacks',
                'arcane_oidc_issuer_url': 'https://auth.step4.example/realms/sophon',
            }},
            {'ansible.builtin.set_fact': {
                'compose': "{{ lookup('template', '" + str(traefik_template) + "') | from_yaml }}",
                'arcane_compose': "{{ lookup('template', '" + str(arcane_template) + "') | from_yaml }}"}},
            {'ansible.builtin.assert': {'that': [
                "'traefik.http.routers.arcane.rule=Host(`arcane.step4.example`)' in arcane_compose.services.arcane.labels",
                "'traefik.http.services.arcane.loadbalancer.server.port=3552' in arcane_compose.services.arcane.labels",
                "'traefik.http.routers.arcane.tls.certresolver=letsencrypt' in arcane_compose.services.arcane.labels",
                "'--providers.file.directory=/etc/traefik' in compose.services.traefik.command",
                "'--certificatesresolvers.letsencrypt.acme.dnschallenge.resolvers=1.1.1.1:53,8.8.8.8:53' in compose.services.traefik.command",
                "'--certificatesresolvers.letsencrypt.acme.keyType=EC256' in compose.services.traefik.command",
                "'traefik.http.routers.arcane.tls.domains[0].main=step4.example' in arcane_compose.services.arcane.labels",
                "'traefik.http.routers.arcane.tls.domains[0].sans=*.step4.example' in arcane_compose.services.arcane.labels",
                "'traefik.http.routers.dashboard.tls.domains[0].main=step4.example' in compose.services.traefik.labels",
                "'traefik.http.routers.dashboard.tls.domains[0].sans=*.step4.example' in compose.services.traefik.labels",
                "compose.services.traefik.entrypoint is not defined",
                "'resolv.conf' not in compose.services['traefik-init'].entrypoint[2]",
                "'proxy' in compose.services.traefik.networks",
            ]}},
            {'ansible.builtin.shell': "{{ compose.services['traefik-init'].entrypoint[2] | replace('/etc/traefik', '" + str(config) + "') }}"},
        ])
        assert result.returncode == 0, result.stdout + result.stderr
        assert (config / 'dynamic.yml').is_file()
        assert not (config / 'portainer.yml').exists()


def test_wildcard_certificate_labels_cover_every_certresolver_router():
    # Traefik does not apply an entrypoint-level `domains` to routers that set their own tls options, so every router
    # that asks the resolver for a certificate must name the wildcard itself or it would request a per-name certificate.
    seen = set()
    for template in sorted((ROOT / 'roles').rglob('*.j2')):
        text = template.read_text()
        for router in re.findall(r'traefik\.http\.routers\.([\w-]+)\.tls\.certresolver=letsencrypt', text):
            seen.add(router)
            assert f'traefik.http.routers.{router}.tls.domains[0].main={{{{' in text, (template.name, router)
            assert f'traefik.http.routers.{router}.tls.domains[0].sans=*.{{{{' in text, (template.name, router)
    assert seen == {'dashboard', 'arcane', 'keycloak', 'gitea', 'coredns', 'ldap-certs'}, seen


def test_ldaps_reads_the_wildcard_certificate_by_its_main_domain():
    # The certs dumper names files after the certificate's main domain, which is now the domain itself.
    text = (ROOT / 'roles/openldap/templates/docker-compose.yml.j2').read_text()
    assert 'openldap_tls_domain }}.crt' not in text and 'openldap_tls_domain }}.key' not in text
    assert text.count('{{ openldap_tls_cert_name }}.crt') == 2 and text.count('{{ openldap_tls_cert_name }}.key') == 1
    defaults = yaml.safe_load((ROOT / 'roles/openldap/defaults/main.yml').read_text())
    with tempfile.TemporaryDirectory() as temporary:
        result, _ = run_fixture(Path(temporary), [
            {'ansible.builtin.set_fact': {'openldap_domain': 'Slow.Jedi.'}},
            {'ansible.builtin.set_fact': {'cert_name': defaults['openldap_tls_cert_name'],
                                          'dns_name': defaults['openldap_tls_domain']}},
            {'ansible.builtin.assert': {'that': ["cert_name == 'slow.jedi'", "dns_name == 'ldap.Slow.Jedi.'"]}},
        ])
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == '__main__':
    test_wildcard_certificate_labels_cover_every_certresolver_router()
    test_ldaps_reads_the_wildcard_certificate_by_its_main_domain()
    test_step_settings()
    test_service_task_graph()
    test_site_configuration_and_secret_boundary()
    test_provision_preservation()
    test_domain_deferred_until_services()
    test_existing_immutable_media_are_reused_and_size_checked()
    test_service_resolution_follows_entered_domain()
    test_service_resolution_probe_uses_python_and_exact_addresses()
    test_gitea_initializer_reports_cli_failures()
    test_cloudflared_connects_only_when_a_tunnel_was_found()
    test_missing_tunnel_is_created_only_after_a_successful_empty_lookup()
    test_stale_tunnel_cnames_are_repointed_but_other_records_are_not()
    test_acme_backup_retries_container_replacement_race()
    test_arcane_routing_configured_with_traefik()
    print('Deployment step checks passed')
