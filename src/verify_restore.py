"""Hypervisor-agnostic automated Sophon restore drill (`verify-restore`).

Destroys any prior managed drill VM (`SOPHON_INFRAVM_NAME`, default `sophon-dev-infravm`) across
Proxmox, VMware vCenter, or Microsoft Hyper-V, then provisions a fresh VM with the unattended
`sophon-secrets.iso` (or `sophon-dev-secrets.iso`) attached.

Inside the guest, `sophon-autodeploy` automatically:
1. Installs CoreOS to the blank virtual disk and reboots.
2. Reads `sophon-seed.json` in RAM (`tmpfs`), sets the `core` password hash and network, and ejects the ISO.
3. Detects snapshots on the SFTP backup repository and deploys stacks in Safe Standby Mode
   (Cloudflare tunnel postponed, Kopia connected `--readonly`, scheduled backups paused).
4. Runs `sophon-restore --snapshot latest --yes` and verifies all 7 containers + local HTTPS endpoints.

No `serial0` socket or workstation-to-VM `:443` network route is required. When the workstation IS
on the same network and `--wait-https` (or `SOPHON_VERIFY_HTTPS=true`) is passed, `verify-restore`
also polls the restored HTTPS endpoints over SNI until the drill VM reports ready.
"""

import argparse
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
    env = doc.get('env') or {}
    cidr = net.get('cidr', '')
    ip = cidr.split('/')[0] if '/' in cidr else env.get('SOPHON_INFRAVM_IP', '')
    return {
        'domain': env.get('SOPHON_DOMAIN', ''),
        'network_mode': net.get('mode', 'dhcp'),
        'ip': ip,
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Destroy and re-provision a clean Sophon restore-drill VM across Proxmox, vCenter, or Hyper-V.'
    )
    parser.add_argument('--vm-name', default='', help='Target drill VM name (default: SOPHON_INFRAVM_NAME or sophon-dev-infravm)')
    parser.add_argument('--secrets-iso', type=Path, default=None,
                        help='Unattended secrets ISO for the drill VM (default: SOPHON_SECRETS_ISO, sophon-dev-secrets.iso, or sophon-secrets.iso)')
    parser.add_argument('--allow-production-name', action='store_true',
                        help='Allow destroying and recreating production VM name `sophon-infravm` (for deliberate DR cutover)')
    parser.add_argument('--wait-https', action='store_true',
                        help='When workstation has L3 reachability to the drill VM, wait until restored HTTPS services pass SNI checks')
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

    vm_name = (options.vm_name or os.environ.get('SOPHON_INFRAVM_NAME') or 'sophon-dev-infravm').strip()
    if vm_name in ('sophon-infravm', 'sophon-backup') and not options.allow_production_name:
        print(
            f"Refusing to run destructive restore drill against '{vm_name}'. "
            'Set SOPHON_INFRAVM_NAME=sophon-dev-infravm (or pass --allow-production-name for intentional DR replacement).',
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
        print(f'verify-restore preflight failed: {error}', file=sys.stderr)
        return 2

    wait_https = options.wait_https or os.environ.get('SOPHON_VERIFY_HTTPS', '').strip().lower() in ('1', 'true', 'yes')
    target_ip = (options.target_ip or os.environ.get('SOPHON_VERIFY_HTTPS_IP') or meta['ip']).strip()
    if wait_https and not target_ip:
        print('--wait-https requires a static IP in sophon-seed.json or --target-ip / SOPHON_VERIFY_HTTPS_IP.', file=sys.stderr)
        return 2

    print(f'== Sophon Restore Drill ({hypervisor}): rebuilding {vm_name} with {iso_path.name} ==')
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
            'SOPHON_SECRETS_ISO': str(Path(os.path.abspath(iso_path))),
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
            f"== Drill VM '{vm_name}' provisioned on {hypervisor} with {iso_path.name} ==\n"
            'Inside the guest, sophon-autodeploy will install CoreOS, restore the latest snapshot in safe standby mode\n'
            '(Cloudflare tunnel postponed, Kopia read-only, scheduled backups paused), and verify all services locally.\n'
            'Check the hypervisor console login banner (/etc/issue) or /var/lib/sophon/autodeploy-status.json for completion.'
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
