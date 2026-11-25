"""Installed InfraVM's console-first service deployment entry point."""

import argparse
import getpass
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import termios

import deployment
import backup_destination

NOT_SHARED = {'SOPHON_CF_TOKEN', 'SOPHON_KOPIA_PASSWORD',
              'SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64'}
INTERNET_ONLY = {
    'SOPHON_ACME_EMAIL', 'SOPHON_CF_TOKEN', 'SOPHON_CF_TUNNEL_ENABLED', 'SOPHON_CF_TUNNEL_NAME',
    'SOPHON_CF_ACCESS_ENABLED', 'SOPHON_CF_ACCESS_EMAILS',
}


def read_secret(prompt):
    """Read a secret from the terminal, echoing one * per character."""
    descriptor = sys.stdin.fileno()
    if not os.isatty(descriptor):
        return getpass.getpass(prompt)
    original = termios.tcgetattr(descriptor)
    settings = termios.tcgetattr(descriptor)
    settings[3] &= ~(termios.ICANON | termios.ECHO)
    settings[6][termios.VMIN] = 1
    settings[6][termios.VTIME] = 0
    data = bytearray()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        termios.tcsetattr(descriptor, termios.TCSANOW, settings)
        while True:
            byte = os.read(descriptor, 1)
            if byte in (b'', b'\x04') and not data:
                raise EOFError
            if byte in (b'\r', b'\n', b''):
                break
            if byte == b'\x03':
                raise KeyboardInterrupt
            if byte == b'\x1b':
                # Swallow an escape sequence (arrow keys and similar) instead of storing it.
                while select.select([descriptor], [], [], 0.02)[0]:
                    os.read(descriptor, 1)
            elif byte in (b'\x7f', b'\x08'):
                if data:
                    while data and data[-1] & 0xC0 == 0x80:
                        data.pop()
                    if data:
                        data.pop()
                    sys.stdout.write('\b \b')
            elif byte >= b' ':
                data += byte
                if byte[0] & 0xC0 != 0x80:
                    sys.stdout.write('*')
            sys.stdout.flush()
    finally:
        termios.tcsetattr(descriptor, termios.TCSANOW, original)
        sys.stdout.write('\n')
        sys.stdout.flush()
    return data.decode('utf-8', 'replace')


def ask_yes_no(question, default=False):
    answer = input(f'{question} [{"Y/n" if default else "y/N"}] ').strip().lower()
    return default if not answer else answer.startswith('y')


def prompt_field(name, label, kind, current, required):
    """Ask until a required field has a value; Enter keeps the current value or default."""
    while True:
        if kind == 'password':
            answer = read_secret(f'{label} ({name})' + (' [configured]' if current else '') + ': ')
        else:
            answer = input(f'{label} ({name})' + (f' [{current}]' if current else '') + ': ')
        value = answer or current
        if required and not value.strip():
            print('This value is required.')
        elif name == 'SOPHON_ARCANE_ADMIN_PASSWORD' and len(value) < 12:
            print('The Arcane admin password needs at least 12 characters.')
        elif kind == 'path' and value and not Path(value).is_file():
            print(f'File not found on this VM: {value}')
        else:
            return value


def prompt_shared_password(values, fields):
    """Offer one password for every service account; returns the names it was applied to."""
    shared = [name for name, field in fields.items()
              if field[2] == 'password' and name not in NOT_SHARED and not values[name]]
    # Only offer this when a required password is still missing; the optional Gitea one just follows the answer.
    if not any(fields[name][4] for name in shared) or not ask_yes_no('Use the same password for all service accounts?'):
        return []
    while True:
        password = read_secret('Password for all accounts (12+ characters): ')
        if len(password) < 12:
            print('Use at least 12 characters.')
        elif password != read_secret('Confirm password: '):
            print('The passwords do not match.')
        else:
            break
    for name in shared:
        values[name] = password
    return shared


def _write_json_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.sophon-json-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Configure Sophon services on installed InfraVM')
    parser.add_argument('--run', action='store_true', help='Validate exported settings and run without prompting')
    parser.add_argument('--auto', action='store_true',
                        help='Unattended deployment: initialize Day 1 if SFTP repository is empty, or deploy in safe standby mode if snapshots exist')
    parser.add_argument('--seed-file', type=Path, default=Path('/run/sophon-seed/sophon-seed.json'),
                        help='Optional RAM-backed seed JSON file consumed and unlinked on startup')
    parser.add_argument('--refresh-certificates', action='store_true',
                        help='Replace an existing Traefik certificate store with the validated local handoff')
    parser.add_argument('--prepare-restore', action='store_true',
                        help='Deploy services as a read-only standby ready for sophon-restore without touching Cloudflare or writing backups')
    options = parser.parse_args(argv)
    seed_target = 'prod'
    seed_prod_network = None
    seed_sftp: dict[str, str] = {}
    if options.auto:
        options.run = True
        if options.seed_file and options.seed_file.is_file() and not options.seed_file.is_symlink():
            try:
                seed_doc = json.loads(options.seed_file.read_text(encoding='utf-8'))
            finally:
                options.seed_file.unlink(missing_ok=True)
            seed_target = str(seed_doc.get('target') or 'prod')
            if isinstance(seed_doc.get('prod_network'), dict):
                seed_prod_network = seed_doc['prod_network']
            elif isinstance(seed_doc.get('network'), dict):
                seed_prod_network = seed_doc['network']
            if isinstance(seed_doc.get('sftp'), dict):
                seed_sftp = {
                    k: v for k, v in seed_doc['sftp'].items()
                    if isinstance(k, str) and isinstance(v, str) and v
                }
            for k, v in (seed_doc.get('env') or {}).items():
                if isinstance(k, str) and isinstance(v, str) and k.startswith('SOPHON_'):
                    os.environ.setdefault(k, v)
    if os.environ.get('SOPHON_DEPLOYMENT_STEP') != 'configure':
        parser.error('Start this program using sophon-deploy on installed InfraVM.')
    try:
        site = deployment.load_site_settings()
        for name, value in site.items():
            if name in ('SOPHON_INFRAVM_IP', 'SOPHON_VNET_DNS') and value:
                os.environ[name] = value
            else:
                os.environ.setdefault(name, value)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    prepare_restore = (
        options.prepare_restore
        or seed_target == 'dev'
        or os.environ.get('SOPHON_PREPARE_RESTORE', '').strip().lower() == 'true'
    )
    arguments = ['--step', 'configure']
    values = deployment.new_control(os.environ, 'configure')['values']
    backup_key_b64 = seed_sftp.get('backup_private_key_b64') or (
        values.get('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', '')
        if values.get('SOPHON_KOPIA_SFTP_USERNAME') == 'sophon-backup' else ''
    )
    desired_standby = {
        'domain': values.get('SOPHON_DOMAIN', ''),
        'desired_cf_tunnel_enabled': values.get('SOPHON_CF_TUNNEL_ENABLED', 'true') == 'true' and bool(values.get('SOPHON_CF_TOKEN', '').strip()),
        'desired_cf_token': values.get('SOPHON_CF_TOKEN', '').strip(),
        'desired_cf_tunnel_name': values.get('SOPHON_CF_TUNNEL_NAME', 'sophon'),
        'desired_cf_access_enabled': values.get('SOPHON_CF_ACCESS_ENABLED', 'true') == 'true',
        'desired_cf_access_emails': values.get('SOPHON_CF_ACCESS_EMAILS', '').strip(),
        'desired_acme_email': values.get('SOPHON_ACME_EMAIL', 'admin@example.com'),
        'desired_backups_enabled': values.get('SOPHON_KOPIA_BACKUPS_ENABLED', 'true') == 'true',
        'desired_snapshot_interval': values.get('SOPHON_KOPIA_SNAPSHOT_INTERVAL', '6h'),
        'desired_coarse_enabled': values.get('SOPHON_KOPIA_COARSE_ENABLED', 'false') == 'true',
        'desired_coarse_volumes_dir': values.get('SOPHON_KOPIA_COARSE_VOLUMES_DIR', '/var/home/sophon/.local/share/containers/storage/volumes'),
        'desired_coarse_work_dir': values.get('SOPHON_KOPIA_COARSE_WORK_DIR', '/var/home/sophon/sophon-coarse'),
    }
    if seed_prod_network is not None:
        desired_standby['prod_network'] = seed_prod_network
    if backup_key_b64:
        desired_standby['desired_sftp_username'] = 'sophon-backup'
        desired_standby['desired_sftp_private_key_b64'] = backup_key_b64
    if prepare_restore:
        if seed_sftp.get('restore_private_key_b64'):
            values['SOPHON_KOPIA_SFTP_USERNAME'] = 'sophon-restore'
            values['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64'] = seed_sftp['restore_private_key_b64']
        values['SOPHON_CF_TOKEN'] = ''
        values['SOPHON_CF_TUNNEL_ENABLED'] = 'false'
        values['SOPHON_KOPIA_BACKUPS_ENABLED'] = 'false'
        values['SOPHON_KOPIA_COARSE_ENABLED'] = 'false'
        values['SOPHON_KOPIA_CREATE_REPOSITORY'] = 'false'
    if not options.run:
        if not sys.stdin.isatty():
            parser.error('Interactive setup requires a terminal; use --run with exported settings for automation.')
        print('Sophon Step 4: configure services on this InfraVM. Service passwords are not saved.')
        print('SFTP client identity and confirmed host pins are kept under project credentials/kopia.')
        print('Settings already exported in the environment are used; Enter keeps a displayed default.')
        fields = deployment.step_fields('configure')
        try:
            if prepare_restore:
                has_internet = False
                skipped = INTERNET_ONLY | {'SOPHON_ACME_FILE', 'SOPHON_KOPIA_BACKUPS_ENABLED',
                                           'SOPHON_KOPIA_SNAPSHOT_INTERVAL', 'SOPHON_KOPIA_COARSE_ENABLED'}
            else:
                # Offline sites supply a pre-issued certificate file (from the secret disk) instead of Let's Encrypt access.
                # Connected is the normal case; only an explicit "n" selects the pre-issued certificate path.
                has_internet = ask_yes_no('Does this site have internet access?', default=True)
                skipped = {'SOPHON_ACME_FILE'} if has_internet else INTERNET_ONLY
                if not has_internet:
                    values['SOPHON_CF_TOKEN'] = ''
                    if not values['SOPHON_ACME_FILE']:
                        print('No certificate disk was found. Attach the secret ISO (label SOPHON_CERTS) to this VM and '
                              'run sophon-deploy again, or give the path of an acme.json already under /workspace.')
                else:
                    values['SOPHON_ACME_FILE'] = ''
            prompt_shared_password(values, {name: field for name, field in fields.items() if name not in skipped})
            for name, (label, group, kind, default, required) in fields.items():
                value = values[name]
                if value and site.get(name):
                    # Saved when the network was set up at the start of this step; do not ask twice.
                    print(f'{label} ({name}): {value} (saved network)')
                    continue
                if name in skipped:
                    continue
                if name == 'SOPHON_CF_TUNNEL_NAME' and values.get('SOPHON_CF_TUNNEL_ENABLED') != 'true':
                    continue
                if name in (
                    *backup_destination.MATERIAL,
                    'SOPHON_KOPIA_CREATE_REPOSITORY',
                    'SOPHON_CF_ACCESS_ENABLED',
                    'SOPHON_CF_ACCESS_EMAILS',
                    'SOPHON_KEYCLOAK_REGISTRATION_ALLOWED',
                    'SOPHON_NEXUS_ANONYMOUS_ENABLED',
                ):
                    continue
                if name in ('SOPHON_KOPIA_COARSE_VOLUMES_DIR', 'SOPHON_KOPIA_COARSE_WORK_DIR') and values['SOPHON_KOPIA_COARSE_ENABLED'] != 'true':
                    continue
                if kind == 'password' and value:
                    # Supplied by the environment or the shared password; keep it without asking again.
                    continue
                needed = required or (name == 'SOPHON_ACME_FILE' and not has_internet and not prepare_restore) or (
                    name == 'SOPHON_CF_TOKEN' and has_internet)
                values[name] = prompt_field(name, label, kind, value, needed)
            values, error = deployment.validate_settings(values, {}, 'configure')
            if error:
                print(f'Configuration rejected: {error}', file=sys.stderr)
                return 2
        except (EOFError, KeyboardInterrupt):
            print('\nDeployment cancelled; no service changes were made.', file=sys.stderr)
            return 130
        except (OSError, ValueError) as error:
            print(f'Certificate input failed: {error}', file=sys.stderr)
            return 2
    else:
        values, error = deployment.validate_settings(values, {}, 'configure')
        if error:
            print(f'Configuration rejected: {error}', file=sys.stderr)
            return 2
    try:
        values = backup_destination.prepare(values, root=deployment.ROOT,
                                            confirm=None if options.run else ask_yes_no,
                                            readonly=prepare_restore,
                                            auto=options.auto)
        auto_mode = values.pop('_SOPHON_AUTO_MODE', None)
        if auto_mode == 'standby_restore':
            prepare_restore = True
            if seed_sftp.get('restore_private_key_b64'):
                values['SOPHON_KOPIA_SFTP_USERNAME'] = 'sophon-restore'
                values['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64'] = seed_sftp['restore_private_key_b64']
            values['SOPHON_CF_TOKEN'] = ''
            values['SOPHON_CF_TUNNEL_ENABLED'] = 'false'
            values['SOPHON_KOPIA_BACKUPS_ENABLED'] = 'false'
            values['SOPHON_KOPIA_COARSE_ENABLED'] = 'false'
            values['SOPHON_KOPIA_CREATE_REPOSITORY'] = 'false'
        values = deployment.stage_certificates(values)
        values['SOPHON_ARCANE_ENCRYPTION_KEY'] = deployment.ensure_arcane_encryption_key(deployment.ROOT)
    except KeyboardInterrupt:
        print('\nConfigure cancelled; no service stacks were deployed. Retained SFTP state is available for retry.',
              file=sys.stderr)
        return 130
    except EOFError:
        print('Configure preflight ended before completion; no service stacks were deployed.', file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'Configure preflight failed: {error}', file=sys.stderr)
        return 2
    standby_file = deployment.ROOT / 'standby.json'
    desired_standby['domain'] = values.get('SOPHON_DOMAIN', desired_standby['domain'])
    desired_standby['sftp_username'] = values.get('SOPHON_KOPIA_SFTP_USERNAME', '')
    if prepare_restore:
        _write_json_private(standby_file, desired_standby)
    else:
        standby_file.unlink(missing_ok=True)
    os.environ.pop('SOPHON_ACME_FILE', None)
    os.environ.update(values)
    os.environ['SOPHON_KOPIA_PREFLIGHT_COMPLETE'] = 'true'
    if prepare_restore:
        os.environ['SOPHON_PREPARE_RESTORE'] = 'true'
    else:
        os.environ.pop('SOPHON_PREPARE_RESTORE', None)
    extra = ['--extra-vars', 'traefik_acme_replace_existing=true'] if options.refresh_certificates else []
    rc = deployment.main(arguments + ['--', '4-configure.yml', *extra])
    if rc == 0:
        _write_json_private(
            deployment.ROOT / 'deploy-result.json',
            {
                'mode': auto_mode or ('standby_restore' if prepare_restore else 'day1'),
                'domain': values.get('SOPHON_DOMAIN', ''),
                'infravm_ip': values.get('SOPHON_INFRAVM_IP', ''),
                'prepare_restore': bool(prepare_restore),
            },
        )
    return rc


if __name__ == '__main__':
    sys.exit(main())
