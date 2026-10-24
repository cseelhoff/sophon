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


ROOT = Path(__file__).resolve().parent
STAGES = {
    'prestage': 'Prestage',
    'proxmox': 'Proxmox authentication',
    'nfs': 'NFS VM',
    'infravm': 'InfraVM',
    'coredns': 'CoreDNS',
    'traefik': 'Traefik',
    'openldap': 'OpenLDAP',
    'keycloak': 'Keycloak',
    'gitea': 'Gitea',
    'kopia': 'Kopia',
}
CELLS = {
    'prestage': [
        '_SmIZJivoSQWszKZv9Yz-16', '_SmIZJivoSQWszKZv9Yz-52',
        '_SmIZJivoSQWszKZv9Yz-53', '_SmIZJivoSQWszKZv9Yz-54',
        '_SmIZJivoSQWszKZv9Yz-55', '_SmIZJivoSQWszKZv9Yz-56',
        '_SmIZJivoSQWszKZv9Yz-57', '_SmIZJivoSQWszKZv9Yz-62',
    ],
    'proxmox': ['_SmIZJivoSQWszKZv9Yz-3'],
    'nfs': ['_SmIZJivoSQWszKZv9Yz-65'],
    'infravm': ['_SmIZJivoSQWszKZv9Yz-2', 'Ht_PiSHEylvQ-5aMCYDO-5', '_SmIZJivoSQWszKZv9Yz-12'],
    'coredns': ['497KWN3HuOLj0ZWtI1qn-3'],
    'traefik': ['BveltJdI-pWs-qTDEDWz-51'],
    'openldap': ['BveltJdI-pWs-qTDEDWz-50'],
    'keycloak': ['BveltJdI-pWs-qTDEDWz-53'],
    'gitea': ['BveltJdI-pWs-qTDEDWz-45'],
    'kopia': ['Ht_PiSHEylvQ-5aMCYDO-4'],
}
FIELDS = {
    'SOPHON_PROXMOX_HOST': ('Host', 'Proxmox', 'host', '', True),
    'SOPHON_PROXMOX_PORT': ('API port', 'Proxmox', 'number', '8006', True),
    'SOPHON_PROXMOX_NODE': ('Node', 'Proxmox', 'text', 'proxmox', True),
    'SOPHON_PROXMOX_ISO_STORAGE': ('ISO storage', 'Proxmox', 'text', 'local', True),
    'SOPHON_PROXMOX_PASSWORD': ('Password', 'Proxmox', 'password', '', True),
    'SOPHON_VNET_INTERFACE': ('Bridge', 'Network', 'text', 'vmbr0', True),
    'SOPHON_VNET_VLAN': ('VLAN', 'Network', 'number', '', False),
    'SOPHON_VNET_GATEWAY': ('Gateway', 'Network', 'ipv4', '', True),
    'SOPHON_VNET_CIDR': ('Prefix length', 'Network', 'number', '', True),
    'SOPHON_NFS_IP': ('NFS VM address', 'Network', 'ipv4', '', True),
    'SOPHON_INFRAVM_IP': ('InfraVM address', 'Network', 'ipv4', '', True),
    'SOPHON_DOMAIN': ('Domain', 'Site', 'host', '', True),
    'SOPHON_ACME_EMAIL': ('ACME email', 'Site', 'email', 'admin@example.com', False),
    'SOPHON_CF_TOKEN': ('Cloudflare API token', 'Site', 'password', '', False),
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
    'SOPHON_KOPIA_ADMIN_PASSWORD': ('Kopia admin password', 'Access', 'password', '', False),
    'SOPHON_KOPIA_REPOSITORY_PASSWORD': ('Kopia repository password', 'Access', 'password', '', False),
}
LIMITS = {'SOPHON_PROXMOX_PORT': (1, 65535), 'SOPHON_VNET_VLAN': (1, 4094), 'SOPHON_VNET_CIDR': (1, 32)}


def new_control(environment):
    return {'values': {name: environment.get(name) or definition[3] for name, definition in FIELDS.items()},
            'environment': dict(environment), 'token': secrets.token_urlsafe(32), 'processes': [],
            'diagram_labels': {}}


def settings_payload(control):
    fields = []
    for name, (label, group, kind, default, required) in FIELDS.items():
        value = control['values'][name]
        fields.append({'name': name, 'label': label, 'group': group, 'kind': kind,
                       'required': required, 'configured': bool(value),
                       'value': value,
                       'limits': LIMITS.get(name)})
    return {'fields': fields}


def validate_settings(current, changes):
    if not isinstance(changes, dict) or not changes.keys() <= FIELDS.keys():
        return None, 'Unexpected deployment setting.'
    if any(not isinstance(value, str) or len(value) > 4096 or any(ord(character) < 32 for character in value)
           for value in changes.values()):
        return None, 'Settings must be single-line strings of at most 4096 characters.'
    values = current | changes
    for name, (label, group, kind, default, required) in FIELDS.items():
        value = values[name] if kind == 'password' else values[name].strip()
        values[name] = value
        if required and not value.strip():
            return None, f'{group}: {label} ({name}) is required.'
        if kind == 'password' and value and not value.strip():
            return None, f'{label} ({name}) must not contain only whitespace.'
        if name == 'SOPHON_PORTAINER_ADMIN_PASSWORD' and len(value) < 12:
            return None, f'{label} ({name}) must contain at least 12 characters.'
        if not value:
            continue
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
        except ValueError:
            return None, f'{label} is not a valid {kind} value.'
    addresses = [ipaddress.IPv4Address(values[name]) for name in
                 ('SOPHON_VNET_GATEWAY', 'SOPHON_NFS_IP', 'SOPHON_INFRAVM_IP')]
    network = ipaddress.IPv4Network(f"{addresses[0]}/{values['SOPHON_VNET_CIDR']}", strict=False)
    if len(set(addresses)) != 3 or any(address not in network or address in
                                     (network.network_address, network.broadcast_address) for address in addresses):
        return None, 'Gateway and VM addresses must be distinct usable addresses in the same subnet.'
    return values, None


def trace_environment(directory, environment):
    environment = dict(environment)
    environment['ANSIBLE_CALLBACK_PLUGINS'] = os.pathsep.join(filter(None, [
        str(ROOT / 'callback_plugins'), environment.get('ANSIBLE_CALLBACK_PLUGINS')]))
    environment['ANSIBLE_CALLBACKS_ENABLED'] = ','.join(filter(None, [
        environment.get('ANSIBLE_CALLBACKS_ENABLED'), 'sophon_progress']))
    environment['SOPHON_PROGRESS_FILE'] = str(directory / 'status.json')
    environment['ANSIBLE_LOG_PATH'] = str(directory / 'ansible.log')
    for name in ('ANSIBLE_RUN_TAGS', 'ANSIBLE_SKIP_TAGS'):
        environment.pop(name, None)
    return environment


def shell_command(directory, values, environment):
    traced = trace_environment(directory, environment)
    exports = values | {name: traced[name] for name in (
        'ANSIBLE_CALLBACK_PLUGINS', 'ANSIBLE_CALLBACKS_ENABLED', 'SOPHON_PROGRESS_FILE', 'ANSIBLE_LOG_PATH')}
    # ponytail: one operator per trace directory; files are not process locks.
    return '\n'.join([
        '# Contains secrets. Do not share; your terminal may save this in history.',
        *[f'export {name}={shlex.quote(value)}' for name, value in exports.items()],
        'unset ANSIBLE_RUN_TAGS ANSIBLE_SKIP_TAGS',
        '(', '  umask 077',
        f'  cd -- {shlex.quote(str(ROOT))} || exit',
        f'  mkdir -p -- {shlex.quote(str(directory))} || exit',
        '  touch -- "$ANSIBLE_LOG_PATH" && chmod 600 -- "$ANSIBLE_LOG_PATH" || exit',
        '  rm -f -- "$SOPHON_PROGRESS_FILE" || exit',
        '  ansible-playbook site.yml', ')', '',
    ])


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
    if not 0 < length <= 32768:
        return '413 Content Too Large', {'error': 'Deployment settings exceed the request limit.'}
    try:
        changes = json.loads(environment['wsgi.input'].read(length))
    except TimeoutError:
        return '408 Request Timeout', {'error': 'Deployment settings were not fully received.'}
    except (ValueError, UnicodeError):
        return '400 Bad Request', {'error': 'Invalid JSON settings.'}
    values, error = validate_settings(control['values'], changes)
    if error:
        return '400 Bad Request', {'error': error}
    if environment['PATH_INFO'] == '/command':
        return '200 OK', {'command': shell_command(directory, values, control['environment'])}
    try:
        descriptor = os.open(directory / 'deploy.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'deployment.py'), '--state-dir', str(directory), '--', 'site.yml'],
                cwd=ROOT, env=control['environment'] | values, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        control['processes'].append(process)
    except OSError:
        return '500 Internal Server Error', {'error': 'Could not start Ansible. Check the controller environment.'}
    return '202 Accepted', {'state': 'running'}


def new_report():
    return {
        'run_id': str(uuid.uuid4()), 'state': 'running', 'pid': os.getpid(),
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
        body = json.dumps({'stages': STAGES, 'cells': CELLS, 'labels': control['diagram_labels'],
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
    diagram = (ROOT / 'sophon.svg').read_bytes()
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


def serve(directory, port=0):
    try:
        diagram, labels = load_diagram()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    assets = {
        '/': ((ROOT / 'dashboard' / 'index.html').read_bytes(), 'text/html; charset=utf-8'),
        '/diagram.svg': (diagram, 'image/svg+xml'),
    }
    control = new_control(os.environ)
    control['diagram_labels'] = labels
    with make_server('127.0.0.1', port, partial(application, directory, assets, control),
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
        startup_error = ('Could not start ansible-playbook. Stop the viewer, enter the project environment '
                         'with nix develop, then restart: python3 deployment.py --serve --port 41327. '
                         'If using a custom port or --state-dir, keep those options when restarting.')
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


def main():
    parser = argparse.ArgumentParser(description='Local Sophon deployment progress viewer')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'artifacts' / 'deployment')
    parser.add_argument('--serve', action='store_true', help='Serve the dashboard; deployment starts only on request')
    parser.add_argument('--port', type=int, default=0, help='Dashboard port with --serve (default: choose a free port)')
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='-- site.yml [Ansible arguments]')
    options = parser.parse_args()
    if not 0 <= options.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    directory = options.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if options.serve:
        return serve(directory, options.port)
    arguments = options.arguments
    if arguments and arguments[0] == '--':
        arguments = arguments[1:]
    if not arguments:
        parser.print_help()
        return 2
    return launch(directory, arguments)


if __name__ == '__main__':
    sys.exit(main())
