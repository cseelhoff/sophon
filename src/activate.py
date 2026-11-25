"""Promote a standby-restored InfraVM into active production (`sophon-activate`).

Switches Kopia from `--readonly` standby to active scheduled backups (running and verifying an
immediate snapshot) and, when configured, registers Cloudflare tunnel DNS routes and starts `cloudflared`.
"""

import argparse
import json
import os
from pathlib import Path
import sys

import backup_destination
import console
import deployment

KOPIA_ENV_MAP = {
    'KOPIA_PASSWORD': 'SOPHON_KOPIA_PASSWORD',
    'KOPIA_SERVER_USERNAME': 'SOPHON_KOPIA_SERVER_USERNAME',
    'KOPIA_SERVER_PASSWORD': 'SOPHON_KOPIA_SERVER_PASSWORD',
    'KOPIA_SFTP_HOST': 'SOPHON_KOPIA_SFTP_HOST',
    'KOPIA_SFTP_PORT': 'SOPHON_KOPIA_SFTP_PORT',
    'KOPIA_SFTP_USERNAME': 'SOPHON_KOPIA_SFTP_USERNAME',
    'KOPIA_SFTP_PATH': 'SOPHON_KOPIA_SFTP_PATH',
    'KOPIA_SFTP_PRIVATE_KEY_B64': 'SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64',
    'KOPIA_SFTP_KNOWN_HOSTS_B64': 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64',
    'KOPIA_CLIENT_USERNAME': 'SOPHON_KOPIA_CLIENT_USERNAME',
    'KOPIA_CLIENT_HOSTNAME': 'SOPHON_KOPIA_CLIENT_HOSTNAME',
    'KOPIA_SNAPSHOT_INTERVAL': 'SOPHON_KOPIA_SNAPSHOT_INTERVAL',
}


def _parse_kopia_env_stdin(raw: str) -> dict[str, str]:
    entries = json.loads(raw or '[]')
    if not isinstance(entries, list):
        raise ValueError('Expected JSON list of container environment entries.')
    mapped: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, str) or '=' not in item:
            continue
        key, val = item.split('=', 1)
        target = KOPIA_ENV_MAP.get(key)
        if target and val != '':
            mapped[target] = val
    return mapped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Promote a standby-restored Sophon InfraVM to active production.')
    parser.add_argument('--yes', '-y', action='store_true', help='Activate without interactive confirmation')
    parser.add_argument('--enable-tunnel', action='store_true', help='Enable Cloudflare tunnel publishing')
    parser.add_argument('--disable-tunnel', action='store_true', help='Keep Cloudflare tunnel disabled')
    options = parser.parse_args(argv)

    if os.environ.get('SOPHON_DEPLOYMENT_STEP') != 'configure':
        parser.error('Start this program using sophon-activate on installed InfraVM.')
    if options.enable_tunnel and options.disable_tunnel:
        parser.error('Cannot combine --enable-tunnel and --disable-tunnel.')

    try:
        site = deployment.load_site_settings()
        for k, v in site.items():
            os.environ.setdefault(k, v)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    standby_path = deployment.ROOT / 'standby.json'
    standby = {}
    if standby_path.is_file():
        standby = json.loads(standby_path.read_text(encoding='utf-8'))

    raw_kopia_env = os.environ.pop('SOPHON_ACTIVATE_KOPIA_ENV_JSON', '')
    kopia_env = _parse_kopia_env_stdin(raw_kopia_env) if raw_kopia_env else {}
    values = dict(kopia_env)
    for k, v in os.environ.items():
        if k.startswith('SOPHON_') and v != '':
            values[k] = v

    domain = values.get('SOPHON_DOMAIN') or standby.get('domain', '')
    infravm_ip = values.get('SOPHON_INFRAVM_IP', '')
    if not domain or not infravm_ip:
        print('Missing SOPHON_DOMAIN or SOPHON_INFRAVM_IP; run sophon-deploy first.', file=sys.stderr)
        return 2
    values['SOPHON_DOMAIN'] = domain
    values['SOPHON_INFRAVM_IP'] = infravm_ip

    sftp_user = values.get('SOPHON_KOPIA_SFTP_USERNAME', '')
    if sftp_user == 'sophon-restore' and standby.get('desired_sftp_private_key_b64'):
        sftp_user = standby.get('desired_sftp_username') or 'sophon-backup'
        values['SOPHON_KOPIA_SFTP_USERNAME'] = sftp_user
        values['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64'] = standby['desired_sftp_private_key_b64']
    if sftp_user == 'sophon-restore':
        print(
            "Cannot promote to active production while paired with the confined read-only 'sophon-restore' SFTP account.\n"
            "Pair with 'sophon-backup' via sophon-deploy first so scheduled backups have write permission.",
            file=sys.stderr,
        )
        return 2

    want_tunnel = bool(standby.get('desired_cf_tunnel_enabled', False))
    if options.enable_tunnel:
        want_tunnel = True
    elif options.disable_tunnel:
        want_tunnel = False

    cf_token = (values.get('SOPHON_CF_TOKEN') or standby.get('desired_cf_token') or '').strip()
    if want_tunnel and not cf_token:
        if not sys.stdin.isatty():
            print('Cloudflare tunnel activation requires SOPHON_CF_TOKEN (or pass --disable-tunnel).', file=sys.stderr)
            return 2
        cf_token = console.read_secret('Cloudflare API token (SOPHON_CF_TOKEN): ').strip()
        if not cf_token:
            print('SOPHON_CF_TOKEN is required when enabling Cloudflare tunnel.', file=sys.stderr)
            return 2

    values['SOPHON_CF_TUNNEL_ENABLED'] = 'true' if want_tunnel else 'false'
    values['SOPHON_CF_TOKEN'] = cf_token if want_tunnel else ''
    values['SOPHON_CF_TUNNEL_NAME'] = values.get('SOPHON_CF_TUNNEL_NAME') or standby.get('desired_cf_tunnel_name', 'sophon')
    if 'SOPHON_CF_ACCESS_ENABLED' not in values or not str(values['SOPHON_CF_ACCESS_ENABLED']).strip():
        values['SOPHON_CF_ACCESS_ENABLED'] = 'true' if standby.get('desired_cf_access_enabled', True) else 'false'
    if ('SOPHON_CF_ACCESS_EMAILS' not in values or not str(values['SOPHON_CF_ACCESS_EMAILS']).strip()) and standby.get('desired_cf_access_emails'):
        values['SOPHON_CF_ACCESS_EMAILS'] = str(standby['desired_cf_access_emails']).strip()
    values['SOPHON_ACME_EMAIL'] = values.get('SOPHON_ACME_EMAIL') or standby.get('desired_acme_email', 'admin@example.com')
    values['SOPHON_KOPIA_BACKUPS_ENABLED'] = 'true'
    values['SOPHON_KOPIA_SNAPSHOT_INTERVAL'] = values.get('SOPHON_KOPIA_SNAPSHOT_INTERVAL') or standby.get('desired_snapshot_interval', '6h')
    values['SOPHON_KOPIA_COARSE_ENABLED'] = 'true' if standby.get('desired_coarse_enabled', False) else values.get('SOPHON_KOPIA_COARSE_ENABLED', 'false')
    values['SOPHON_KOPIA_COARSE_VOLUMES_DIR'] = standby.get(
        'desired_coarse_volumes_dir', '/var/home/sophon/.local/share/containers/storage/volumes'
    )
    values['SOPHON_KOPIA_COARSE_WORK_DIR'] = standby.get('desired_coarse_work_dir', '/var/home/sophon/sophon-coarse')

    if not options.yes:
        if not sys.stdin.isatty():
            print('Pass --yes for non-interactive production activation.', file=sys.stderr)
            return 2
        tunnel_desc = f"enabled ({values['SOPHON_CF_TUNNEL_NAME']})" if want_tunnel else 'disabled'
        if not console.ask_yes_no(
            f'Promote {domain} ({infravm_ip}) to active production '
            f'(scheduled {values["SOPHON_KOPIA_SNAPSHOT_INTERVAL"]} backups: enabled, Cloudflare tunnel: {tunnel_desc})?',
            default=False,
        ):
            print('Activation cancelled; server remains in safe read-only standby.')
            return 130

    try:
        values = backup_destination.prepare(
            values,
            root=deployment.ROOT,
            confirm=None if options.yes else console.ask_yes_no,
            readonly=False,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Production activation preflight failed: {error}', file=sys.stderr)
        return 2

    saved_env = os.environ.copy()
    try:
        os.environ.pop('SOPHON_PREPARE_RESTORE', None)
        os.environ.update(values)
        os.environ['SOPHON_KOPIA_PREFLIGHT_COMPLETE'] = 'true'
        rc = deployment.main(['--step', 'configure', '--', '5-activate.yml'])
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    if rc == 0:
        standby_path.unlink(missing_ok=True)
        console._write_json_private(
            deployment.ROOT / 'deploy-result.json',
            {
                'mode': 'production_active',
                'domain': domain,
                'infravm_ip': infravm_ip,
                'prepare_restore': False,
            },
        )
    return rc


if __name__ == '__main__':
    sys.exit(main())
