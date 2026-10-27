import argparse
from functools import partial
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
from socketserver import ThreadingMixIn
import subprocess
import sys
import tempfile
import time
import uuid
from wsgiref.simple_server import WSGIServer, make_server
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / 'dashboard'
STAGES = {
    'prepare': 'Prepare media',
    'proxmox': 'Proxmox authentication',
    'vcenter': 'vCenter authentication',
    'workstation': 'VMware Workstation',
    'infravm': 'InfraVM',
    'coredns': 'CoreDNS',
    'traefik': 'Traefik',
    'openldap': 'OpenLDAP',
    'keycloak': 'Keycloak',
    'gitea': 'Gitea',
}
CELLS = {
    'prepare': [
        '_SmIZJivoSQWszKZv9Yz-52',
        '_SmIZJivoSQWszKZv9Yz-53', '_SmIZJivoSQWszKZv9Yz-54',
        '_SmIZJivoSQWszKZv9Yz-55', '_SmIZJivoSQWszKZv9Yz-56',
        '_SmIZJivoSQWszKZv9Yz-57', '_SmIZJivoSQWszKZv9Yz-62',
    ],
    'proxmox': ['_SmIZJivoSQWszKZv9Yz-3'],
    'vcenter': [],
    'workstation': [],
    'infravm': ['_SmIZJivoSQWszKZv9Yz-2', 'Ht_PiSHEylvQ-5aMCYDO-5'],
    'coredns': ['497KWN3HuOLj0ZWtI1qn-3'],
    'traefik': ['BveltJdI-pWs-qTDEDWz-51'],
    'openldap': ['BveltJdI-pWs-qTDEDWz-50'],
    'keycloak': ['BveltJdI-pWs-qTDEDWz-53'],
    'gitea': ['BveltJdI-pWs-qTDEDWz-45'],
}
FIELDS = {
    'SOPHON_HYPERVISOR': ('Hypervisor', 'Hypervisor', 'choice', 'proxmox', True),
    'SOPHON_PROXMOX_HOST': ('Host', 'Proxmox', 'host', '', True),
    'SOPHON_PROXMOX_PORT': ('API port', 'Proxmox', 'number', '8006', True),
    'SOPHON_PROXMOX_NODE': ('Node', 'Proxmox', 'text', 'proxmox', True),
    'SOPHON_PROXMOX_ISO_STORAGE': ('ISO storage', 'Proxmox', 'text', 'local', True),
    'SOPHON_PROXMOX_DISK_STORAGE': ('VM disk storage', 'Proxmox', 'text', 'local-lvm', True),
    'SOPHON_PROXMOX_PASSWORD': ('Password', 'Proxmox', 'password', '', True),
    'SOPHON_VCENTER_URL': ('HTTPS URL', 'vCenter', 'url', '', True),
    'SOPHON_VCENTER_USERNAME': ('Username', 'vCenter', 'text', 'administrator@vsphere.local', True),
    'SOPHON_VCENTER_PASSWORD': ('Password', 'vCenter', 'password', '', True),
    'SOPHON_VCENTER_CA_FILE': ('CA PEM file (on provisioning laptop)', 'vCenter', 'text', '', False),
    'SOPHON_VCENTER_DATACENTER': ('Datacenter', 'vCenter', 'text', '', True),
    'SOPHON_VCENTER_CLUSTER': ('Cluster', 'vCenter', 'text', '', True),
    'SOPHON_VCENTER_DATASTORE': ('Shared datastore', 'vCenter', 'text', '', True),
    'SOPHON_VCENTER_NETWORK': ('Port group (already configured for its VLAN)', 'vCenter', 'text', '', True),
    'SOPHON_VCENTER_FOLDER': ('VM folder (empty uses datacenter VM root)', 'vCenter', 'text', '', False),
    'SOPHON_WORKSTATION_NETWORK': ('Network mode', 'Workstation', 'choice', 'bridged', True),
    'SOPHON_VNET_INTERFACE': ('Bridge', 'Network', 'text', 'vmbr0', True),
    'SOPHON_VNET_VLAN': ('VLAN', 'Network', 'number', '', False),
    'SOPHON_INFRAVM_CORES': ('CPU cores', 'VM', 'number', '8', True),
    'SOPHON_INFRAVM_NAME': ('VM name', 'VM', 'text', 'sophon-infravm', True),
    'SOPHON_INFRAVM_MEMORY': ('Memory (MiB)', 'VM', 'number', '8192', True),
    'SOPHON_INFRAVM_DISK_GB': ('Disk size (GiB)', 'VM', 'number', '100', True),
    'SOPHON_VNET_DNS': ('Upstream DNS', 'Site', 'ipv4', '', False),
    'SOPHON_INFRAVM_IP': ('InfraVM address', 'Site', 'ipv4', '', True),
    'SOPHON_DOMAIN': ('Domain', 'Site', 'host', '', True),
    'SOPHON_ACME_EMAIL': ('ACME email', 'Site', 'email', 'admin@example.com', False),
    'SOPHON_CF_TOKEN': ('Cloudflare API token', 'Site', 'password', '', False),
    'SOPHON_ACME_PAYLOAD': ('Pre-issued certificates: base64 acme.json (optional; contains private keys)',
                            'Site', 'certificate', '', False),
    'SOPHON_CF_TUNNEL_NAME': ('Cloudflare tunnel name', 'Site', 'text', 'sophon', False),
    'SOPHON_KEYCLOAK_REALM': ('Keycloak realm', 'Access', 'text', 'sophon', False),
    'SOPHON_KEYCLOAK_REALM_DISPLAY_NAME': ('Realm display name', 'Access', 'text', '', False),
    'SOPHON_KEYCLOAK_ADMIN_PASSWORD': ('Keycloak admin password', 'Access', 'password', '', True),
    'SOPHON_KEYCLOAK_DB_PASSWORD': ('Keycloak database password', 'Access', 'password', '', True),
    'SOPHON_KEYCLOAK_PORTAINER_CLIENT_SECRET': ('Portainer OIDC client secret', 'Access', 'password', '', True),
    'SOPHON_GITEA_ADMIN_PASSWORD': ('Gitea admin password', 'Access', 'password', '', True),
    'SOPHON_OPENLDAP_ADMIN_PASSWORD': ('LDAP admin password', 'Access', 'password', '', True),
    'SOPHON_OPENLDAP_EXAMPLE_USER_PASSWORD': ('LDAP seed-user password', 'Access', 'password', '', True),
    'SOPHON_OPENLDAP_EXAMPLE_ADMIN_PASSWORD': ('LDAP seed-admin password', 'Access', 'password', '', True),
    'SOPHON_PORTAINER_LICENSE_KEY': ('Portainer licence key', 'Access', 'password', '', False),
    'SOPHON_PORTAINER_ADMIN_PASSWORD': ('Portainer admin password', 'Access', 'password', '', True),
    'SOPHON_GITEA_DB_PASSWORD': ('Gitea database password', 'Access', 'password', '', False),
}
LIMITS = {'SOPHON_PROXMOX_PORT': (1, 65535), 'SOPHON_VNET_VLAN': (1, 4094),
          'SOPHON_INFRAVM_CORES': (1, 256), 'SOPHON_INFRAVM_MEMORY': (1024, 1048576),
          'SOPHON_INFRAVM_DISK_GB': (40, 1048576)}
STEP_STAGES = {
    'provision': ('prepare', 'proxmox', 'infravm'),
    'configure': ('infravm', 'coredns', 'traefik', 'openldap', 'keycloak', 'gitea'),
}
CHOICES = {'SOPHON_HYPERVISOR': ('proxmox', 'vcenter', 'workstation'),
           'SOPHON_WORKSTATION_NETWORK': ('bridged', 'nat', 'hostonly')}
PROVISION_GROUPS = ('Hypervisor', 'Proxmox', 'vCenter', 'Workstation', 'Network', 'VM')
SITE_FIELDS = tuple(name for name, field in FIELDS.items()
                    if field[2] not in ('password', 'certificate') and field[1] not in PROVISION_GROUPS and name != 'SOPHON_DOMAIN')


def field_provider(group):
    return ('proxmox' if group in ('Proxmox', 'Network') else
            'vcenter' if group == 'vCenter' else 'workstation' if group == 'Workstation' else None)


def step_stages(step, provider='proxmox'):
    return ('prepare', provider, 'infravm') if step == 'provision' else STEP_STAGES[step]


def step_fields(step='provision'):
    if step == 'provision':
        return {name: field for name, field in FIELDS.items() if field[1] in PROVISION_GROUPS}
    return {name: field for name, field in FIELDS.items()
            if field[1] not in PROVISION_GROUPS}


def new_control(environment, step='provision'):
    environment = {name: value for name, value in environment.items()
                   if not name.startswith('SOPHON_') or name in step_fields(step)}
    values = {name: environment.get(name) or definition[3] for name, definition in step_fields(step).items()}
    environment.pop('SOPHON_ACME_PAYLOAD', None)
    return {'values': values,
            'environment': environment | {'SOPHON_DEPLOYMENT_STEP': step},
            'token': secrets.token_urlsafe(32), 'processes': [],
            'diagram_labels': {}, 'step': step}


def settings_payload(control):
    fields = []
    for name, (label, group, kind, default, required) in step_fields(control['step']).items():
        value = control['values'][name]
        fields.append({'name': name, 'label': label, 'group': group, 'kind': kind,
                       'required': required, 'configured': bool(value),
                       'value': '' if kind == 'certificate' else value,
                       'provider': field_provider(group), 'choices': CHOICES.get(name),
                       'limits': LIMITS.get(name)})
    return {'fields': fields}


def validate_settings(current, changes, step='provision'):
    definitions = step_fields(step)
    if not isinstance(changes, dict) or not changes.keys() <= definitions.keys():
        return None, 'Unexpected deployment setting.'
    if any(not isinstance(value, str)
           or len(value) > (2 * 1024 * 1024 if name == 'SOPHON_ACME_PAYLOAD' else 4096)
           or (name != 'SOPHON_ACME_PAYLOAD' and any(ord(character) < 32 for character in value))
           for name, value in changes.items()):
        return None, 'Settings must be bounded strings: 4096 characters per single-line setting, or 2 MiB for certificates.'
    values = current | changes
    provider = values.get('SOPHON_HYPERVISOR', 'proxmox').strip()
    if provider not in CHOICES['SOPHON_HYPERVISOR']:
        return None, 'Hypervisor must be proxmox, vcenter or workstation.'
    for name, (label, group, kind, default, required) in definitions.items():
        if field_provider(group) not in (None, provider):
            continue
        value = values[name] if kind in ('password', 'certificate') else values[name].strip()
        values[name] = value
        if required and not value.strip():
            return None, f'{group}: {label} ({name}) is required.'
        if kind == 'password' and value and not value.strip():
            return None, f'{label} ({name}) must not contain only whitespace.'
        if name == 'SOPHON_PORTAINER_ADMIN_PASSWORD' and len(value) < 12:
            return None, f'{label} ({name}) must contain at least 12 characters.'
        if not value:
            continue
        if name in CHOICES and value not in CHOICES[name]:
            return None, f'{label} must be one of: {", ".join(CHOICES[name])}.'
        try:
            if kind == 'ipv4':
                ipaddress.IPv4Address(value)
            elif kind == 'number':
                minimum, maximum = LIMITS[name]
                if not minimum <= int(value) <= maximum:
                    return None, f'{label} must be between {minimum} and {maximum}.'
            elif kind == 'host':
                if len(value) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                                               for part in value.rstrip('.').split('.')):
                    return None, f'{label} must be a hostname or IPv4 address, without a URL scheme or port.'
            elif kind == 'email' and ('@' not in value or any(character.isspace() for character in value)):
                return None, f'{label} must be an email address.'
            elif kind == 'url':
                url = urlsplit(value)
                if (url.scheme != 'https' or not url.hostname or url.username or url.password
                        or url.path not in ('', '/', '/sdk') or url.query or url.fragment):
                    return None, f'{label} must be HTTPS without embedded credentials, query or fragment.'
            elif name == 'SOPHON_INFRAVM_NAME' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', value):
                return None, 'VM name must contain 1-80 letters, digits, dots, underscores or hyphens.'
        except ValueError:
            return None, f'{label} is not a valid {kind} value.'
    if values.get('SOPHON_ACME_PAYLOAD'):
        import certificates
        try:
            certificates.decode_payload(values['SOPHON_ACME_PAYLOAD'], values['SOPHON_DOMAIN'])
        except (ValueError, OSError) as error:
            return None, str(error)
    return values, None


def stage_certificates(values):
    values = dict(values)
    encoded = values.pop('SOPHON_ACME_PAYLOAD', '')
    if encoded:
        import certificates
        certificates.import_payload(encoded, values['SOPHON_DOMAIN'], ROOT / 'artifacts/traefik/acme.json')
    return values


def trace_environment(directory, environment):
    environment = dict(environment)
    environment['ANSIBLE_CALLBACK_PLUGINS'] = os.pathsep.join(filter(None, [
        str(ROOT / 'callback_plugins'), environment.get('ANSIBLE_CALLBACK_PLUGINS')]))
    environment['ANSIBLE_CALLBACKS_ENABLED'] = ','.join(filter(None, [
        environment.get('ANSIBLE_CALLBACKS_ENABLED'), 'sophon_progress']))
    environment['SOPHON_PROGRESS_FILE'] = str(directory / 'status.json')
    environment['ANSIBLE_LOG_PATH'] = str(directory / 'ansible.log')
    environment.pop('SOPHON_ACME_PAYLOAD', None)
    for name in ('ANSIBLE_RUN_TAGS', 'ANSIBLE_SKIP_TAGS'):
        environment.pop(name, None)
    return environment


def shell_command(directory, values, environment, step='provision'):
    exports = {name: value for name, value in values.items() if name != 'SOPHON_ACME_PAYLOAD'}
    exports['SOPHON_DEPLOYMENT_STEP'] = step
    lines = [
        '# Contains secrets. Do not share; your terminal may save this in history.',
        'unset ' + ' '.join(name for name in FIELDS if name not in step_fields(step)),
        *[f'export {name}={shlex.quote(value)}' for name, value in exports.items()],
    ]
    if step == 'provision' and values.get('SOPHON_HYPERVISOR') == 'workstation':
        powershell = [f"$env:{name} = '{value.replace(chr(39), chr(39) * 2)}'"
                      for name, value in exports.items()]
        powershell.append(".\\sophon.ps1 provision")
        return '\n'.join([
            '# Contains secrets. Do not share; your terminal may save this in history.',
            *powershell, '',
        ])
    if step == 'configure':
        return '\n'.join(lines + [
            f'sophon-controller --run --state-dir {shlex.quote(str(directory))}', ''])
    # Bash command, run from the project directory; names without values pass the exported secrets through.
    passed = ' '.join(f'-e {name}' for name in exports)
    return '\n'.join(lines + [
        'docker run --rm --init -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock '
        f'-v sophon-state:/state {passed} sophon-tools provision', ''])


def action_request(directory, control, environment):
    origin = f"http://127.0.0.1:{environment['SERVER_PORT']}"
    if (environment.get('HTTP_ORIGIN') != origin or
        not secrets.compare_digest(environment.get('HTTP_X_SOPHON_TOKEN', '').encode(), control['token'].encode())):
        return '403 Forbidden', {'error': 'Deployment request is not authorized by this dashboard.'}
    if environment.get('CONTENT_TYPE', '').split(';')[0].strip() != 'application/json':
        return '415 Unsupported Media Type', {'error': 'A JSON request is required.'}
    try:
        length = int(environment.get('CONTENT_LENGTH', '0'))
    except ValueError:
        return '400 Bad Request', {'error': 'Invalid request length.'}
    if not 0 < length <= 2 * 1024 * 1024 + 32768:
        return '413 Content Too Large', {'error': 'Deployment settings exceed the request limit.'}
    try:
        changes = json.loads(environment['wsgi.input'].read(length))
    except TimeoutError:
        return '408 Request Timeout', {'error': 'Deployment settings were not fully received.'}
    except (ValueError, UnicodeError):
        return '400 Bad Request', {'error': 'Invalid JSON settings.'}
    values, error = validate_settings(control['values'], changes, control['step'])
    if error:
        return '400 Bad Request', {'error': error}
    try:
        values = stage_certificates(values)
    except (OSError, ValueError):
        return '500 Internal Server Error', {'error': 'Could not stage private certificates. Check project permissions and identity paths.'}
    if environment['PATH_INFO'] == '/command':
        return '200 OK', {'command': shell_command(directory, values, control['environment'], control['step'])}
    try:
        descriptor = os.open(directory / 'deploy.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--state-dir', str(directory),
                 '--step', control['step'],
                 '--', '2-provision.yml' if control['step'] == 'provision' else '4-configure.yml'],
                cwd=ROOT, env=control['environment'] | values, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        control['processes'].append(process)
    except OSError:
        return '500 Internal Server Error', {'error': 'Could not start Ansible. Check the controller environment.'}
    return '202 Accepted', {'state': 'running'}


def new_report():
    return {
        'run_id': str(uuid.uuid4()), 'state': 'running', 'pid': os.getpid(),
        'hypervisor': os.environ.get('SOPHON_HYPERVISOR') or 'proxmox',
        'started_at': time.time(), 'updated_at': time.time(), 'current': None,
        'stages': {stage: {'state': 'pending', 'ok': 0, 'skipped': 0,
                           'ignored': 0, 'rescued': 0, 'tasks': []} for stage in STAGES},
    }


def write_report(path, report):
    report['updated_at'] = time.time()
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.progress-')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(report, output)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_report(directory):
    path = directory / 'status.json'
    try:
        report = json.loads(path.read_text())
    except FileNotFoundError:
        report = new_report()
        report.update(state='idle', run_id='', started_at=0, updated_at=0, pid=0)
        return report
    except (OSError, ValueError):
        report = new_report()
        report['state'] = 'unknown'
        return report
    if report['state'] == 'running':
        try:
            pid = report.get('pid', 0)
            if pid <= 0:
                raise ProcessLookupError
            os.kill(pid, 0)
        except ProcessLookupError:
            report['state'] = 'unknown'
            if report['current']:
                report['stages'][report['current']]['state'] = 'unknown'
        except PermissionError:
            pass
    return report


def application(directory, assets, control, environment, start_response):
    control['processes'][:] = [process for process in control['processes'] if process.poll() is None]
    headers = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
               ('Referrer-Policy', 'no-referrer'),
               ('Content-Security-Policy', "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'self'; frame-src 'self'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'self'")]
    address = f"127.0.0.1:{environment['SERVER_PORT']}"
    if environment.get('HTTP_HOST') != address:
        start_response('403 Forbidden', headers)
        return [b'Forbidden']
    path = environment['PATH_INFO']
    if environment['REQUEST_METHOD'] == 'POST' and path in ('/command', '/run'):
        status, payload = action_request(directory, control, environment)
        body = json.dumps(payload).encode()
        start_response(status, headers + [('Content-Type', 'application/json'), ('Content-Length', str(len(body)))])
        return [body]
    if environment['REQUEST_METHOD'] != 'GET':
        start_response('403 Forbidden', headers)
        return [b'Forbidden']
    if path == '/status.json':
        body, content_type = json.dumps(read_report(directory)).encode(), 'application/json'
    elif path == '/settings.json':
        if not secrets.compare_digest(environment.get('HTTP_X_SOPHON_TOKEN', '').encode(), control['token'].encode()):
            start_response('403 Forbidden', headers)
            return [b'Forbidden']
        body, content_type = json.dumps(settings_payload(control)).encode(), 'application/json'
    elif path == '/config.json':
        stages = {key: ('Portainer' if key == 'infravm' and control['step'] == 'configure' else STAGES[key])
                  for key in step_stages(control['step'], control['values'].get('SOPHON_HYPERVISOR', 'proxmox'))}
        body = json.dumps({'stages': stages, 'cells': {key: CELLS[key] for key in stages},
                           'providers': {key: {'label': STAGES[key], 'cells': CELLS[key]}
                                         for key in CHOICES['SOPHON_HYPERVISOR']},
                           'step': control['step'], 'labels': control['diagram_labels'],
                           'token': control['token'], 'log_path': str(directory / 'deploy.log')}).encode()
        content_type = 'application/json'
    elif path in assets:
        body, content_type = assets[path]
    else:
        start_response('404 Not Found', headers)
        return [b'Not found']
    if path == '/diagram.svg':
        headers = [(key, value) for key, value in headers if key != 'Content-Security-Policy']
        headers.append(('Content-Security-Policy', "default-src 'none'; img-src data:; "
                        "style-src 'unsafe-inline'; script-src 'none'; frame-ancestors 'self'"))
    start_response('200 OK', headers + [('Content-Type', content_type),
                                      ('Content-Length', str(len(body)))])
    return [body]


class DashboardServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def load_diagram():
    diagram = (ASSETS / 'sophon.svg').read_bytes()
    root = ET.fromstring(diagram)
    cells = {node.get('data-cell-id'): node for node in root.iter() if node.get('data-cell-id')}
    cell_ids = cells.keys()
    missing = [cell for cells in CELLS.values() for cell in cells if cell not in cell_ids]
    if missing:
        raise ValueError('SVG mapping missing cells: ' + ', '.join(missing))
    labels = {}
    for cell, node in cells.items():
        if any(child.get('data-cell-id') for child in node.iter() if child is not node):
            continue
        text_nodes = node.findall('.//{http://www.w3.org/2000/svg}foreignObject')
        if not text_nodes:
            text_nodes = node.findall('.//{http://www.w3.org/2000/svg}text')
        label = ' '.join(' '.join(text.itertext()) for text in text_nodes)
        if label.strip():
            labels[cell] = ' '.join(label.split())
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ET.register_namespace('xhtml', 'http://www.w3.org/1999/xhtml')
    style = ET.Element('{http://www.w3.org/2000/svg}style')
    style.text = (
        ':root { color-scheme: dark !important; background: #151c20 !important; '
        '--ge-dark-color: #151c20; } '
        'foreignObject > div { background-color: transparent !important; color: #e6edf3 !important; }'
    )
    root.insert(0, style)
    return ET.tostring(root, encoding='utf-8'), labels


def serve(directory, port=0, step='provision', bind='127.0.0.1'):
    try:
        diagram, labels = load_diagram()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    assets = {
        '/': ((ASSETS / 'index.html').read_bytes(), 'text/html; charset=utf-8'),
        '/diagram.svg': (diagram, 'image/svg+xml'),
    }
    control = new_control(os.environ, step)
    control['diagram_labels'] = labels
    # Requests are still only accepted for Host/Origin 127.0.0.1:<port>; --bind only lets a container publish the port.
    with make_server(bind, port, partial(application, directory, assets, control),
                     server_class=DashboardServer) as server:
        print(f'http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()
    return 0


def launch(directory, arguments):
    unsupported = {'--check', '-C', '--syntax-check', '--list-tasks', '--list-tags',
                   '--list-hosts', '--start-at-task', '--tags', '-t', '--skip-tags'}
    if any(argument.split('=')[0] in unsupported for argument in arguments):
        print('The deployment viewer requires a full, non-check-mode run.', file=sys.stderr)
        return 2
    report = new_report()
    write_report(directory / 'status.json', report)
    environment = trace_environment(directory, os.environ)
    descriptor = os.open(environment['ANSIBLE_LOG_PATH'], os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    process = None
    interrupted = False
    startup_error = None

    def forward_signal(number, frame):
        nonlocal interrupted
        interrupted = True
        if process is not None:
            process.send_signal(number)

    previous_signals = {number: signal.signal(number, forward_signal)
                        for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        process = subprocess.Popen(['ansible-playbook', *arguments], env=environment)
        exit_code = process.wait()
    except OSError:
        startup_error = ('Could not start ansible-playbook. Run the dashboard with the sophon-tools container '
                         '(docker run ... sophon-tools dashboard), or install ansible-core.')
        print(startup_error, file=sys.stderr)
        exit_code = 127
    finally:
        for number, handler in previous_signals.items():
            signal.signal(number, handler)
    report = json.loads((directory / 'status.json').read_text())
    if report['state'] == 'running' or interrupted:
        report['state'] = 'interrupted' if interrupted or exit_code < 0 else 'failed' if exit_code else 'unknown'
        if report['current']:
            report['stages'][report['current']]['state'] = report['state']
    elif exit_code != 0:
        report['state'] = 'failed'
    report['exit_code'] = 128 - exit_code if exit_code < 0 else exit_code
    if startup_error:
        report['error'] = startup_error
    write_report(directory / 'status.json', report)
    return report['exit_code']


def load_site_settings():
    site_file = ROOT / 'site.json'
    if not site_file.exists():
        return {}
    site = json.loads(site_file.read_text())
    if not isinstance(site, dict) or not site.keys() <= set(SITE_FIELDS) or any(
            not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value)
            for value in site.values()):
        raise ValueError('site.json must contain only supported non-secret site settings')
    return site


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local Sophon deployment progress viewer')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--serve', action='store_true', help='Serve the dashboard; deployment starts only on request')
    parser.add_argument('--port', type=int, default=0, help='Dashboard port with --serve (default: choose a free port)')
    parser.add_argument('--bind', default='127.0.0.1', help='Dashboard bind address (a container publishing the port needs 0.0.0.0)')
    parser.add_argument('--step', choices=STEP_STAGES, default=os.environ.get('SOPHON_DEPLOYMENT_STEP', 'provision'),
                        help='provision on the laptop; configure on installed InfraVM')
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='-- 4-configure.yml [Ansible arguments]')
    options = parser.parse_args(argv)
    try:
        for name, value in load_site_settings().items():
            os.environ.setdefault(name, value)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if options.step == 'configure':
        if os.environ.get('SOPHON_DEPLOYMENT_STEP') != 'configure':
            parser.error('Start step 4 using sophon-controller on installed InfraVM.')
    elif (os.environ.get('SOPHON_HYPERVISOR') or 'proxmox') not in CHOICES['SOPHON_HYPERVISOR']:
        parser.error('SOPHON_HYPERVISOR must be proxmox, vcenter or workstation.')
    state_root = Path(os.environ.get('SOPHON_STATE_DIR') or ROOT / 'artifacts')
    for name in list(os.environ):
        if name.startswith('SOPHON_') and name not in step_fields(options.step):
            os.environ.pop(name, None)
    os.environ['SOPHON_DEPLOYMENT_STEP'] = options.step
    if not 0 <= options.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    directory = (options.state_dir or state_root / 'deployment' / options.step).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if options.serve:
        return serve(directory, options.port, options.step, options.bind)
    arguments = options.arguments
    if arguments and arguments[0] == '--':
        arguments = arguments[1:]
    if not arguments:
        parser.print_help()
        return 2
    expected = '2-provision.yml' if options.step == 'provision' else '4-configure.yml'
    if arguments[0] in ('2-provision.yml', '4-configure.yml') and arguments[0] != expected:
        parser.error(f'{options.step} requires {expected}; choose the correct deployment step.')
    return launch(directory, arguments)


if __name__ == '__main__':
    sys.exit(main())
