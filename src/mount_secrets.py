"""Upload and hot-mount `sophon-secrets.iso` onto a running Sophon InfraVM (`sophon-mount-secrets.sh`).

Verifies that the secrets ISO contains a valid `acme.json` (so an unusable disk is never mounted),
attaches it to the target Sophon-managed VM across Proxmox, VMware vCenter, or Microsoft Hyper-V,
prunes superseded `sophon-secrets-*.iso` copies from hypervisor storage, and removes the local
workstation `artifacts/secrets/sophon-secrets.iso` copy unless `--keep-local` is passed.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import certificates
import deployment

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SECRETS_ISO = ROOT / 'artifacts/secrets/sophon-secrets.iso'


def verify_secrets_iso(iso_path: Path, expected_domain: str = '') -> tuple[str, str]:
    """Ensure `iso_path` exists and carries a valid, non-expired `acme.json`; return `(domain, expires_iso)`."""
    iso_path = Path(os.path.abspath(iso_path))
    if not iso_path.is_file() or iso_path.is_symlink():
        raise ValueError(
            f'Missing secrets ISO: {iso_path}\n'
            'Generate or renew it first with: sh sophon-build-isos.sh'
        )
    if not shutil.which('xorriso'):
        raise ValueError('xorriso is missing; run this through the sophon-tools container.')
    with tempfile.TemporaryDirectory(prefix='sophon-verify-secrets-') as work:
        acme_out = Path(work) / 'acme.json'
        seed_out = Path(work) / 'sophon-seed.json'
        res = subprocess.run(
            ['xorriso', '-osirrox', 'on', '-indev', str(iso_path), '-extract', '/acme.json', str(acme_out)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0 or not acme_out.is_file():
            raise ValueError(
                f'{iso_path} does not contain /acme.json.\n'
                'Ensure SOPHON_CF_TOKEN is configured or run: sh sophon-build-isos.sh --renew-certs'
            )
        domain = expected_domain.strip().lower().rstrip('.')
        if not domain:
            subprocess.run(
                ['xorriso', '-osirrox', 'on', '-indev', str(iso_path), '-extract', '/sophon-seed.json', str(seed_out)],
                capture_output=True,
                text=True,
            )
            if seed_out.is_file():
                import json
                try:
                    doc = json.loads(seed_out.read_text(encoding='utf-8'))
                    domain = ((doc.get('env') or {}).get('SOPHON_DOMAIN') or '').strip().lower().rstrip('.')
                except Exception:
                    domain = ''
        raw = acme_out.read_bytes()
    if domain:
        expiries = certificates.validate_data(raw, domain)
    else:
        import json
        data = json.loads(raw)
        found_domain = ''
        for store in data.values() if isinstance(data, dict) else ():
            if isinstance(store, dict):
                for entry in store.get('Certificates') or ():
                    main = ((entry.get('domain') or {}).get('main') or '').strip().lower().rstrip('.')
                    if main:
                        found_domain = main
                        break
            if found_domain:
                break
        if not found_domain:
            raise ValueError('The acme.json inside sophon-secrets.iso contains no certificate entry.')
        domain = found_domain
        expiries = certificates.validate_data(raw, domain)
    return domain, expiries[domain]


def run_mount_playbook(vm_name: str, iso_path: Path) -> int:
    extra_env = {
        'SOPHON_MOUNT_SECRETS_VM_NAME': vm_name,
        'SOPHON_SECRETS_ISO': str(Path(os.path.abspath(iso_path))),
    }
    saved = {k: os.environ.get(k) for k in extra_env}
    try:
        os.environ.update(extra_env)
        return deployment.main(['--step', 'provision', '--', '3-mount-secrets.yml'])
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Upload and hot-mount sophon-secrets.iso onto a Sophon InfraVM, then remove the local ISO copy.'
    )
    parser.add_argument('--vm-name', default='', help='Target Sophon VM name (default: SOPHON_INFRAVM_NAME)')
    parser.add_argument('--secrets-iso', type=Path, default=None,
                        help='Path to sophon-secrets.iso (default: artifacts/secrets/sophon-secrets.iso)')
    parser.add_argument('--keep-local', action='store_true',
                        help='Keep the local workstation sophon-secrets.iso copy after mounting')
    options = parser.parse_args(argv)

    hypervisor = (os.environ.get('SOPHON_HYPERVISOR') or 'proxmox').strip().lower()
    if hypervisor not in deployment.CHOICES['SOPHON_HYPERVISOR']:
        print('SOPHON_HYPERVISOR must be proxmox, vcenter or hyperv.', file=sys.stderr)
        return 2

    vm_name = (options.vm_name or os.environ.get('SOPHON_INFRAVM_NAME') or 'sophon-infravm').strip()
    if vm_name == 'sophon-backup':
        print("Refusing to mount InfraVM certificates onto backup VM 'sophon-backup'.", file=sys.stderr)
        return 2

    iso_path = options.secrets_iso
    if iso_path is None:
        env_iso = (os.environ.get('SOPHON_SECRETS_ISO') or '').strip()
        iso_path = Path(env_iso) if env_iso else DEFAULT_SECRETS_ISO
    iso_path = Path(os.path.abspath(iso_path))

    try:
        domain, expires_at = verify_secrets_iso(iso_path, os.environ.get('SOPHON_DOMAIN', ''))
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    print(f"Mounting certificate disk for {domain} (valid until {expires_at}) onto '{vm_name}' ({hypervisor})...")
    rc = run_mount_playbook(vm_name, iso_path)
    if rc != 0:
        return rc

    if not options.keep_local:
        iso_path.unlink(missing_ok=True)
        print(f'Removed local workstation copy: {iso_path}')
    print(f"Certificate disk attached to '{vm_name}'.")
    print(f"Next: open https://{domain} and click 'Update Certificates' (or run `sophon-refresh-certs` on the VM console).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
