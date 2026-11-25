"""Build optional unattended secrets/seed ISOs for InfraVM and the Sophon Backup Server.

Produces mode-0600 ISOs under `artifacts/secrets/`:
- `sophon-secrets.iso` (volume label `SOPHON_CERTS`): carries optional `acme.json` (air-gapped TLS store)
  and optional `sophon-seed.json` (hashed `core` console password, static/DHCP network settings, and
  validated `SOPHON_*` service + pre-paired SFTP settings).
  A single `sophon-secrets.iso` works for both Day-1 builds and disaster-recovery / dev restores:
  when `/repository` is empty it initializes Day 1; when snapshots already exist it restores the latest
  snapshot into safe standby mode (Cloudflare tunnel postponed, Kopia read-only, scheduled backups paused)
  until an operator explicitly runs `sophon-activate`.
- `sophon-sftp-secrets.iso` (volume label `SOPHON_BACKUP_CERTS`): optional unattended seed ISO for the
  Alpine SFTP Backup Server carrying hashed `admin` password, static/DHCP network config, deterministic
  `ssh_host_ed25519_key`, and pre-authorized `sophon-backup` / `sophon-restore` public keys.
"""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import paramiko

import certificates
import cli
import console
import deployment

ROOT = Path(__file__).resolve().parent.parent
SECRETS_ISO = ROOT / 'artifacts/secrets/sophon-secrets.iso'
BACKUP_SECRETS_ISO = ROOT / 'artifacts/secrets/sophon-sftp-secrets.iso'
BACKUP_SECRETS_LABEL = 'SOPHON_BACKUP_CERTS'

_CRYPT64 = './0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

_SHA512_CRYPT_PERM = (
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4),
    (47, 5, 26), (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51),
    (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
    (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
    (62, 20, 41),
)


def _b64_encode_triplet(b2: int, b1: int, b0: int, count: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(count):
        out.append(_CRYPT64[w & 0x3F])
        w >>= 6
    return ''.join(out)


def sha512_crypt(password: str, salt: str | None = None) -> str:
    """Standard Ulrich Drepper SHA-512 `$6$` crypt (5000 rounds) compatible with glibc and musl `chpasswd -e`."""
    if not password:
        raise ValueError('Password must not be empty.')
    if salt is None:
        salt = ''.join(secrets.choice(_CRYPT64) for _ in range(16))
    salt = salt[:16]
    if not salt or any(ch not in _CRYPT64 for ch in salt):
        raise ValueError('Salt must use [./0-9A-Za-z] characters.')

    pw = password.encode('utf-8')
    st = salt.encode('ascii')
    pw_len = len(pw)

    alt = hashlib.sha512(pw + st + pw).digest()
    ctx = hashlib.sha512(pw + st)
    full, rem = divmod(pw_len, 64)
    for _ in range(full):
        ctx.update(alt)
    ctx.update(alt[:rem])

    n = pw_len
    while n > 0:
        ctx.update(alt if (n & 1) else pw)
        n >>= 1
    digest_a = ctx.digest()

    dp_ctx = hashlib.sha512()
    for _ in range(pw_len):
        dp_ctx.update(pw)
    dp = dp_ctx.digest()
    full_p, rem_p = divmod(pw_len, 64)
    p_bytes = (dp * full_p) + dp[:rem_p]

    ds_ctx = hashlib.sha512()
    for _ in range(16 + digest_a[0]):
        ds_ctx.update(st)
    ds = ds_ctx.digest()
    full_s, rem_s = divmod(len(st), 64)
    s_bytes = (ds * full_s) + ds[:rem_s]

    c = digest_a
    for r in range(5000):
        c_ctx = hashlib.sha512()
        c_ctx.update(p_bytes if (r & 1) else c)
        if r % 3:
            c_ctx.update(s_bytes)
        if r % 7:
            c_ctx.update(p_bytes)
        c_ctx.update(c if (r & 1) else p_bytes)
        c = c_ctx.digest()

    encoded = ''.join(_b64_encode_triplet(c[i], c[j], c[k], 4) for i, j, k in _SHA512_CRYPT_PERM)
    encoded += _b64_encode_triplet(0, 0, c[63], 2)
    return f'$6${salt}${encoded}'


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(prefix='.secret-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _generate_ed25519_pair(comment: str) -> tuple[bytes, str]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    pub_openssh = priv.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode('ascii')
    return priv_pem, f'{pub_openssh} {comment}\n'


def ensure_paired_sftp_keys(state_dir: Path, sftp_host: str, sftp_port: int = 22) -> dict:
    """Create or reuse deterministic paired SSH host + client keys under `<state_dir>/sftp/<host>-<port>`."""
    safe_host = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in sftp_host.strip())
    pair_dir = state_dir.expanduser().resolve() / 'sftp' / f'{safe_host}-{int(sftp_port)}'
    if pair_dir.is_symlink():
        raise ValueError(f'Refusing symlinked SFTP key directory: {pair_dir}')
    pair_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pair_dir.chmod(0o700)

    specs = (
        ('ssh_host_ed25519_key', 'root@sophon-backup'),
        ('client_backup_ed25519', 'sophon-backup'),
        ('client_restore_ed25519', 'sophon-restore'),
    )
    for stem, comment in specs:
        priv_path = pair_dir / stem
        pub_path = pair_dir / f'{stem}.pub'
        if not priv_path.exists() or not pub_path.exists():
            priv_pem, pub_line = _generate_ed25519_pair(comment)
            _write_private(priv_path, priv_pem)
            _write_private(pub_path, pub_line.encode('ascii'))

    host_pub_parts = (pair_dir / 'ssh_host_ed25519_key.pub').read_text(encoding='ascii').strip().split()
    key_type, key_b64 = host_pub_parts[0], host_pub_parts[1]
    dest = sftp_host.strip() if int(sftp_port) == 22 else f'[{sftp_host.strip()}]:{int(sftp_port)}'
    known_hosts = f'{dest} {key_type} {key_b64}\n'.encode('ascii')

    backup_pub_parts = (pair_dir / 'client_backup_ed25519.pub').read_text(encoding='ascii').strip().split()
    restore_pub_parts = (pair_dir / 'client_restore_ed25519.pub').read_text(encoding='ascii').strip().split()

    return {
        'host_private_key': (pair_dir / 'ssh_host_ed25519_key').read_bytes(),
        'host_public_key': (pair_dir / 'ssh_host_ed25519_key.pub').read_bytes(),
        'client_backup_private_key': (pair_dir / 'client_backup_ed25519').read_bytes(),
        'client_restore_private_key': (pair_dir / 'client_restore_ed25519').read_bytes(),
        'known_hosts': known_hosts,
        'authorized_keys_backup': f'restrict {backup_pub_parts[0]} {backup_pub_parts[1]} sophon-backup\n'.encode('ascii'),
        'authorized_keys_restore': f'restrict {restore_pub_parts[0]} {restore_pub_parts[1]} sophon-restore\n'.encode('ascii'),
    }


def validate_ipv4_network(cidr: str, gateway: str, dns: str) -> dict:
    """Validate static IPv4 CIDR, gateway, and upstream DNS."""
    iface = ipaddress.IPv4Interface(cidr.strip())
    prefix = iface.network.prefixlen
    if not 1 <= prefix <= 30:
        raise ValueError('IPv4 prefix length must be between 1 and 30.')
    ip = iface.ip
    gw = ipaddress.IPv4Address(gateway.strip())
    resolver = ipaddress.IPv4Address(dns.strip())
    for addr, label in ((ip, 'IPv4 address'), (gw, 'Gateway'), (resolver, 'DNS server')):
        if addr.is_loopback or addr.is_multicast or addr.is_unspecified:
            raise ValueError(f'{label} must be a unicast non-loopback IPv4 address.')
    if gw not in iface.network or gw == ip:
        raise ValueError('Gateway must be distinct and within the same IPv4 subnet.')
    if ip in (iface.network.network_address, iface.network.broadcast_address):
        raise ValueError('Host IPv4 address must not be the network or broadcast address.')
    if gw in (iface.network.network_address, iface.network.broadcast_address):
        raise ValueError('Gateway IPv4 address must not be the network or broadcast address.')
    return {
        'mode': 'static',
        'ip': str(ip),
        'prefix': prefix,
        'cidr': f'{ip}/{prefix}',
        'gateway': str(gw),
        'dns': str(resolver),
    }


def resolve_dev_ipv4_network(
    dev_cidr_or_ip: str,
    prod_network: dict,
    dev_gateway: str = '',
    dev_dns: str = '',
) -> dict:
    """Resolve and validate the dev/drill IPv4 network against `prod_network`."""
    raw = dev_cidr_or_ip.strip()
    if not raw:
        raise ValueError('Dev IPv4 CIDR is empty.')
    if '/' not in raw:
        prod_cidr = prod_network.get('cidr', '')
        if '/' not in prod_cidr:
            raise ValueError('SOPHON_DEV_NETWORK_CIDR must include a prefix length (for example 10.0.60.107/24).')
        raw = f"{raw}/{prod_cidr.split('/', 1)[1]}"
    gw = dev_gateway.strip() or prod_network.get('gateway', '').strip()
    dns = dev_dns.strip() or prod_network.get('dns', '1.1.1.1').strip()
    if not gw:
        raise ValueError('Dev static network requires SOPHON_DEV_NETWORK_GATEWAY (or SOPHON_NETWORK_GATEWAY).')
    validated = validate_ipv4_network(raw, gw, dns)
    prod_ip = prod_network.get('ip') or (prod_network.get('cidr', '').split('/')[0] if '/' in prod_network.get('cidr', '') else '')
    if prod_ip and validated['ip'] == prod_ip:
        raise ValueError(f"Dev IPv4 address ({validated['ip']}) must differ from production IPv4 address ({prod_ip}).")
    return {
        'mode': 'static',
        'cidr': validated['cidr'],
        'gateway': validated['gateway'],
        'dns': validated['dns'],
    }


def write_private_iso(files: dict[str, tuple[bytes, int]], label: str, output: Path) -> Path:
    if not shutil.which('xorriso'):
        raise ValueError('xorriso is missing; run this through the sophon-tools container.')
    output = Path(os.path.abspath(output))
    if output.is_symlink():
        raise ValueError(f'Secret ISO path must not be a symlink: {output}')
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='sophon-secrets-iso-') as work:
        stage = Path(work) / 'contents'
        stage.mkdir(mode=0o700)
        for rel, (content, mode) in sorted(files.items()):
            if '..' in Path(rel).parts or rel.startswith('/'):
                raise ValueError(f'Unsafe relative path in secret ISO: {rel!r}')
            dest = stage / rel
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest.write_bytes(content)
            dest.chmod(mode)
        image = Path(work) / 'secrets.iso'
        subprocess.run(
            ['xorriso', '-as', 'mkisofs', '-R', '-J', '-iso-level', '3', '-V', label, '-o', str(image), str(stage)],
            check=True,
            capture_output=True,
        )
        fd, partial = tempfile.mkstemp(prefix='.sophon-secrets-', suffix='.iso', dir=output.parent)
        try:
            with os.fdopen(fd, 'wb') as dst, image.open('rb') as src:
                os.fchmod(dst.fileno(), 0o600)
                shutil.copyfileobj(src, dst)
            os.replace(partial, output)
        finally:
            Path(partial).unlink(missing_ok=True)
    return output


def extract_acme_from_iso(iso_path: Path) -> bytes | None:
    """Read `/acme.json` out of an existing `SOPHON_CERTS` ISO without mounting (via xorriso)."""
    if not iso_path.is_file() or iso_path.is_symlink() or not shutil.which('xorriso'):
        return None
    with tempfile.TemporaryDirectory(prefix='sophon-extract-acme-') as work:
        target = Path(work) / 'acme.json'
        res = subprocess.run(
            ['xorriso', '-osirrox', 'on', '-indev', str(iso_path), '-extract', '/acme.json', str(target)],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            return target.read_bytes()
    return None


def find_existing_acme_bytes(
    domain: str,
    state_dir: Path,
    explicit_acme: str = '',
    *,
    force_renew: bool = False,
) -> bytes | None:
    """Locate pre-issued `acme.json` from explicit path, certificate state dir, or existing secret ISO."""
    domain = certificates.normalize_domain(domain)
    if explicit_acme:
        p = Path(explicit_acme).expanduser()
        if not p.is_file() or p.is_symlink():
            raise ValueError(f'Certificate file not found: {p}')
        data = p.read_bytes()
        certificates.validate_data(data, domain)
        return certificates.minimal_store(data, domain)
    if force_renew:
        return None

    state_acme = state_dir.expanduser().resolve() / 'certificates' / domain / 'acme.json'
    if state_acme.is_file() and not state_acme.is_symlink() and certificates.ready_to_export(state_acme, domain):
        return certificates.minimal_store(state_acme.read_bytes(), domain)

    extracted = extract_acme_from_iso(SECRETS_ISO)
    if extracted and certificates.ready_to_export_data(extracted, domain):
        return certificates.minimal_store(extracted, domain)
    return None


def build_backup_secrets_iso(
    *,
    admin_password_hash: str,
    network: dict,
    sftp_material: dict,
    output: Path = BACKUP_SECRETS_ISO,
) -> Path:
    """Write `artifacts/secrets/sophon-sftp-secrets.iso` (label `SOPHON_BACKUP_CERTS`, mode `0600`)."""
    if not admin_password_hash.startswith('$6$'):
        raise ValueError('admin_password_hash must be a SHA-512 ($6$) password hash.')
    if network.get('mode') == 'dhcp':
        net_lines = [
            'MODE=dhcp',
            f"DNS={network.get('dns') or '1.1.1.1'}",
        ]
    else:
        validated = validate_ipv4_network(network['cidr'], network['gateway'], network['dns'])
        net_lines = [
            'MODE=static',
            f"IP={validated['ip']}",
            f"PREFIX={validated['prefix']}",
            f"GATEWAY={validated['gateway']}",
            f"DNS={validated['dns']}",
        ]
    files = {
        'admin.shadow': ((admin_password_hash.strip() + '\n').encode('ascii'), 0o400),
        'network.env': (('\n'.join(net_lines) + '\n').encode('ascii'), 0o400),
        'ssh_host_ed25519_key': (sftp_material['host_private_key'], 0o400),
        'ssh_host_ed25519_key.pub': (sftp_material['host_public_key'], 0o444),
        'authorized_keys_backup': (sftp_material['authorized_keys_backup'], 0o444),
        'authorized_keys_restore': (sftp_material['authorized_keys_restore'], 0o444),
    }
    return write_private_iso(files, BACKUP_SECRETS_LABEL, output)


def build_infravm_secrets_iso(
    *,
    core_password_hash: str,
    network: dict,
    configure_values: dict[str, str],
    dev_network: dict | None = None,
    sftp_keys: dict[str, str] | None = None,
    acme_bytes: bytes | None = None,
    output: Path = SECRETS_ISO,
) -> Path:
    """Validate configure settings and write `artifacts/secrets/sophon-secrets.iso` (label `SOPHON_CERTS`)."""
    if not core_password_hash.startswith('$6$'):
        raise ValueError('core_password_hash must be a SHA-512 ($6$) password hash.')

    values = deployment.new_control(configure_values, 'configure')['values']
    if network.get('mode') == 'dhcp':
        dns = (network.get('dns') or values.get('SOPHON_VNET_DNS') or '1.1.1.1').strip()
        ipaddress.IPv4Address(dns)
        net_payload = {'mode': 'dhcp', 'dns': dns}
        # Placeholder loopback-free IPv4 for schema validation when DHCP resolves actual IP at boot.
        values_for_validation = dict(values, SOPHON_INFRAVM_IP=values.get('SOPHON_INFRAVM_IP') or '10.0.0.2', SOPHON_VNET_DNS=dns)
        values['SOPHON_INFRAVM_IP'] = ''
        values['SOPHON_VNET_DNS'] = dns
    else:
        validated_net = validate_ipv4_network(network['cidr'], network['gateway'], network['dns'])
        net_payload = {
            'mode': 'static',
            'cidr': validated_net['cidr'],
            'gateway': validated_net['gateway'],
            'dns': validated_net['dns'],
        }
        values['SOPHON_INFRAVM_IP'] = validated_net['ip']
        values['SOPHON_VNET_DNS'] = validated_net['dns']
        values_for_validation = dict(values)

    dev_net_payload = None
    if dev_network:
        if dev_network.get('mode') == 'dhcp':
            dev_dns = (dev_network.get('dns') or net_payload['dns']).strip()
            ipaddress.IPv4Address(dev_dns)
            dev_net_payload = {'mode': 'dhcp', 'dns': dev_dns}
        else:
            dev_net_payload = resolve_dev_ipv4_network(
                dev_network['cidr'],
                net_payload,
                dev_network.get('gateway', ''),
                dev_network.get('dns', ''),
            )

    values_for_validation['SOPHON_ACME_FILE'] = ''
    checked, error = deployment.validate_settings(values_for_validation, {}, 'configure')
    if error:
        raise ValueError(f'Invalid InfraVM seed configuration: {error}')
    for required_material in ('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64'):
        if not checked.get(required_material, '').strip():
            raise ValueError(f'{required_material} is required in unattended secrets ISO.')

    domain = checked['SOPHON_DOMAIN']
    if acme_bytes is not None:
        certificates.validate_data(acme_bytes, domain)
        acme_bytes = certificates.minimal_store(acme_bytes, domain)
    elif not checked.get('SOPHON_CF_TOKEN', '').strip():
        raise ValueError(
            f'Air-gapped seed for {domain} has neither SOPHON_CF_TOKEN nor a pre-issued acme.json. '
            'Run `sh sophon-build-isos.sh certificates issue` first or pass --acme-file.'
        )

    clean_env = {k: v for k, v in checked.items() if k != 'SOPHON_ACME_FILE' and v != ''}
    for opt_key in (
        'SOPHON_CF_ACCESS_ENABLED',
        'SOPHON_CF_ACCESS_EMAILS',
        'SOPHON_KEYCLOAK_REGISTRATION_ALLOWED',
        'SOPHON_NEXUS_ANONYMOUS_ENABLED',
    ):
        opt_val = str(configure_values.get(opt_key, '')).strip()
        if opt_val:
            clean_env[opt_key] = opt_val
    if network.get('mode') == 'dhcp':
        clean_env.pop('SOPHON_INFRAVM_IP', None)

    seed_doc: dict = {
        'version': 1,
        'target': 'dev' if clean_env.get('SOPHON_KOPIA_SFTP_USERNAME') == 'sophon-restore' else 'prod',
        'core_password_hash': core_password_hash.strip(),
        'network': net_payload,
        'env': clean_env,
    }
    if dev_net_payload is not None:
        seed_doc['dev_network'] = dev_net_payload
    if sftp_keys:
        seed_doc['sftp'] = dict(sftp_keys)
    files: dict[str, tuple[bytes, int]] = {
        'sophon-seed.json': ((json.dumps(seed_doc, indent=2, sort_keys=True) + '\n').encode('utf-8'), 0o400),
    }
    if acme_bytes is not None:
        files['acme.json'] = (acme_bytes, 0o400)
    return write_private_iso(files, certificates.CERTIFICATE_LABEL, output)


def derive_dev_secrets_iso(source_iso: Path, env: dict[str, str], output_iso: Path) -> dict:
    """Build a temporary dev/drill seed ISO from `source_iso`, targeting the dev IP in safe standby mode."""
    source_iso = Path(os.path.abspath(source_iso))
    if not source_iso.is_file() or source_iso.is_symlink():
        raise ValueError(
            f'Missing unattended secrets ISO: {source_iso}\n'
            'Generate one first with: sh sophon-build-isos.sh'
        )
    if not shutil.which('xorriso'):
        raise ValueError('xorriso is missing; run this through the sophon-tools container.')
    with tempfile.TemporaryDirectory(prefix='sophon-derive-dev-') as work:
        seed_path = Path(work) / 'sophon-seed.json'
        res = subprocess.run(
            ['xorriso', '-osirrox', 'on', '-indev', str(source_iso), '-extract', '/sophon-seed.json', str(seed_path)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0 or not seed_path.is_file():
            raise ValueError(
                f'{source_iso} does not contain /sophon-seed.json (it appears to be a certificate-only ISO).\n'
                'Build an unattended seed ISO with: sh sophon-build-isos.sh'
            )
        seed_doc = json.loads(seed_path.read_text(encoding='utf-8'))
        acme_bytes = extract_acme_from_iso(source_iso)

    prod_net = dict(seed_doc.get('prod_network') or seed_doc.get('network') or {'mode': 'dhcp', 'dns': '1.1.1.1'})
    env_dev_cidr = (env.get('SOPHON_DEV_NETWORK_CIDR') or '').strip()
    env_dev_gw = (env.get('SOPHON_DEV_NETWORK_GATEWAY') or '').strip()
    env_dev_dns = (env.get('SOPHON_DEV_VNET_DNS') or '').strip()

    if prod_net.get('mode') == 'static':
        if env_dev_cidr:
            dev_net = resolve_dev_ipv4_network(env_dev_cidr, prod_net, env_dev_gw, env_dev_dns)
        elif seed_doc.get('dev_network', {}).get('mode') == 'static':
            saved_dev = seed_doc['dev_network']
            dev_net = resolve_dev_ipv4_network(
                saved_dev['cidr'],
                prod_net,
                env_dev_gw or saved_dev.get('gateway', ''),
                env_dev_dns or saved_dev.get('dns', ''),
            )
        elif seed_doc.get('target') == 'dev':
            dev_net = dict(prod_net)
        else:
            prod_ip = prod_net.get('cidr', '').split('/')[0]
            raise ValueError(
                f'SOPHON_DEV_NETWORK_CIDR is required for provision-dev so the dev VM does not collide '
                f'with production IP {prod_ip}. Set SOPHON_DEV_NETWORK_CIDR in .env (or regenerate `secrets`).'
            )
    else:
        dev_dns = env_dev_dns or (seed_doc.get('dev_network') or {}).get('dns') or prod_net.get('dns', '1.1.1.1')
        ipaddress.IPv4Address(dev_dns)
        dev_net = {'mode': 'dhcp', 'dns': dev_dns}

    seed_env = dict(seed_doc.get('env') or {})
    if dev_net['mode'] == 'static':
        dev_ip = dev_net['cidr'].split('/')[0]
        seed_env['SOPHON_INFRAVM_IP'] = dev_ip
    else:
        dev_ip = ''
        seed_env.pop('SOPHON_INFRAVM_IP', None)
    seed_env['SOPHON_VNET_DNS'] = dev_net['dns']

    sftp_keys = dict(seed_doc.get('sftp') or {})
    if sftp_keys.get('restore_private_key_b64'):
        seed_env['SOPHON_KOPIA_SFTP_USERNAME'] = 'sophon-restore'
        seed_env['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64'] = sftp_keys['restore_private_key_b64']

    derived_doc = dict(seed_doc)
    derived_doc['target'] = 'dev'
    derived_doc['prod_network'] = prod_net
    derived_doc['network'] = dev_net
    derived_doc['env'] = seed_env

    files: dict[str, tuple[bytes, int]] = {
        'sophon-seed.json': ((json.dumps(derived_doc, indent=2, sort_keys=True) + '\n').encode('utf-8'), 0o400),
    }
    if acme_bytes is not None:
        files['acme.json'] = (acme_bytes, 0o400)
    write_private_iso(files, certificates.CERTIFICATE_LABEL, output_iso)
    prod_ip = prod_net.get('cidr', '').split('/')[0] if prod_net.get('mode') == 'static' else ''
    return {
        'domain': seed_env.get('SOPHON_DOMAIN', ''),
        'network_mode': dev_net['mode'],
        'ip': dev_ip,
        'prod_ip': prod_ip,
    }


def _apply_shared_service_password(env: dict[str, str], shared_password: str) -> None:
    fields = deployment.step_fields('configure')
    for name, field in fields.items():
        if field[2] == 'password' and name not in console.NOT_SHARED and not env.get(name):
            env[name] = shared_password


def _validate_disk_gb(name: str, raw: str, minimum: int, maximum: int = 1048576) -> str:
    text = raw.strip()
    if not text.isdigit():
        raise ValueError(f'{name} must be a whole number between {minimum} and {maximum} GiB.')
    val = int(text)
    if not minimum <= val <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum} GiB.')
    return str(val)


def _prompt_disk_gb(name: str, label: str, current: str, minimum: int) -> str:
    while True:
        candidate = console.prompt_field(name, label, 'number', current, True)
        try:
            return _validate_disk_gb(name, candidate, minimum)
        except ValueError as err:
            print(err)


def _prompt_and_persist_dotenv(updates: dict[str, str], dotenv_path: Path) -> None:
    if not updates or not sys.stdin.isatty():
        return
    try:
        resolved_default = (ROOT / '.env').resolve()
        dotenv_label = '.env' if dotenv_path.resolve() == resolved_default else str(dotenv_path)
    except OSError:
        dotenv_label = str(dotenv_path)
    if console.ask_yes_no(f'Save configuration decisions to {dotenv_label} (mode 0600) for future runs?', default=True):
        template = (ROOT / '.env.example') if (ROOT / '.env.example').is_file() else None
        cli.save_dotenv(updates, path=dotenv_path, template_path=template)
        print(f'Saved configuration to {dotenv_label} (mode 0600).')


def _apply_deferred_dotenv(pending_file: Path) -> int:
    if not pending_file.is_file() or pending_file.is_symlink():
        pending_file.unlink(missing_ok=True)
        return 0
    try:
        payload = json.loads(pending_file.read_text(encoding='utf-8'))
    finally:
        pending_file.unlink(missing_ok=True)
    updates = payload.get('updates') or {}
    dotenv_path = Path(payload.get('dotenv_path') or (ROOT / '.env'))
    try:
        _prompt_and_persist_dotenv(updates, dotenv_path)
        return 0
    except (OSError, ValueError) as error:
        print(f'Saving .env failed: {error}', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nSkipped saving .env.', file=sys.stderr)
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Generate mode-0600 unattended secrets ISOs (`sophon-secrets.iso` and optional `sophon-sftp-secrets.iso`).'
    )
    parser.add_argument('--run', action='store_true', help='Build non-interactively from exported SOPHON_* variables')
    parser.add_argument('--with-backup', action='store_true',
                        help='Also build artifacts/secrets/sophon-sftp-secrets.iso and pre-pair SSH host/client keys')
    parser.add_argument('--readonly-sftp', action='store_true',
                        help='Use the confined read-only `sophon-restore` SFTP key in `sophon-secrets.iso`')
    parser.add_argument('--renew-certs', action='store_true',
                        help=f'Force issuing a fresh 90-day wildcard certificate even when >{certificates.MIN_REMAINING_DAYS} days remain')
    parser.add_argument('--acme-file', default=os.environ.get('SOPHON_ACME_FILE', ''),
                        help='Optional path to pre-issued Traefik acme.json for air-gapped sites')
    parser.add_argument('--output-iso', type=Path, default=SECRETS_ISO,
                        help='Output path for InfraVM secrets ISO (default: %(default)s)')
    parser.add_argument('--backup-output-iso', type=Path, default=BACKUP_SECRETS_ISO,
                        help='Output path for Backup Server secrets ISO (default: %(default)s)')
    parser.add_argument('--derive-dev-iso', type=Path, default=None,
                        help='Derive a dev/drill secrets ISO at this path from --output-iso and exit')
    parser.add_argument('--dotenv-path', type=Path, default=ROOT / '.env',
                        help=argparse.SUPPRESS)
    parser.add_argument('--defer-dotenv', type=Path, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument('--apply-dotenv', type=Path, default=None,
                        help=argparse.SUPPRESS)
    default_state = Path(os.environ.get('SOPHON_STATE_DIR') or Path.home() / '.local/state/sophon')
    parser.add_argument('--state-dir', type=Path, default=default_state)
    options = parser.parse_args(argv)

    if options.apply_dotenv is not None:
        return _apply_deferred_dotenv(options.apply_dotenv)

    env = dict(os.environ)
    if options.derive_dev_iso is not None:
        try:
            derive_dev_secrets_iso(options.output_iso, env, options.derive_dev_iso)
            return 0
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            print(f'Dev secrets ISO generation failed: {error}', file=sys.stderr)
            return 1
    explicit_with_backup = options.with_backup or env.get('SOPHON_WITH_BACKUP_SECRETS', '').strip().lower() in ('1', 'true', 'yes')
    explicit_without_backup = env.get('SOPHON_WITH_BACKUP_SECRETS', '').strip().lower() in ('0', 'false', 'no')
    has_backup_cidr = bool(env.get('SOPHON_BACKUP_NETWORK_CIDR', '').strip())
    with_backup = False if (explicit_without_backup and not options.with_backup) else (explicit_with_backup or has_backup_cidr)
    readonly_sftp = options.readonly_sftp or env.get('SOPHON_SECRETS_READONLY_SFTP', '').strip().lower() in ('1', 'true', 'yes')
    prompted = False
    used_shared_service_password = False

    def require_tty(missing_name: str) -> None:
        if not sys.stdin.isatty():
            parser.error(f'{missing_name} is unset; set it in .env or run from an interactive terminal.')

    def need_field(name: str, label: str, kind: str, current: str, required: bool = True, fallback: str = '') -> str:
        nonlocal prompted
        if current.strip():
            return current.strip()
        if not required and not sys.stdin.isatty():
            return fallback
        require_tty(name)
        prompted = True
        return console.prompt_field(name, label, kind, fallback, required)

    try:
        shared_pw = env.get('SOPHON_SHARED_PASSWORD', '')
        if shared_pw:
            used_shared_service_password = True
            _apply_shared_service_password(env, shared_pw)

        core_hash = env.get('SOPHON_CORE_PASSWORD_HASH', '').strip()
        core_plain = env.get('SOPHON_CORE_PASSWORD', '') or shared_pw
        raw_backup_admin_plain = env.get('SOPHON_BACKUP_ADMIN_PASSWORD', '')
        backup_admin_hash = env.get('SOPHON_BACKUP_ADMIN_PASSWORD_HASH', '').strip()
        backup_admin_plain = raw_backup_admin_plain or core_plain

        net_mode = (env.get('SOPHON_NETWORK_MODE') or ('static' if env.get('SOPHON_NETWORK_CIDR') else '')).strip().lower()
        raw_net_cidr = env.get('SOPHON_NETWORK_CIDR', '').strip()
        net_cidr = raw_net_cidr
        net_gw = env.get('SOPHON_NETWORK_GATEWAY', '').strip()
        raw_net_dns = env.get('SOPHON_VNET_DNS', '').strip()
        net_dns = raw_net_dns or '1.1.1.1'
        dev_cidr = env.get('SOPHON_DEV_NETWORK_CIDR', '').strip()
        dev_gw = env.get('SOPHON_DEV_NETWORK_GATEWAY', '').strip()
        dev_dns = env.get('SOPHON_DEV_VNET_DNS', '').strip()
        raw_infravm_disk_gb = env.get('SOPHON_INFRAVM_DISK_GB', '').strip()
        infravm_disk_gb = raw_infravm_disk_gb or '1024'
        raw_bk_cidr = env.get('SOPHON_BACKUP_NETWORK_CIDR', '').strip()
        raw_backup_disk_gb = env.get('SOPHON_BACKUP_VM_DISK_GB', '').strip()
        backup_disk_gb = raw_backup_disk_gb or '1536'

        if not options.run:
            if not core_hash and not core_plain:
                require_tty('SOPHON_SHARED_PASSWORD')
                prompted = True
                print('== Sophon Unattended Secrets ISO Builder ==')
                while True:
                    pw = console.read_secret('Console password for InfraVM user `core` (12+ characters): ')
                    if len(pw) < 12:
                        print('Use at least 12 characters.')
                    elif pw != console.read_secret('Confirm password: '):
                        print('Passwords do not match.')
                    else:
                        core_plain = pw
                        break
                if not shared_pw and console.ask_yes_no('Use this same password for local Sophon service accounts?', default=True):
                    used_shared_service_password = True
                    _apply_shared_service_password(env, core_plain)
            if not net_mode:
                require_tty('SOPHON_NETWORK_CIDR')
                prompted = True
                use_static = console.ask_yes_no('Configure a static IPv4 address for InfraVM (recommended)?', default=True)
                net_mode = 'static' if use_static else 'dhcp'
            if net_mode == 'static':
                net_cidr = need_field('SOPHON_NETWORK_CIDR', 'InfraVM IPv4 CIDR (e.g. 10.0.60.101/24)', 'text', net_cidr, True)
                net_gw = need_field('SOPHON_NETWORK_GATEWAY', 'InfraVM IPv4 gateway', 'ipv4', net_gw, True)
                if not raw_net_cidr and not dev_cidr and sys.stdin.isatty():
                    prompted = True
                    dev_cidr = console.prompt_field(
                        'SOPHON_DEV_NETWORK_CIDR',
                        'Optional Dev / Restore-Drill IPv4 CIDR (e.g. 10.0.60.107/24, Enter to skip)',
                        'text',
                        dev_cidr,
                        False,
                    )
            if not raw_net_dns and not raw_net_cidr and sys.stdin.isatty():
                prompted = True
                net_dns = console.prompt_field('SOPHON_VNET_DNS', 'Upstream DNS server IPv4', 'ipv4', net_dns, True)
            if not raw_infravm_disk_gb and not raw_net_cidr and sys.stdin.isatty():
                prompted = True
                infravm_disk_gb = _prompt_disk_gb(
                    'SOPHON_INFRAVM_DISK_GB',
                    'InfraVM thin virtual disk size in GiB (min 40)',
                    infravm_disk_gb,
                    40,
                )
            env['SOPHON_DOMAIN'] = need_field('SOPHON_DOMAIN', 'Sophon domain', 'host', env.get('SOPHON_DOMAIN', ''), True)
            default_realm_display = env.get('SOPHON_KEYCLOAK_REALM', '').strip() or 'sophon'
            env['SOPHON_KEYCLOAK_REALM_DISPLAY_NAME'] = need_field(
                'SOPHON_KEYCLOAK_REALM_DISPLAY_NAME',
                'Keycloak realm display name (e.g. Slow Jedi)',
                'text',
                env.get('SOPHON_KEYCLOAK_REALM_DISPLAY_NAME', ''),
                False,
                default_realm_display,
            )

            if not with_backup and not explicit_without_backup and not env.get('SOPHON_KOPIA_SFTP_HOST', '').strip():
                require_tty('SOPHON_BACKUP_NETWORK_CIDR or SOPHON_KOPIA_SFTP_HOST')
                prompted = True
                with_backup = console.ask_yes_no('Also generate a paired unattended secrets ISO for a Sophon SFTP Backup VM?', default=True)

            if with_backup:
                bk_cidr = need_field('SOPHON_BACKUP_NETWORK_CIDR', 'Backup VM IPv4 CIDR (e.g. 10.0.60.100/24)',
                                     'text', env.get('SOPHON_BACKUP_NETWORK_CIDR', ''), True)
                env['SOPHON_BACKUP_NETWORK_CIDR'] = bk_cidr
                env['SOPHON_BACKUP_NETWORK_GATEWAY'] = env.get('SOPHON_BACKUP_NETWORK_GATEWAY', '').strip() or net_gw
                if not env['SOPHON_BACKUP_NETWORK_GATEWAY']:
                    env['SOPHON_BACKUP_NETWORK_GATEWAY'] = need_field(
                        'SOPHON_BACKUP_NETWORK_GATEWAY', 'Backup VM IPv4 gateway', 'ipv4', '', True)
                env['SOPHON_BACKUP_NETWORK_DNS'] = env.get('SOPHON_BACKUP_NETWORK_DNS', '').strip() or net_dns
                env.setdefault('SOPHON_KOPIA_SFTP_HOST', bk_cidr.split('/')[0])
                if not raw_backup_disk_gb and not raw_bk_cidr and sys.stdin.isatty():
                    prompted = True
                    backup_disk_gb = _prompt_disk_gb(
                        'SOPHON_BACKUP_VM_DISK_GB',
                        'Backup VM thin virtual disk size in GiB (min 16)',
                        backup_disk_gb,
                        16,
                    )
                if not backup_admin_hash and not backup_admin_plain:
                    backup_admin_plain = core_plain
            else:
                env['SOPHON_KOPIA_SFTP_HOST'] = need_field(
                    'SOPHON_KOPIA_SFTP_HOST', 'SFTP server hostname or IPv4', 'host', env.get('SOPHON_KOPIA_SFTP_HOST', ''), True)

            env.setdefault('SOPHON_KOPIA_SFTP_PORT', '22')
            env.setdefault('SOPHON_KOPIA_SFTP_PATH', '/repository')
            if not env.get('SOPHON_KOPIA_PASSWORD'):
                require_tty('SOPHON_KOPIA_PASSWORD')
                prompted = True
                while True:
                    kpw = console.read_secret('Kopia repository encryption password: ')
                    if not kpw.strip():
                        print('Kopia password is required.')
                    elif kpw != console.read_secret('Confirm Kopia repository password: '):
                        print('Passwords do not match.')
                    else:
                        env['SOPHON_KOPIA_PASSWORD'] = kpw
                        break
            fields = deployment.step_fields('configure')
            has_cf_token = bool(env.get('SOPHON_CF_TOKEN', '').strip())
            is_airgapped = env.get('SOPHON_AIRGAPPED', '').strip().lower() in ('1', 'true', 'yes')
            has_any_acme = find_existing_acme_bytes(env['SOPHON_DOMAIN'], options.state_dir, options.acme_file) is not None
            has_acme = False if options.renew_certs else has_any_acme
            if has_cf_token:
                has_internet = True
            elif options.acme_file or is_airgapped or options.renew_certs:
                has_internet = False
            else:
                require_tty('SOPHON_CF_TOKEN')
                prompted = True
                has_internet = console.ask_yes_no('Does InfraVM have outbound internet access?', default=not has_any_acme)
            if not has_internet:
                env['SOPHON_CF_TOKEN'] = ''
                env['SOPHON_CF_TUNNEL_ENABLED'] = 'false'
                if not has_acme:
                    env['SOPHON_ACME_EMAIL'] = need_field(
                        'SOPHON_ACME_EMAIL', 'ACME email', 'email', env.get('SOPHON_ACME_EMAIL', ''), True, 'admin@example.com')
                    print('Issuing wildcard certificate on this connected workstation for air-gapped InfraVM...')
                    issue_args = [
                        'issue',
                        '--domain', env['SOPHON_DOMAIN'],
                        '--email', env['SOPHON_ACME_EMAIL'],
                        '--state-dir', str(options.state_dir.expanduser().resolve() / 'certificates'),
                        '--output-iso', str(options.output_iso),
                        *(['--force'] if options.renew_certs else []),
                    ]
                    if certificates.main(issue_args) != 0:
                        return 1
            else:
                if not env.get('SOPHON_CF_TOKEN'):
                    require_tty('SOPHON_CF_TOKEN')
                    prompted = True
                    env['SOPHON_CF_TOKEN'] = console.read_secret('Cloudflare API token (SOPHON_CF_TOKEN): ')
                env['SOPHON_ACME_EMAIL'] = need_field(
                    'SOPHON_ACME_EMAIL', 'ACME email', 'email', env.get('SOPHON_ACME_EMAIL', ''), True, 'admin@example.com')
                if options.renew_certs:
                    print('Issuing/renewing wildcard certificate on this workstation for sophon-secrets.iso...')
                    if certificates.main([
                        'issue',
                        '--domain', env['SOPHON_DOMAIN'],
                        '--email', env['SOPHON_ACME_EMAIL'],
                        '--state-dir', str(options.state_dir.expanduser().resolve() / 'certificates'),
                        '--output-iso', str(options.output_iso),
                        '--force',
                    ]) != 0:
                        return 1
                if not env.get('SOPHON_CF_TUNNEL_ENABLED', '').strip():
                    if has_cf_token:
                        env['SOPHON_CF_TUNNEL_ENABLED'] = 'true'
                    else:
                        require_tty('SOPHON_CF_TUNNEL_ENABLED')
                        prompted = True
                        tunnel_ok = console.ask_yes_no('Publish public Cloudflare tunnel & DNS records on Day 1?', default=True)
                        env['SOPHON_CF_TUNNEL_ENABLED'] = 'true' if tunnel_ok else 'false'

            for name, (label, _grp, kind, default, required) in fields.items():
                if kind == 'password' and required and not env.get(name) and name not in backup_destination_material():
                    require_tty(name)
                    prompted = True
                    env[name] = console.prompt_field(name, label, kind, default, True)

        if not core_hash:
            if not core_plain:
                raise ValueError('Supply SOPHON_CORE_PASSWORD, SOPHON_SHARED_PASSWORD, or SOPHON_CORE_PASSWORD_HASH.')
            core_hash = sha512_crypt(core_plain)

        infravm_disk_gb = _validate_disk_gb('SOPHON_INFRAVM_DISK_GB', infravm_disk_gb, 40)
        if with_backup or raw_backup_disk_gb:
            backup_disk_gb = _validate_disk_gb('SOPHON_BACKUP_VM_DISK_GB', backup_disk_gb, 16)

        if not net_mode:
            net_mode = 'static' if net_cidr else 'dhcp'
        if net_mode == 'static':
            if not net_cidr or not net_gw:
                raise ValueError('Static InfraVM network requires SOPHON_NETWORK_CIDR and SOPHON_NETWORK_GATEWAY.')
            infra_network = {'mode': 'static', 'cidr': net_cidr, 'gateway': net_gw, 'dns': net_dns}
            dev_network = {'mode': 'static', 'cidr': dev_cidr, 'gateway': dev_gw, 'dns': dev_dns} if dev_cidr else None
        elif net_mode == 'dhcp':
            infra_network = {'mode': 'dhcp', 'dns': net_dns}
            dev_network = {'mode': 'dhcp', 'dns': dev_dns or net_dns} if dev_dns else None
        else:
            raise ValueError('SOPHON_NETWORK_MODE must be static or dhcp.')

        env.setdefault('SOPHON_KOPIA_SFTP_PORT', '22')
        env.setdefault('SOPHON_KOPIA_SFTP_PATH', '/repository')
        if readonly_sftp:
            env['SOPHON_KOPIA_SFTP_USERNAME'] = 'sophon-restore'
        else:
            env.setdefault('SOPHON_KOPIA_SFTP_USERNAME', 'sophon-backup')

        if with_backup:
            bk_mode = (env.get('SOPHON_BACKUP_NETWORK_MODE') or ('static' if env.get('SOPHON_BACKUP_NETWORK_CIDR') else 'dhcp')).strip().lower()
            if bk_mode == 'static':
                bk_cidr = env.get('SOPHON_BACKUP_NETWORK_CIDR', '').strip()
                bk_gw = env.get('SOPHON_BACKUP_NETWORK_GATEWAY', '').strip() or net_gw
                bk_dns = env.get('SOPHON_BACKUP_NETWORK_DNS', '').strip() or net_dns
                if not bk_cidr or not bk_gw:
                    raise ValueError('Backup VM static network requires SOPHON_BACKUP_NETWORK_CIDR and SOPHON_BACKUP_NETWORK_GATEWAY.')
                bk_net = validate_ipv4_network(bk_cidr, bk_gw, bk_dns)
                env.setdefault('SOPHON_KOPIA_SFTP_HOST', bk_net['ip'])
            else:
                bk_net = {'mode': 'dhcp', 'dns': env.get('SOPHON_BACKUP_NETWORK_DNS', '').strip() or net_dns}
            if not env.get('SOPHON_KOPIA_SFTP_HOST'):
                raise ValueError('Supply SOPHON_KOPIA_SFTP_HOST when using DHCP for the Backup VM.')

        sftp_host = env.get('SOPHON_KOPIA_SFTP_HOST', '').strip()
        if not sftp_host:
            raise ValueError('SOPHON_KOPIA_SFTP_HOST is required.')
        sftp_port = int(env.get('SOPHON_KOPIA_SFTP_PORT', '22'))

        paired = None
        sftp_keys = None
        if with_backup or not (env.get('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64') and env.get('SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64')):
            paired = ensure_paired_sftp_keys(options.state_dir, sftp_host, sftp_port)
            backup_key_b64 = base64.b64encode(paired['client_backup_private_key']).decode('ascii')
            restore_key_b64 = base64.b64encode(paired['client_restore_private_key']).decode('ascii')
            sftp_keys = {
                'backup_private_key_b64': backup_key_b64,
                'restore_private_key_b64': restore_key_b64,
            }
            env.setdefault('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', restore_key_b64 if readonly_sftp else backup_key_b64)
            env.setdefault('SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64', base64.b64encode(paired['known_hosts']).decode('ascii'))

        domain = env.get('SOPHON_DOMAIN', '').strip()
        if not domain:
            raise ValueError('SOPHON_DOMAIN is required.')
        acme_bytes = None
        if not env.get('SOPHON_CF_TOKEN', '').strip() or options.acme_file:
            acme_bytes = find_existing_acme_bytes(domain, options.state_dir, options.acme_file)
        else:
            cached_acme = options.state_dir.expanduser().resolve() / 'certificates' / certificates.normalize_domain(domain) / 'acme.json'
            if cached_acme.is_file() and not cached_acme.is_symlink() and certificates.ready_to_export(cached_acme, domain):
                acme_bytes = certificates.minimal_store(cached_acme.read_bytes(), domain)

        if with_backup:
            if not backup_admin_hash:
                if not backup_admin_plain:
                    backup_admin_plain = core_plain
                if not backup_admin_plain:
                    raise ValueError('Supply SOPHON_BACKUP_ADMIN_PASSWORD or SOPHON_BACKUP_ADMIN_PASSWORD_HASH.')
                backup_admin_hash = sha512_crypt(backup_admin_plain)
            bk_iso = build_backup_secrets_iso(
                admin_password_hash=backup_admin_hash,
                network=bk_net,
                sftp_material=paired,
                output=options.backup_output_iso,
            )
            print(f'Backup Server secrets ISO written: {bk_iso} ({bk_iso.stat().st_size} bytes, label {BACKUP_SECRETS_LABEL})')
        elif options.backup_output_iso == BACKUP_SECRETS_ISO and BACKUP_SECRETS_ISO.exists() and not BACKUP_SECRETS_ISO.is_symlink():
            BACKUP_SECRETS_ISO.unlink(missing_ok=True)

        infra_iso = build_infravm_secrets_iso(
            core_password_hash=core_hash,
            network=infra_network,
            configure_values=env,
            dev_network=dev_network,
            sftp_keys=sftp_keys,
            acme_bytes=acme_bytes,
            output=options.output_iso,
        )
        print(f'InfraVM secrets ISO written: {infra_iso} ({infra_iso.stat().st_size} bytes, label {certificates.CERTIFICATE_LABEL})')

        should_offer_dotenv = (not options.run) and sys.stdin.isatty() and (prompted or not options.dotenv_path.exists())
        if should_offer_dotenv:
            dotenv_updates: dict[str, str] = {}
            effective_shared_pw = shared_pw or (core_plain if used_shared_service_password else '')
            if effective_shared_pw:
                dotenv_updates['SOPHON_SHARED_PASSWORD'] = effective_shared_pw
                if core_plain and core_plain != effective_shared_pw:
                    dotenv_updates['SOPHON_CORE_PASSWORD'] = core_plain
            elif core_plain:
                dotenv_updates['SOPHON_CORE_PASSWORD'] = core_plain
            elif core_hash:
                dotenv_updates['SOPHON_CORE_PASSWORD_HASH'] = core_hash

            for name, (_label, _grp, kind, _default, required) in deployment.step_fields('configure').items():
                if kind == 'password' and required and name not in backup_destination_material() and env.get(name):
                    if not effective_shared_pw or env[name] != effective_shared_pw:
                        dotenv_updates[name] = env[name]

            if with_backup:
                if raw_backup_admin_plain:
                    dotenv_updates['SOPHON_BACKUP_ADMIN_PASSWORD'] = raw_backup_admin_plain
                elif env.get('SOPHON_BACKUP_ADMIN_PASSWORD_HASH', '').strip():
                    dotenv_updates['SOPHON_BACKUP_ADMIN_PASSWORD_HASH'] = env['SOPHON_BACKUP_ADMIN_PASSWORD_HASH'].strip()

            dotenv_updates['SOPHON_NETWORK_MODE'] = net_mode
            if net_mode == 'static':
                dotenv_updates['SOPHON_NETWORK_CIDR'] = net_cidr
                dotenv_updates['SOPHON_NETWORK_GATEWAY'] = net_gw
                if dev_cidr:
                    dotenv_updates['SOPHON_DEV_NETWORK_CIDR'] = dev_cidr
                if dev_gw:
                    dotenv_updates['SOPHON_DEV_NETWORK_GATEWAY'] = dev_gw
                if dev_dns:
                    dotenv_updates['SOPHON_DEV_VNET_DNS'] = dev_dns
            else:
                dotenv_updates['SOPHON_NETWORK_CIDR'] = ''
                dotenv_updates['SOPHON_NETWORK_GATEWAY'] = ''
                if dev_dns:
                    dotenv_updates['SOPHON_DEV_VNET_DNS'] = dev_dns
            dotenv_updates['SOPHON_VNET_DNS'] = net_dns
            dotenv_updates['SOPHON_INFRAVM_DISK_GB'] = infravm_disk_gb
            dotenv_updates['SOPHON_DOMAIN'] = domain
            if env.get('SOPHON_KEYCLOAK_REALM_DISPLAY_NAME', '').strip():
                dotenv_updates['SOPHON_KEYCLOAK_REALM_DISPLAY_NAME'] = env['SOPHON_KEYCLOAK_REALM_DISPLAY_NAME'].strip()
            dotenv_updates['SOPHON_WITH_BACKUP_SECRETS'] = 'true' if with_backup else 'false'
            if with_backup:
                dotenv_updates['SOPHON_BACKUP_VM_DISK_GB'] = backup_disk_gb
                dotenv_updates['SOPHON_BACKUP_NETWORK_MODE'] = bk_mode
                if bk_mode == 'static':
                    dotenv_updates['SOPHON_BACKUP_NETWORK_CIDR'] = env['SOPHON_BACKUP_NETWORK_CIDR'].strip()
                    dotenv_updates['SOPHON_BACKUP_NETWORK_GATEWAY'] = env['SOPHON_BACKUP_NETWORK_GATEWAY'].strip()
                    dotenv_updates['SOPHON_BACKUP_NETWORK_DNS'] = env['SOPHON_BACKUP_NETWORK_DNS'].strip()
                else:
                    dotenv_updates['SOPHON_BACKUP_NETWORK_DNS'] = bk_net['dns']
            elif raw_backup_disk_gb:
                dotenv_updates['SOPHON_BACKUP_VM_DISK_GB'] = backup_disk_gb
            dotenv_updates['SOPHON_KOPIA_SFTP_HOST'] = sftp_host
            if str(sftp_port) != '22':
                dotenv_updates['SOPHON_KOPIA_SFTP_PORT'] = str(sftp_port)
            if env.get('SOPHON_KOPIA_SFTP_PATH', '/repository') != '/repository':
                dotenv_updates['SOPHON_KOPIA_SFTP_PATH'] = env['SOPHON_KOPIA_SFTP_PATH']
            if readonly_sftp:
                dotenv_updates['SOPHON_SECRETS_READONLY_SFTP'] = 'true'
            if env.get('SOPHON_KOPIA_PASSWORD'):
                dotenv_updates['SOPHON_KOPIA_PASSWORD'] = env['SOPHON_KOPIA_PASSWORD']

            if env.get('SOPHON_CF_TOKEN', '').strip():
                dotenv_updates['SOPHON_AIRGAPPED'] = ''
                dotenv_updates['SOPHON_CF_TOKEN'] = env['SOPHON_CF_TOKEN'].strip()
                if env.get('SOPHON_ACME_EMAIL', '').strip():
                    dotenv_updates['SOPHON_ACME_EMAIL'] = env['SOPHON_ACME_EMAIL'].strip()
                dotenv_updates['SOPHON_CF_TUNNEL_ENABLED'] = env.get('SOPHON_CF_TUNNEL_ENABLED', 'true').strip().lower()
            else:
                dotenv_updates['SOPHON_AIRGAPPED'] = 'true'
                dotenv_updates['SOPHON_CF_TOKEN'] = ''
                dotenv_updates['SOPHON_CF_TUNNEL_ENABLED'] = 'false'
                if env.get('SOPHON_ACME_EMAIL', '').strip():
                    dotenv_updates['SOPHON_ACME_EMAIL'] = env['SOPHON_ACME_EMAIL'].strip()
                if options.acme_file:
                    dotenv_updates['SOPHON_ACME_FILE'] = options.acme_file

            if options.defer_dotenv is not None:
                _write_private(
                    options.defer_dotenv,
                    (json.dumps({'dotenv_path': str(options.dotenv_path), 'updates': dotenv_updates}) + '\n').encode('utf-8'),
                )
            else:
                _prompt_and_persist_dotenv(dotenv_updates, options.dotenv_path)
        elif options.defer_dotenv is not None:
            options.defer_dotenv.unlink(missing_ok=True)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'Secrets ISO generation failed: {error}', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nSecrets ISO generation cancelled.', file=sys.stderr)
        return 130


def backup_destination_material() -> tuple[str, str]:
    return ('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64')


if __name__ == '__main__':
    sys.exit(main())
