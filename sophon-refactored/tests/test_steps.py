import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from deployment import new_control, step_fields, settings_payload, validate_settings
from test_deployment import run_fixture


def test_step_settings():
    secrets = {
        'SOPHON_PROXMOX_PASSWORD': 'MANAGEMENT_SECRET',
        'SOPHON_VCENTER_PASSWORD': 'VCENTER_SECRET',
        'SOPHON_VCENTER_URL': 'https://management.example',
        'SOPHON_CONTROLLER_PASSWORD': 'LOGIN_SECRET',
        'SOPHON_CF_TOKEN': 'SERVICE_SECRET',
        'SOPHON_DOMAIN': 'configure.example',
    }
    provision = new_control(secrets, 'provision')
    configure = new_control(secrets, 'configure')
    assert 'SERVICE_SECRET' not in json.dumps(settings_payload(provision))
    assert 'MANAGEMENT_SECRET' not in json.dumps(configure)
    assert 'VCENTER_SECRET' not in json.dumps(configure)
    assert 'management.example' not in json.dumps(configure)
    assert 'LOGIN_SECRET' not in json.dumps(configure)
    assert 'SOPHON_PROXMOX_HOST' not in step_fields('configure')
    assert 'SOPHON_PORTAINER_ADMIN_PASSWORD' not in step_fields('provision')
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
                assert role not in ('proxmox', 'vcenter', 'prepare'), role
                metadata = ROOT / 'roles' / role / 'meta/main.yml'
                if metadata.exists():
                    walk(metadata)
                walk(ROOT / 'roles' / role / 'tasks' / (value.get('tasks_from', 'main') + '.yml'))
            if module in ('ansible.builtin.include_tasks', 'ansible.builtin.import_tasks'):
                filename = value if isinstance(value, str) else value['file']
                assert '{{' not in filename, filename
                walk(path.parent / filename)

    walk(ROOT / '4-configure.yml')
    assert ROOT / 'roles/portainer/tasks/main.yml' in visited
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
                   '--state-dir', str(directory / 'state'), '--', '4-configure.yml']
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
        assert result.returncode == 2 and 'sophon-controller' in result.stderr


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
                               for name in ('', 'traefik', 'auth', 'ldap', 'git', 'portainer', 'dns')]
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


def test_gitea_initializer_reports_cli_failures():
    defaults = yaml.safe_load((ROOT / 'roles/gitea/defaults/main.yml').read_text())
    defaults.update(
        domain_name='site.example', portainer_admin_password='fixture',
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
                    'gitea_init_inspect': {'json': {'State': {'Status': state, 'ExitCode': exit_code}}}}},
                guard,
            ])
            assert (result.returncode == 0) == (state == 'exited' and exit_code == 0)


def test_acme_backup_retries_container_replacement_race():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(500 if len(requests) == 1 else 200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            self.wfile.write(b'container was replaced' if len(requests) == 1 else b'fixture archive')

        def log_message(self, *args):
            pass

    tasks = yaml.safe_load((ROOT / 'roles/traefik/tasks/backup_acme.yml').read_text())
    download = next(task for task in tasks
                    if task['name'] == 'Download acme.json archive from traefik container')
    with HTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory() as temporary:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            directory = Path(temporary)
            archive = directory / 'acme.tar'
            result, _ = run_fixture(directory, [
                {'ansible.builtin.set_fact': {
                    'infravm_portainer_url': f'http://127.0.0.1:{server.server_port}',
                    '_traefik_portainer_endpoint_id': 1, '_traefik_portainer_token': 'fixture',
                    '_traefik_acme_dl_tar': {'path': str(archive)}}},
                download | {'delay': 0},
            ])
            assert result.returncode == 0, result.stdout + result.stderr
            assert archive.read_bytes() == b'fixture archive'
            assert len(requests) == 2
        finally:
            server.shutdown()
            thread.join()


def test_portainer_routing_configured_with_traefik():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        config = directory / 'traefik'
        config.mkdir()
        template = ROOT / 'roles/traefik/templates/docker-compose.yml.j2'
        result, _ = run_fixture(directory, [
            {'ansible.builtin.set_fact': {
                'domain_name': 'Step4.Example.', 'traefik_fqdn': 'traefik.step4.example',
                'traefik_image_tag': 'v3.2', 'coredns_cf_token': '', 'traefik_port_http': 80,
                'traefik_port_https': 443, 'traefik_acme_email': 'admin@example.com',
                'traefik_acme_server': 'https://acme.invalid', 'traefik_log_level': 'INFO',
            }},
            {'ansible.builtin.set_fact': {'compose': "{{ lookup('template', '" + str(template) + "') | from_yaml }}"}},
            {'ansible.builtin.assert': {'that': [
                "'traefik.http.routers.portainer.rule=Host(`portainer.step4.example`)' in compose.services.traefik.labels",
                "'traefik.http.routers.portainer.service=portainer@file' in compose.services.traefik.labels",
                "'traefik.http.routers.portainer.tls.certresolver=letsencrypt' in compose.services.traefik.labels",
                "'--providers.file.directory=/etc/traefik' in compose.services.traefik.command",
                "'--certificatesresolvers.letsencrypt.acme.dnschallenge.resolvers=1.1.1.1:53,8.8.8.8:53' in compose.services.traefik.command",
                "compose.services.traefik.entrypoint is not defined",
                "'resolv.conf' not in compose.services['traefik-init'].entrypoint[2]",
                "'proxy' in compose.services.traefik.networks",
            ]}},
            {'ansible.builtin.shell': "{{ compose.services['traefik-init'].entrypoint[2] | replace('/etc/traefik', '" + str(config) + "') }}"},
        ])
        assert result.returncode == 0, result.stdout + result.stderr
        dynamic = yaml.safe_load((config / 'portainer.yml').read_text())
        assert dynamic['http']['services']['portainer']['loadBalancer']['servers'] == [
            {'url': 'http://portainer:9000'}]


if __name__ == '__main__':
    test_step_settings()
    test_service_task_graph()
    test_site_configuration_and_secret_boundary()
    test_provision_preservation()
    test_domain_deferred_until_services()
    test_existing_immutable_media_are_reused_and_size_checked()
    test_service_resolution_follows_entered_domain()
    test_service_resolution_probe_uses_python_and_exact_addresses()
    test_gitea_initializer_reports_cli_failures()
    test_acme_backup_retries_container_replacement_race()
    test_portainer_routing_configured_with_traefik()
    print('Deployment step checks passed')
