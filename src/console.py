"""Installed InfraVM's console-first service deployment entry point."""

import argparse
import getpass
import os
from pathlib import Path
import select
import sys
import termios

import deployment

NOT_SHARED = {'SOPHON_CF_TOKEN', 'SOPHON_PORTAINER_LICENSE_KEY'}
INTERNET_ONLY = {'SOPHON_ACME_EMAIL', 'SOPHON_CF_TOKEN', 'SOPHON_CF_TUNNEL_NAME'}


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
        elif name == 'SOPHON_PORTAINER_ADMIN_PASSWORD' and len(value) < 12:
            print('The Portainer admin password needs at least 12 characters.')
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Configure Sophon services on installed InfraVM')
    parser.add_argument('--run', action='store_true', help='Validate exported settings and run without prompting')
    parser.add_argument('--refresh-certificates', action='store_true',
                        help='Replace an existing Traefik certificate store with the validated local handoff')
    options = parser.parse_args(argv)
    if os.environ.get('SOPHON_DEPLOYMENT_STEP') != 'configure':
        parser.error('Start this program using sophon-deploy on installed InfraVM.')
    try:
        site = deployment.load_site_settings()
        for name, value in site.items():
            os.environ.setdefault(name, value)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    arguments = ['--step', 'configure']
    values = deployment.new_control(os.environ, 'configure')['values']
    if not options.run:
        if not sys.stdin.isatty():
            parser.error('Interactive setup requires a terminal; use --run with exported settings for automation.')
        print('Sophon Step 4: configure services on this InfraVM. Inputs are not saved.')
        print('Settings already exported in the environment are used; Enter keeps a displayed default.')
        fields = deployment.step_fields('configure')
        try:
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
                if kind == 'password' and value:
                    # Supplied by the environment or the shared password; keep it without asking again.
                    continue
                needed = required or (name == 'SOPHON_ACME_FILE' and not has_internet) or (
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
        values = deployment.stage_certificates(values)
    except (OSError, ValueError) as error:
        print(f'Certificate import failed: {error}', file=sys.stderr)
        return 2
    os.environ.pop('SOPHON_ACME_FILE', None)
    os.environ.update(values)
    extra = ['--extra-vars', 'traefik_acme_replace_existing=true'] if options.refresh_certificates else []
    return deployment.main(arguments + ['--', '4-configure.yml', *extra])


if __name__ == '__main__':
    sys.exit(main())
