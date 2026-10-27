"""Sophon settings and the playbook launcher shared by the laptop (steps 1-2) and the installed InfraVM (step 4)."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
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
    'SOPHON_HYPERV_HOST': ('Host (WinRM)', 'Hyper-V', 'host', '', True),
    'SOPHON_HYPERV_PORT': ('WinRM port (5985 HTTP, 5986 HTTPS)', 'Hyper-V', 'number', '5985', True),
    'SOPHON_HYPERV_USERNAME': ('Username', 'Hyper-V', 'text', 'Administrator', True),
    'SOPHON_HYPERV_PASSWORD': ('Password', 'Hyper-V', 'password', '', True),
    'SOPHON_HYPERV_CA_FILE': ('CA PEM file for HTTPS (on provisioning laptop)', 'Hyper-V', 'text', '', False),
    'SOPHON_HYPERV_SWITCH': ('Virtual switch (already configured for its VLAN)', 'Hyper-V', 'text', '', True),
    'SOPHON_HYPERV_PATH': ('VM folder on the host (empty uses the host default)', 'Hyper-V', 'text', '', False),
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
LIMITS = {'SOPHON_PROXMOX_PORT': (1, 65535), 'SOPHON_HYPERV_PORT': (1, 65535), 'SOPHON_VNET_VLAN': (1, 4094),
          'SOPHON_INFRAVM_CORES': (1, 256), 'SOPHON_INFRAVM_MEMORY': (1024, 1048576),
          'SOPHON_INFRAVM_DISK_GB': (40, 1048576)}
STEPS = ('provision', 'configure')
CHOICES = {'SOPHON_HYPERVISOR': ('proxmox', 'vcenter', 'hyperv')}
PROVISION_GROUPS = ('Hypervisor', 'Proxmox', 'vCenter', 'Hyper-V', 'Network', 'VM')
SITE_FIELDS = tuple(name for name, field in FIELDS.items()
                    if field[2] not in ('password', 'certificate') and field[1] not in PROVISION_GROUPS and name != 'SOPHON_DOMAIN')


PROVIDER_GROUPS = {'Proxmox': 'proxmox', 'Network': 'proxmox', 'vCenter': 'vcenter', 'Hyper-V': 'hyperv'}


def field_provider(group):
    return PROVIDER_GROUPS.get(group)


def step_fields(step='provision'):
    if step == 'provision':
        return {name: field for name, field in FIELDS.items() if field[1] in PROVISION_GROUPS}
    return {name: field for name, field in FIELDS.items()
            if field[1] not in PROVISION_GROUPS}


def new_control(environment, step='provision'):
    values = {name: environment.get(name) or definition[3] for name, definition in step_fields(step).items()}
    return {'values': values, 'step': step}


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
        return None, 'Hypervisor must be proxmox, vcenter or hyperv.'
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


def launch(arguments):
    environment = dict(os.environ)
    # The certificate payload is staged to a file; the playbook never needs it in its environment.
    environment.pop('SOPHON_ACME_PAYLOAD', None)
    try:
        return subprocess.run(['ansible-playbook', *arguments], env=environment).returncode
    except OSError:
        print('Could not start ansible-playbook. Run this through the sophon-tools container '
              '(bash sophon-provision.sh) or install ansible-core.', file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a Sophon playbook with only the settings its step uses')
    parser.add_argument('--step', choices=STEPS, default=os.environ.get('SOPHON_DEPLOYMENT_STEP', 'provision'),
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
        parser.error('SOPHON_HYPERVISOR must be proxmox, vcenter or hyperv.')
    for name in list(os.environ):
        if name.startswith('SOPHON_') and name not in step_fields(options.step):
            os.environ.pop(name, None)
    os.environ['SOPHON_DEPLOYMENT_STEP'] = options.step
    arguments = options.arguments
    if arguments and arguments[0] == '--':
        arguments = arguments[1:]
    if not arguments:
        parser.print_help()
        return 2
    expected = '2-provision.yml' if options.step == 'provision' else '4-configure.yml'
    if arguments[0] in ('2-provision.yml', '4-configure.yml') and arguments[0] != expected:
        parser.error(f'{options.step} requires {expected}; choose the correct deployment step.')
    return launch(arguments)


if __name__ == '__main__':
    sys.exit(main())
