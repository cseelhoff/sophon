"""Deploy/recreate a dev restore-drill InfraVM (`provision-dev` / `verify-restore`) or replace prod (`provision --recreate`).

`provision-dev` always stops and deletes any existing `SOPHON_DEV_INFRAVM_NAME` (default
`sophon-dev-infravm`) across Proxmox, VMware vCenter, or Microsoft Hyper-V, derives a temporary
dev seed ISO configured with `SOPHON_DEV_NETWORK_CIDR` (which also serves as its self-pointing DNS)
and the confined read-only `sophon-restore` SFTP key while preserving the production network/key
metadata for `sophon-activate`, and boots a clean dev InfraVM in Safe Standby Mode.
"""

import argparse
import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import deployment
import secrets_iso

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEV_SECRETS_ISO = ROOT / 'artifacts/secrets/sophon-dev-secrets.iso'


def inspect_seed_iso(iso_path: Path) -> dict:
    """Validate that `iso_path` exists and extract non-secret metadata from `/sophon-seed.json`."""
    iso_path = Path(os.path.abspath(iso_path))
    if not iso_path.is_file() or iso_path.is_symlink():
        raise ValueError(
            f'Missing unattended secrets ISO: {iso_path}\n'
            'Generate one first with: bash sophon-provision.sh secrets'
        )
    if not shutil.which('xorriso'):
        raise ValueError('xorriso is missing; run this through the sophon-tools container.')
    with tempfile.TemporaryDirectory(prefix='sophon-inspect-seed-') as work:
        seed_out = Path(work) / 'sophon-seed.json'
        res = subprocess.run(
            ['xorriso', '-osirrox', 'on', '-indev', str(iso_path), '-extract', '/sophon-seed.json', str(seed_out)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0 or not seed_out.is_file():
            raise ValueError(
                f'{iso_path} does not contain /sophon-seed.json (it appears to be a certificate-only ISO).\n'
                'Build an unattended seed ISO with: bash sophon-provision.sh secrets'
            )
        doc = json.loads(seed_out.read_text(encoding='utf-8'))
    net = doc.get('network') or {}
    dev_net = doc.get('dev_network') or {}
    env = doc.get('env') or {}
    cidr = net.get('cidr', '')
    ip = cidr.split('/')[0] if '/' in cidr else env.get('SOPHON_INFRAVM_IP', '')
    dev_cidr = dev_net.get('cidr', '')
    dev_ip = dev_cidr.split('/')[0] if '/' in dev_cidr else ''
    return {
        'target': doc.get('target', 'prod'),
        'domain': env.get('SOPHON_DOMAIN', ''),
        'network_mode': net.get('mode', 'dhcp'),
        'ip': ip,
        'dev_ip': dev_ip,
        'has_dev_network': bool(dev_net),
        'sftp_username': env.get('SOPHON_KOPIA_SFTP_USERNAME', ''),
    }


def _sni_https_get(ip: str, hostname: str, path: str, ca_file: str = '', timeout: float = 10.0) -> tuple[int, bytes]:
    ctx = ssl.create_default_context(cafile=ca_file or None)
    with socket.create_connection((ip, 443), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=hostname) as tls_sock:
            conn = http.client.HTTPSConnection(hostname, 443, timeout=timeout, context=ctx)
            conn.sock = tls_sock
            conn.request('GET', path, headers={'Host': hostname, 'User-Agent': 'sophon-verify-restore/2.0'})
            resp = conn.getresponse()
            body = resp.read(65536)
            return resp.status, body


def wait_for_https_ready(ip: str, domain: str, *, ca_file: str = '', timeout_sec: int = 1800, poll_sec: float = 10.0) -> None:
    """Poll Keycloak OIDC discovery, Gitea API version, and Arcane UI over SNI HTTPS until all pass."""
    checks = (
        (f'auth.{domain}', '/realms/master/.well-known/openid-configuration'),
        (f'git.{domain}', '/api/v1/version'),
        (f'arcane.{domain}', '/'),
    )
    deadline = time.monotonic() + timeout_sec
    last_error = 'waiting for VM boot and restore'
    while time.monotonic() < deadline:
        all_ok = True
        for host, path in checks:
            try:
                status, _body = _sni_https_get(ip, host, path, ca_file=ca_file)
                if status != 200:
                    all_ok = False
                    last_error = f'https://{host}{path} returned HTTP {status}'
                    break
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                all_ok = False
                last_error = f'https://{host}{path}: {exc}'
                break
        if all_ok:
            return
        time.sleep(poll_sec)
    raise TimeoutError(f'Timed out after {timeout_sec}s waiting for restored HTTPS services at {ip}: {last_error}')


def run_playbook(playbook: str, extra_env: dict[str, str]) -> int:
    saved = {k: os.environ.get(k) for k in extra_env}
    try:
        for k, v in extra_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return deployment.main(['--step', 'provision', '--', playbook])
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _resolve_dev_vm_name(explicit_name: str, env: dict[str, str]) -> str:
    if explicit_name.strip():
        return explicit_name.strip()
    dev_name = (env.get('SOPHON_DEV_INFRAVM_NAME') or '').strip()
    if dev_name:
        return dev_name
    legacy_name = (env.get('SOPHON_INFRAVM_NAME') or '').strip()
    if legacy_name and legacy_name not in ('sophon-infravm', 'sophon-backup'):
        return legacy_name
    return 'sophon-dev-infravm'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Destroy and re-provision a clean Sophon dev/drill VM (or replace prod with --prod-recreate).'
    )
    parser.add_argument('--prod-recreate', action='store_true',
                        help='Destroy and recreate the production InfraVM (SOPHON_INFRAVM_NAME) in one step')
    parser.add_argument('--destroy-only', action='store_true',
                        help='Stop and delete a Sophon-managed VM (default: SOPHON_INFRAVM_NAME) and exit')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Skip interactive confirmation when destroying/recreating a production VM')
    parser.add_argument('--vm-name', default='',
                        help='Override target VM name (default: SOPHON_DEV_INFRAVM_NAME for dev, SOPHON_INFRAVM_NAME for --prod-recreate)')
    parser.add_argument('--secrets-iso', type=Path, default=None,
                        help='Unattended secrets ISO (default: SOPHON_SECRETS_ISO or artifacts/secrets/sophon-secrets.iso)')
    parser.add_argument('--allow-production-name', action='store_true',
                        help='Allow targeting production VM name without --prod-recreate')
    parser.add_argument('--wait-https', action='store_true',
                        help='When workstation has L3 reachability to the VM, wait until restored HTTPS services pass SNI checks')
    parser.add_argument('--target-ip', default='', help='Override target IPv4 address when using --wait-https')
    parser.add_argument('--ca-file', default=os.environ.get('SOPHON_VERIFY_RESTORE_CA_FILE', ''),
                        help='Optional custom CA bundle file when using --wait-https')
    parser.add_argument('--timeout', type=int, default=int(os.environ.get('SOPHON_VERIFY_TIMEOUT', '1800')),
                        help='Maximum seconds to wait when --wait-https is enabled (default: %(default)s)')
    options = parser.parse_args(argv)

    hypervisor = (os.environ.get('SOPHON_HYPERVISOR') or 'proxmox').strip().lower()
    if hypervisor not in deployment.CHOICES['SOPHON_HYPERVISOR']:
        print('SOPHON_HYPERVISOR must be proxmox, vcenter or hyperv.', file=sys.stderr)
        return 2

    prod_vm_name = (os.environ.get('SOPHON_INFRAVM_NAME') or 'sophon-infravm').strip()
    backup_vm_name = (os.environ.get('SOPHON_BACKUP_VM_NAME') or 'sophon-backup').strip()
    if options.destroy_only:
        vm_name = (options.vm_name or os.environ.get('SOPHON_DESTROY_VM_NAME') or prod_vm_name).strip()
        protected_names = {'sophon-infravm', 'sophon-backup', prod_vm_name, backup_vm_name}
        env_allow = (os.environ.get('SOPHON_ALLOW_PRODUCTION_DESTROY') or '').strip().lower() in ('1', 'true', 'yes')
        if vm_name in protected_names and not (options.yes or options.allow_production_name or env_allow):
            if not sys.stdin.isatty():
                print(f"Pass --yes to destroy VM '{vm_name}' non-interactively.", file=sys.stderr)
                return 2
            reply = input(f"Destroy Sophon VM '{vm_name}' on {hypervisor}? [y/N]: ").strip().lower()
            if reply not in ('y', 'yes'):
                print('Cancelled; VM was not modified.')
                return 130
        return run_playbook(
            '3-destroy-vm.yml',
            {
                'SOPHON_DESTROY_VM_NAME': vm_name,
                'SOPHON_ALLOW_PRODUCTION_DESTROY': 'true',
            },
        )
    if options.prod_recreate:
        vm_name = (options.vm_name or prod_vm_name).strip()
        if vm_name == 'sophon-backup':
            print("Refusing to destroy backup appliance VM 'sophon-backup' via provision --recreate.", file=sys.stderr)
            return 2
        if not options.yes:
            if not sys.stdin.isatty():
                print(f"Pass --yes to destroy and recreate production VM '{vm_name}' non-interactively.", file=sys.stderr)
                return 2
            reply = input(f"Destroy and recreate production VM '{vm_name}' on {hypervisor}? [y/N]: ").strip().lower()
            if reply not in ('y', 'yes'):
                print('Cancelled; production VM was not modified.')
                return 130
        rc = run_playbook(
            '3-destroy-vm.yml',
            {
                'SOPHON_DESTROY_VM_NAME': vm_name,
                'SOPHON_ALLOW_PRODUCTION_DESTROY': 'true',
            },
        )
        if rc != 0:
            return rc
        prov_env: dict[str, str] = {'SOPHON_INFRAVM_NAME': vm_name}
        if options.secrets_iso is not None:
            prov_env['SOPHON_SECRETS_ISO'] = str(Path(os.path.abspath(options.secrets_iso)))
            prov_env['SOPHON_ATTACH_SECRETS_ISO'] = 'true'
        return run_playbook('2-provision.yml', prov_env)

    vm_name = _resolve_dev_vm_name(options.vm_name, os.environ)
    forbidden_names = {'sophon-infravm', 'sophon-backup', prod_vm_name}
    if vm_name in forbidden_names and not options.allow_production_name:
        print(
            f"Refusing to run dev/drill deployment against production VM '{vm_name}'. "
            'Set SOPHON_DEV_INFRAVM_NAME=sophon-dev-infravm (or run `provision --recreate` to replace prod).',
            file=sys.stderr,
        )
        return 2

    iso_path = options.secrets_iso
    if iso_path is None:
        env_iso = os.environ.get('SOPHON_SECRETS_ISO', '').strip()
        if env_iso:
            iso_path = Path(env_iso)
        elif DEFAULT_DEV_SECRETS_ISO.is_file():
            iso_path = DEFAULT_DEV_SECRETS_ISO
        else:
            iso_path = secrets_iso.SECRETS_ISO

    try:
        meta = inspect_seed_iso(iso_path)
    except ValueError as error:
        print(f'provision-dev preflight failed: {error}', file=sys.stderr)
        return 2

    needs_derive = (
        bool(os.environ.get('SOPHON_DEV_NETWORK_CIDR', '').strip())
        or (meta['target'] != 'dev' and (meta['has_dev_network'] or meta['sftp_username'] != 'sophon-restore'))
    )

    with contextlib.ExitStack() as stack:
        effective_iso = Path(os.path.abspath(iso_path))
        if needs_derive:
            effective_iso.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp_dir = Path(stack.enter_context(
                tempfile.TemporaryDirectory(prefix='.sophon-dev-iso-', dir=effective_iso.parent)
            ))
            derived_path = tmp_dir / 'sophon-dev-secrets.iso'
            secrets_iso.derive_dev_secrets_iso(effective_iso, os.environ, derived_path)
            effective_iso = derived_path
            meta = inspect_seed_iso(effective_iso)

        wait_https = options.wait_https or os.environ.get('SOPHON_VERIFY_HTTPS', '').strip().lower() in ('1', 'true', 'yes')
        target_ip = (options.target_ip or os.environ.get('SOPHON_VERIFY_HTTPS_IP') or meta['ip']).strip()
        if wait_https and not target_ip:
            print('--wait-https requires a static IP (SOPHON_DEV_NETWORK_CIDR) or --target-ip / SOPHON_VERIFY_HTTPS_IP.', file=sys.stderr)
            return 2

        print(f'== Sophon Dev/Drill InfraVM ({hypervisor}): recreating {vm_name} ({meta["ip"] or "dhcp"}) ==')
        rc = run_playbook(
            '3-destroy-vm.yml',
            {
                'SOPHON_DESTROY_VM_NAME': vm_name,
                'SOPHON_ALLOW_PRODUCTION_DESTROY': 'true' if options.allow_production_name else 'false',
            },
        )
        if rc != 0:
            return rc

        rc = run_playbook(
            '2-provision.yml',
            {
                'SOPHON_INFRAVM_NAME': vm_name,
                'SOPHON_SECRETS_ISO': str(effective_iso),
                'SOPHON_ATTACH_SECRETS_ISO': 'true',
            },
        )
        if rc != 0:
            return rc

    if wait_https:
        print(f'== Waiting for unattended CoreOS install, standby restore, and HTTPS readiness at {target_ip} ({meta["domain"]}) ==')
        try:
            wait_for_https_ready(target_ip, meta['domain'], ca_file=options.ca_file, timeout_sec=options.timeout)
        except TimeoutError as error:
            print(f'Restore drill verification failed: {error}', file=sys.stderr)
            return 1
        print(f'== Restore drill PASSED: {vm_name} ({target_ip}) restored latest snapshot in safe standby mode ==')
    else:
        print(
            f"== Dev/drill VM '{vm_name}' ({meta['ip'] or 'dhcp'}) provisioned on {hypervisor} ==\n"
            'Inside the guest, sophon-autodeploy will install CoreOS, restore the latest snapshot in safe standby mode\n'
            '(Cloudflare tunnel postponed, Kopia read-only, scheduled backups paused), and verify all services locally.\n'
            'To promote this VM to production (switching from dev IP to prod IP), SSH in as core and run: sophon-activate'
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
