"""Installed InfraVM's console-first service deployment entry point."""

import argparse
import getpass
import os
from pathlib import Path
import sys
import termios

import deployment


def read_certificate(prompt):
    if not os.isatty(sys.stdin.fileno()):
        return getpass.getpass(prompt)
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    settings = termios.tcgetattr(descriptor)
    settings[3] &= ~termios.ICANON
    settings[6][termios.VMIN] = 1
    settings[6][termios.VTIME] = 0
    try:
        # Canonical TTY input truncates lines at about 4 KiB; certificate stores are much larger.
        termios.tcsetattr(descriptor, termios.TCSANOW, settings)
        return getpass.getpass(prompt)
    finally:
        termios.tcsetattr(descriptor, termios.TCSANOW, original)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Configure Sophon services on installed InfraVM')
    parser.add_argument('--run', action='store_true', help='Validate exported settings and run without prompting')
    parser.add_argument('--refresh-certificates', action='store_true',
                        help='Replace an existing Traefik certificate store with the validated local handoff')
    options = parser.parse_args(argv)
    if os.environ.get('SOPHON_DEPLOYMENT_STEP') != 'configure':
        parser.error('Start this program using sophon-controller on installed InfraVM.')
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
        print('Empty input keeps the displayed default or an already supplied secret.')
        try:
            for name, (label, group, kind, default, required) in deployment.step_fields('configure').items():
                value = values[name]
                if value and site.get(name):
                    # Already chosen during installation (step 3); do not ask twice.
                    print(f'{label} ({name}): {value} (from installation)')
                    continue
                if kind == 'certificate':
                    answer = read_certificate(f'{label} (or @/workspace/path/to/payload.txt): ')
                    if answer.startswith('@'):
                        answer = Path(answer[1:]).read_text()
                elif kind == 'password':
                    answer = getpass.getpass(f'{label} ({name})' + (' [configured]' if value else '') + ': ')
                else:
                    answer = input(f'{label} ({name})' + (f' [{value}]' if value else '') + ': ')
                if answer:
                    values[name] = answer
            values, error = deployment.validate_settings(values, {}, 'configure')
            if error:
                print(f'Configuration rejected: {error}', file=sys.stderr)
                return 2
            if input(f'Deploy services through Portainer at {values["SOPHON_INFRAVM_IP"]}:9443? [y/N] ').strip().lower() != 'y':
                print('Deployment cancelled; no service changes were made.')
                return 0
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
    os.environ.pop('SOPHON_ACME_PAYLOAD', None)
    os.environ.update(values)
    extra = ['--extra-vars', 'traefik_acme_replace_existing=true'] if options.refresh_certificates else []
    return deployment.main(arguments + ['--', '4-configure.yml', *extra])


if __name__ == '__main__':
    sys.exit(main())
