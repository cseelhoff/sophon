"""Container entry point: `docker run ... sophon-tools <command> [args]`.

Runs inside the tools image from the mounted project (/workspace), so the same
docker run command works from POSIX sh, bash, PowerShell or cmd.
"""
from pathlib import Path
import os
import re
import sys
import tempfile

import console

ENV_KEY_RE = re.compile(r'^(?:SOPHON_[A-Z0-9_]*|CF_DNS_API_TOKEN)$')
PENDING_DOTENV_JSON = '/tmp/sophon-build-dotenv.json'
HYPERVISOR_COMMANDS = frozenset({
    'provision', 'provision-dev', 'dev', 'provision-backup', 'backup', 'destroy-vm', 'mount-secrets', 'verify-restore',
})


def format_dotenv_value(value: str) -> str:
    """Serialize a single-line `.env` value so `load_dotenv` and PowerShell `Load-EnvFile` read it back verbatim."""
    if '\n' in value or '\r' in value:
        raise ValueError('Refusing to write multi-line value into .env.')
    if not value:
        return ''
    needs_quotes = (
        value != value.strip()
        or (len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"))
    )
    if not needs_quotes:
        return value
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


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
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace('\\\\', '\\')
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded[key] = value
    return loaded


def save_dotenv(
    updates: dict[str, str],
    path: Path = Path('.env'),
    template_path: Path | None = Path('.env.example'),
) -> Path:
    """Persist SOPHON_* decisions to `.env` atomically with mode 0600 while preserving existing lines/comments."""
    if path.is_symlink():
        raise ValueError(f'refusing to write symlinked env file: {path}')
    remaining = {
        key: str(val)
        for key, val in updates.items()
        if ENV_KEY_RE.match(key) and val is not None
    }
    if path.is_file():
        base_lines = path.read_text(encoding='utf-8').splitlines()
    elif template_path is not None and not template_path.is_symlink() and template_path.is_file():
        base_lines = template_path.read_text(encoding='utf-8').splitlines()
    else:
        base_lines = []

    out_lines: list[str] = []
    for raw in base_lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key, _ = stripped.split('=', 1)
            if key in remaining:
                out_lines.append(f'{key}={format_dotenv_value(remaining.pop(key))}')
                continue
        out_lines.append(raw)

    extra = [(key, val) for key, val in remaining.items() if val != '']
    if extra:
        if out_lines and out_lines[-1].strip() != '':
            out_lines.append('')
        out_lines.append('# --- Saved interactive settings ------------------------------------------------------------------------------')
        for key, val in extra:
            out_lines.append(f'{key}={format_dotenv_value(val)}')

    parent = path.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_stat = parent.stat()
    payload = ('\n'.join(out_lines).rstrip('\n') + '\n').encode('utf-8')
    fd, tmp_name = tempfile.mkstemp(prefix='.dotenv-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            if hasattr(os, 'fchown') and hasattr(os, 'geteuid') and os.geteuid() == 0:
                try:
                    os.fchown(handle.fileno(), parent_stat.st_uid, parent_stat.st_gid)
                except OSError:
                    pass
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return path


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


def ensure_hypervisor_inputs(cmd_name: str = 'provision') -> None:
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
        if cmd_name not in ('destroy-vm', 'mount-secrets'):
            _need('SOPHON_VCENTER_CLUSTER', 'Cluster')
        if cmd_name != 'destroy-vm':
            _need('SOPHON_VCENTER_DATASTORE', 'Datastore')
        if cmd_name not in ('destroy-vm', 'mount-secrets'):
            _need('SOPHON_VCENTER_NETWORK', 'Port group')
        _need_secret('SOPHON_VCENTER_PASSWORD')
    elif hv == 'hyperv':
        _need('SOPHON_HYPERV_HOST', 'Hyper-V host name or address')
        _need('SOPHON_HYPERV_USERNAME', 'Hyper-V username', 'Administrator')
        if cmd_name != 'mount-secrets':
            _need('SOPHON_HYPERV_SWITCH', 'Virtual switch')
        _need_secret('SOPHON_HYPERV_PASSWORD')
    else:
        _need('SOPHON_PROXMOX_HOST', 'Proxmox host name or address')
        _need('SOPHON_PROXMOX_NODE', 'Proxmox node', 'proxmox')
        _need_secret('SOPHON_PROXMOX_PASSWORD')
    if cmd_name in ('provision', 'provision-dev', 'dev', 'verify-restore'):
        _need('SOPHON_INFRAVM_DISK_GB', 'InfraVM thin virtual disk size in GiB (min 40)', '1024')
    backup_seed = Path(os.environ.get('SOPHON_BACKUP_SECRETS_ISO', '').strip() or 'artifacts/secrets/sophon-sftp-secrets.iso')
    will_provision_backup = (
        cmd_name in ('provision-backup', 'backup')
        or (
            cmd_name == 'provision'
            and os.environ.get('SOPHON_ATTACH_SECRETS_ISO', 'true').strip().lower() not in ('0', 'false', 'no', 'off')
            and os.environ.get('SOPHON_SKIP_BACKUP_VM', 'false').strip().lower() not in ('1', 'true', 'yes')
            and backup_seed.is_file()
        )
    )
    if will_provision_backup:
        _need('SOPHON_BACKUP_VM_DISK_GB', 'Backup VM thin virtual disk size in GiB (min 16)', '1536')


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
    script = (
        f'trap "rm -f {PENDING_DOTENV_JSON}" EXIT; '
        f'python3 src/secrets_iso.py --defer-dotenv {PENDING_DOTENV_JSON} "$@" && '
        f'ansible-playbook 1-prepare.yml{prep_extra} && '
        f'python3 src/secrets_iso.py --apply-dotenv {PENDING_DOTENV_JSON}'
    )
    return ['sh', '-eu', '-c', script, 'build-isos', *cleaned]


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
    'mount-secrets': lambda args: ['python3', 'src/mount_secrets.py', *args],
    'verify-restore': lambda args: ['python3', 'src/verify_restore.py', *args],
    # </dev/null: the wizard tests assume no terminal, and `docker run -it` would make them wait for keystrokes.
    'test': lambda args: ['sh', '-c', 'exec </dev/null; python3 -m unittest discover -s tests "$@" && '
                          'python3 tests/test_deployment.py >/dev/null && python3 tests/test_steps.py', 'sh', *args],
    'shell': lambda args: ['bash', *args],
}

HELP_BUILD = """usage: sh sophon-build-isos.sh [options | command]

  (default)               build unattended secrets ISOs (auto-renews TLS certs <= 65d left) + CoreOS/apps/SFTP ISOs
  --renew-certs           force immediate wildcard TLS certificate renewal into artifacts/secrets/sophon-secrets.iso
  --force                 also force-rebuild cached CoreOS, apps, and SFTP server ISOs
  --os-only               build only secret-free CoreOS, apps, and SFTP server ISOs (skip secrets ISOs)
  --secrets-only          build only mode-0600 unattended secrets ISOs (sophon-secrets.iso & sophon-sftp-secrets.iso)
  certificates issue ...  standalone certificate-only renewal into artifacts/secrets/sophon-secrets.iso"""

HELP_PROVISION = """usage: sh sophon-provision.sh [options | command]

  (default)               deploy the SFTP Backup VM (when sophon-sftp-secrets.iso exists) and production InfraVM
  --recreate              replace the production InfraVM in place (boots in safe standby until activated)
  backup                  deploy only the standalone Alpine SFTP Backup Server VM"""

HELP_PROVISION_DEV = """usage: sh sophon-provision-dev.sh [options]

  (default)               recreate and restore the dev/drill InfraVM on SOPHON_DEV_NETWORK_CIDR in safe standby mode
  --wait-https            wait until restored HTTPS services pass SNI readiness checks from this workstation
  --secrets-iso <path>    use a specific unattended secrets ISO (default: artifacts/secrets/sophon-secrets.iso)"""

HELP_DESTROY_VM = """usage: sh sophon-destroy-vm.sh [options]

  (default)               stop and delete SOPHON_INFRAVM_NAME (prompts for confirmation on production/backup VMs)
  --vm-name <name>        override target VM name (for example: sophon-dev-infravm)
  --yes                   skip interactive confirmation"""

HELP_MOUNT_SECRETS = """usage: sh sophon-mount-secrets.sh [options]

  (default)               upload and hot-mount sophon-secrets.iso onto SOPHON_INFRAVM_NAME, then delete the local ISO
  --vm-name <name>        override target VM name (for example: sophon-dev-infravm)
  --secrets-iso <path>    use a specific secrets ISO (default: artifacts/secrets/sophon-secrets.iso)
  --keep-local            keep the local workstation sophon-secrets.iso copy after mounting"""

HELP_BY_ENTRYPOINT = {
    'build-isos': HELP_BUILD,
    'provision': HELP_PROVISION,
    'provision-dev': HELP_PROVISION_DEV,
    'destroy-vm': HELP_DESTROY_VM,
    'mount-secrets': HELP_MOUNT_SECRETS,
}

HELP = f'{HELP_BUILD}\n\n{HELP_PROVISION}\n\n{HELP_PROVISION_DEV}\n\n{HELP_MOUNT_SECRETS}\n\n{HELP_DESTROY_VM}\n\n  test | shell            development helpers'


def main(argv: list[str]) -> int:
    entry = os.environ.get('SOPHON_ENTRYPOINT', '').strip()
    if not argv or argv[0] in ('help', '-h', '--help'):
        print(HELP_BY_ENTRYPOINT.get(entry, HELP))
        return 0
    cmd_name = argv[0]
    if os.environ.get('SOPHON_LOAD_DOTENV') == '1' and cmd_name not in ('test', 'shell'):
        try:
            load_dotenv()
        except ValueError as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 1
        if cmd_name in HYPERVISOR_COMMANDS:
            ensure_hypervisor_inputs(cmd_name)
    command = COMMANDS[cmd_name](argv[1:]) if cmd_name in COMMANDS else argv
    os.execvp(command[0], command)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
