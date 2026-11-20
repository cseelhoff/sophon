"""Container entry point: `docker run ... sophon-tools <command> [args]`.

Runs inside the tools image from the mounted project (/workspace), so the same
docker run command works from POSIX sh, bash, PowerShell or cmd.
"""
from pathlib import Path
import os
import re
import sys

import console

ENV_KEY_RE = re.compile(r'^(?:SOPHON_[A-Z0-9_]*|CF_DNS_API_TOKEN)$')
HYPERVISOR_COMMANDS = frozenset({
    'provision', 'provision-dev', 'dev', 'provision-backup', 'backup', 'destroy-vm', 'verify-restore',
})


def load_dotenv(path: Path = Path('.env')) -> dict[str, str]:
    """Ensure .env is mode 0600, reject symlinks, and populate unset SOPHON_* env vars."""
    if path.is_symlink():
        raise ValueError(f'refusing to read symlinked env file: {path}')
    if not path.is_file():
        return {}
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if not ENV_KEY_RE.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _need(name: str, label: str, default: str = '') -> None:
    if os.environ.get(name, '').strip():
        return
    if not sys.stdin.isatty():
        raise SystemExit(f'{label} ({name}) is required; set it in .env.')
    suffix = f' [{default}]' if default else ''
    answer = input(f'{label}{suffix}: ').strip() or default
    if not answer:
        raise SystemExit(f'{label} is required.')
    os.environ[name] = answer


def _need_secret(name: str) -> None:
    if os.environ.get(name, '').strip():
        return
    if not sys.stdin.isatty():
        raise SystemExit(f'{name} is required; set it in .env.')
    value = console.read_secret(f'{name}: ')
    if not value:
        raise SystemExit(f'{name} is required.')
    os.environ[name] = value


def ensure_hypervisor_inputs() -> None:
    if not sys.stdin.isatty():
        return
    hv = os.environ.get('SOPHON_HYPERVISOR', '').strip().lower()
    if not hv:
        print('Where should the VM run?\n  1) Proxmox\n  2) VMware vCenter\n  3) Microsoft Hyper-V (WinRM)')
        choice = input('Choose 1-3: ').strip()
        hv = {'1': 'proxmox', '2': 'vcenter', '3': 'hyperv'}.get(choice, '')
        if not hv:
            raise SystemExit('Choose a number from 1 to 3.')
        os.environ['SOPHON_HYPERVISOR'] = hv
    if hv == 'vcenter':
        _need('SOPHON_VCENTER_URL', 'vCenter URL (https://...)')
        _need('SOPHON_VCENTER_USERNAME', 'vCenter username', 'administrator@vsphere.local')
        _need('SOPHON_VCENTER_DATACENTER', 'Datacenter')
        _need('SOPHON_VCENTER_CLUSTER', 'Cluster')
        _need('SOPHON_VCENTER_DATASTORE', 'Datastore')
        _need('SOPHON_VCENTER_NETWORK', 'Port group')
        _need_secret('SOPHON_VCENTER_PASSWORD')
    elif hv == 'hyperv':
        _need('SOPHON_HYPERV_HOST', 'Hyper-V host name or address')
        _need('SOPHON_HYPERV_USERNAME', 'Hyper-V username', 'Administrator')
        _need('SOPHON_HYPERV_SWITCH', 'Virtual switch')
        _need_secret('SOPHON_HYPERV_PASSWORD')
    else:
        _need('SOPHON_PROXMOX_HOST', 'Proxmox host name or address')
        _need('SOPHON_PROXMOX_NODE', 'Proxmox node', 'proxmox')
        _need_secret('SOPHON_PROXMOX_PASSWORD')


def _build_isos_command(args: list[str]) -> list[str]:
    force = '--force' in args
    os_only = '--os-only' in args
    secrets_only = '--secrets-only' in args
    cleaned = [a for a in args if a not in ('--force', '--os-only', '--secrets-only')]
    prep_extra = ' -e prepare_force=true' if force else ''
    if os_only:
        return ['ansible-playbook', '1-prepare.yml', *(['-e', 'prepare_force=true'] if force else []), *cleaned]
    if secrets_only:
        return ['python3', 'src/secrets_iso.py', *cleaned]
    return ['sh', '-eu', '-c', f'python3 src/secrets_iso.py "$@" && ansible-playbook 1-prepare.yml{prep_extra}', 'build-isos', *cleaned]


def _provision_command(args: list[str]) -> list[str]:
    if '--recreate' in args:
        cleaned = [a for a in args if a != '--recreate']
        return ['python3', 'src/verify_restore.py', '--prod-recreate', *cleaned]
    return ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '2-provision.yml']


COMMANDS = {
    'build-isos': _build_isos_command,
    'prepare': lambda args: ['ansible-playbook', '1-prepare.yml', *args],
    'secrets': lambda args: ['python3', 'src/secrets_iso.py', *args],
    'certificates': lambda args: ['python3', 'src/certificates.py', *args],
    'provision': _provision_command,
    'provision-backup': lambda args: ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '2-provision-backup.yml'],
    'backup': lambda args: ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '2-provision-backup.yml'],
    'provision-dev': lambda args: ['python3', 'src/verify_restore.py', *args],
    'dev': lambda args: ['python3', 'src/verify_restore.py', *args],
    'destroy-vm': lambda args: ['python3', 'src/verify_restore.py', '--destroy-only', *args],
    'verify-restore': lambda args: ['python3', 'src/verify_restore.py', *args],
    # </dev/null: the wizard tests assume no terminal, and `docker run -it` would make them wait for keystrokes.
    'test': lambda args: ['sh', '-c', 'exec </dev/null; python3 -m unittest discover -s tests "$@" && '
                          'python3 tests/test_deployment.py >/dev/null && python3 tests/test_steps.py', 'sh', *args],
    'shell': lambda args: ['bash', *args],
}

HELP_BUILD = """usage: sh sophon-build-isos.sh [options | command]

  (default)               build mode-0600 unattended secrets ISOs + CoreOS, apps, and SFTP server ISOs
  --force                 also force-rebuild cached CoreOS, apps, and SFTP server ISOs
  --os-only               build only secret-free CoreOS, apps, and SFTP server ISOs (skip secrets ISOs)
  --secrets-only          build only mode-0600 unattended secrets ISOs (sophon-secrets.iso & sophon-sftp-secrets.iso)
  certificates issue ...  air-gapped renewal: issue/renew wildcard TLS certificate into artifacts/secrets/sophon-secrets.iso"""

HELP_PROVISION = """usage: sh sophon-provision.sh [options | command]

  (default)               deploy the SFTP Backup VM (when sophon-sftp-secrets.iso exists) and production InfraVM
  --recreate              replace the production InfraVM in place (boots in safe standby until sophon-activate)
  dev [--wait-https]      recreate and restore the dev/drill InfraVM on SOPHON_DEV_NETWORK_CIDR in safe standby mode
  backup                  deploy only the standalone Alpine SFTP Backup Server VM
  destroy-vm [--vm-name]  stop and delete a Sophon-managed VM (default: SOPHON_INFRAVM_NAME)"""

HELP = f'{HELP_BUILD}\n\n{HELP_PROVISION}\n\n  test | shell            development helpers'


def main(argv: list[str]) -> int:
    entry = os.environ.get('SOPHON_ENTRYPOINT', '').strip()
    if not argv or argv[0] in ('help', '-h', '--help'):
        print(HELP_BUILD if entry == 'build-isos' else HELP_PROVISION if entry == 'provision' else HELP)
        return 0
    cmd_name = argv[0]
    if os.environ.get('SOPHON_LOAD_DOTENV') == '1' and cmd_name not in ('test', 'shell'):
        try:
            load_dotenv()
        except ValueError as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 1
        if cmd_name in HYPERVISOR_COMMANDS:
            ensure_hypervisor_inputs()
    command = COMMANDS[cmd_name](argv[1:]) if cmd_name in COMMANDS else argv
    os.execvp(command[0], command)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
