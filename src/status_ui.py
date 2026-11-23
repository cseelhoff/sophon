"""Sophon Appliance Status & Disaster-Recovery Cutover Web UI (`https://<domain>`).

Runs on the pre-loaded `sophon:latest` image behind Traefik TLS with Keycloak OIDC (`arcane-admins`
group required). Mounts only `/var/lib/sophon/control` (never `/var/lib/sophon/project` or Docker
socket), probes whether the target production IP is still occupied on the network before enabling
the Promote button, and triggers `sophon-activate` via the detached host `sophon-web-activate.path`
systemd unit so switching the host NIC from dev IP to prod IP never interrupts cutover.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import errno
import hashlib
import hmac
import html
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SESSION_COOKIE = '__Host-sophon_session'
LOGIN_COOKIE = '__Host-sophon_oidc'
DEFAULT_PROBE_PORTS = (443, 80, 22, 53)
ALLOWED_TUNNEL_MODES = frozenset(('default', 'enable', 'disable'))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(raw: str) -> bytes:
    padded = raw + '=' * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode('ascii'))


def sign_cookie(payload: dict, secret: bytes) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8'))
    sig = _b64url_encode(hmac.new(secret, body.encode('ascii'), hashlib.sha256).digest())
    return f'{body}.{sig}'


def verify_cookie(token: str, secret: bytes, *, now: float | None = None) -> dict | None:
    if not token or '.' not in token:
        return None
    body, sig = token.rsplit('.', 1)
    expected = _b64url_encode(hmac.new(secret, body.encode('ascii'), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64url_decode(body).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    current = time.time() if now is None else now
    exp = data.get('exp')
    if not isinstance(exp, (int, float)) or exp < current:
        return None
    return data


def make_pkce_pair() -> tuple[str, str]:
    verifier = _b64url_encode(secrets.token_bytes(32))
    challenge = _b64url_encode(hashlib.sha256(verifier.encode('ascii')).digest())
    return verifier, challenge


def _jwk_to_public_key(jwk: dict):
    kty = jwk.get('kty')
    if kty == 'RSA':
        n = int.from_bytes(_b64url_decode(jwk['n']), 'big')
        e = int.from_bytes(_b64url_decode(jwk['e']), 'big')
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == 'EC':
        crv_map = {'P-256': ec.SECP256R1(), 'P-384': ec.SECP384R1(), 'P-521': ec.SECP521R1()}
        curve = crv_map.get(jwk.get('crv', ''))
        if curve is None:
            raise ValueError(f"Unsupported EC curve: {jwk.get('crv')}")
        x = int.from_bytes(_b64url_decode(jwk['x']), 'big')
        y = int.from_bytes(_b64url_decode(jwk['y']), 'big')
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    raise ValueError(f'Unsupported JWK kty: {kty!r}')


def verify_oidc_id_token(
    id_token: str,
    jwks: dict,
    *,
    expected_issuer: str,
    expected_client_id: str,
    expected_nonce: str,
    now: float | None = None,
) -> dict:
    parts = id_token.split('.')
    if len(parts) != 3:
        raise ValueError('Malformed JWT id_token.')
    header_b64, payload_b64, sig_b64 = parts
    header = json.loads(_b64url_decode(header_b64).decode('utf-8'))
    claims = json.loads(_b64url_decode(payload_b64).decode('utf-8'))
    signature = _b64url_decode(sig_b64)
    signing_input = f'{header_b64}.{payload_b64}'.encode('ascii')

    alg = header.get('alg', '')
    kid = header.get('kid')
    keys = jwks.get('keys') or []
    candidates = [k for k in keys if not kid or k.get('kid') == kid]
    if not candidates:
        raise ValueError('No matching JWK found for id_token.')

    verified = False
    for jwk in candidates:
        pub = _jwk_to_public_key(jwk)
        try:
            if alg == 'RS256' and isinstance(pub, rsa.RSAPublicKey):
                pub.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
                verified = True
                break
            if alg in ('ES256', 'ES384') and isinstance(pub, ec.EllipticCurvePublicKey):
                coord_size = 32 if alg == 'ES256' else 48
                if len(signature) != coord_size * 2:
                    continue
                r = int.from_bytes(signature[:coord_size], 'big')
                s = int.from_bytes(signature[coord_size:], 'big')
                der_sig = utils.encode_dss_signature(r, s)
                hash_alg = hashes.SHA256() if alg == 'ES256' else hashes.SHA384()
                pub.verify(der_sig, signing_input, ec.ECDSA(hash_alg))
                verified = True
                break
        except InvalidSignature:
            continue
    if not verified:
        raise ValueError('JWT signature verification failed.')

    if claims.get('iss') != expected_issuer:
        raise ValueError(f"Unexpected token issuer {claims.get('iss')!r} (expected {expected_issuer!r}).")

    aud = claims.get('aud')
    aud_set = {aud} if isinstance(aud, str) else set(aud or [])
    if expected_client_id not in aud_set and claims.get('azp') != expected_client_id:
        raise ValueError('Token audience does not match sophon-status.')

    current = time.time() if now is None else now
    exp = claims.get('exp')
    if not isinstance(exp, (int, float)) or exp < current - 30:
        raise ValueError('ID token has expired.')

    if expected_nonce and claims.get('nonce') != expected_nonce:
        raise ValueError('ID token nonce mismatch.')

    return claims


def user_has_admin_group(claims: dict, admin_group: str = 'arcane-admins') -> bool:
    groups = claims.get('groups') or []
    if isinstance(groups, str):
        groups = [groups]
    normalized = {str(g).strip().lstrip('/') for g in groups if g}
    return admin_group.lstrip('/') in normalized


def _probe_single_tcp_port(target_ip: str, port: int, timeout: float) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        err = sock.connect_ex((target_ip, port))
    except OSError:
        return False, ''
    finally:
        sock.close()
    if err == 0:
        return True, f'TCP port {port} is accepting connections on {target_ip}.'
    if err == errno.ECONNREFUSED:
        return True, f'Host {target_ip} actively refused TCP port {port} (IP stack is online).'
    return False, ''


def probe_tcp_ip_occupied(
    target_ip: str,
    *,
    ports: tuple[int, ...] = DEFAULT_PROBE_PORTS,
    timeout: float = 0.5,
) -> tuple[bool, str]:
    """Return `(True, reason)` if `target_ip` answers TCP SYN with SYN-ACK or RST on any probed port."""
    if not target_ip:
        return False, ''
    with ThreadPoolExecutor(max_workers=max(1, len(ports))) as pool:
        futures = [pool.submit(_probe_single_tcp_port, target_ip, port, timeout) for port in ports]
        for fut in futures:
            occupied, reason = fut.result()
            if occupied:
                return True, reason
    return False, ''


def evaluate_cutover_readiness(
    appliance_state: dict,
    activate_status: dict | None = None,
    refresh_certs_status: dict | None = None,
    *,
    tcp_probe_fn=probe_tcp_ip_occupied,
) -> dict:
    """Compute standby/production status and whether Promote / Update Certificates buttons are enabled."""
    activate_status = activate_status or {}
    refresh_certs_status = refresh_certs_status or {}
    standby = bool(appliance_state.get('standby', False))
    mode = str(appliance_state.get('mode') or ('standby_restore' if standby else 'production_active'))
    domain = str(appliance_state.get('domain') or '')
    current_ip = str(appliance_state.get('current_ip') or '')
    target_cidr = str(appliance_state.get('target_cidr') or '')
    target_ip = str(appliance_state.get('target_ip') or '')
    can_promote_sftp = bool(appliance_state.get('can_promote_sftp', True))
    needs_ip_switch = bool(standby and target_ip and target_ip != current_ip)

    host_check = appliance_state.get('target_ip_check') or {}
    host_occupied = bool(host_check.get('occupied', False)) if needs_ip_switch else False
    host_reason = str(host_check.get('reason') or '') if host_occupied else ''

    tcp_occupied = False
    tcp_reason = ''
    if needs_ip_switch:
        tcp_occupied, tcp_reason = tcp_probe_fn(target_ip)

    target_ip_occupied = bool(needs_ip_switch and (tcp_occupied or host_occupied))
    occupancy_reason = tcp_reason or host_reason
    if target_ip_occupied and not occupancy_reason:
        occupancy_reason = f'Target production IP {target_ip} is currently active on the network.'

    act_state = str(activate_status.get('state') or 'idle')
    activation_running = act_state == 'running'

    disabled_reason = ''
    if not standby:
        disabled_reason = 'This Sophon appliance is already running in active production mode.'
    elif activation_running:
        disabled_reason = 'Production cutover is currently in progress.'
    elif target_ip_occupied:
        disabled_reason = (
            f'Target production IP {target_ip} is still in use on the network '
            f'({occupancy_reason}) Shut down or delete the running production Sophon VM '
            '(sh sophon-destroy-vm.sh) before promoting this standby instance.'
        )
    elif not can_promote_sftp:
        disabled_reason = (
            "Paired SFTP credentials are confined to read-only 'sophon-restore' without a write-capable "
            "'sophon-backup' key. Re-pair with 'sophon-backup' before promoting to active production."
        )

    promote_enabled = bool(standby and not disabled_reason)

    cert_disk_raw = appliance_state.get('cert_disk') or {}
    cert_disk_present = bool(cert_disk_raw.get('present', False))
    cert_disk_valid = bool(cert_disk_raw.get('valid', False))
    cert_disk_expires_at = str(cert_disk_raw.get('expires_at') or '')
    cert_disk_error = str(cert_disk_raw.get('error') or '')
    cert_disk_checked_at = str(cert_disk_raw.get('checked_at') or '')

    ref_state = str(refresh_certs_status.get('state') or 'idle')
    refresh_running = ref_state == 'running'

    refresh_disabled_reason = ''
    if activation_running:
        refresh_disabled_reason = 'Production cutover is currently in progress.'
    elif refresh_running:
        refresh_disabled_reason = 'Certificate update is currently in progress.'
    elif not cert_disk_present:
        refresh_disabled_reason = (
            'No certificate disk (SOPHON_CERTS) is attached. '
            "Run 'sh sophon-mount-secrets.sh' on your workstation first."
        )
    elif not cert_disk_valid:
        refresh_disabled_reason = cert_disk_error or 'Attached certificate disk failed validation.'

    refresh_certs_enabled = not bool(refresh_disabled_reason)

    return {
        'standby': standby,
        'mode': mode,
        'domain': domain,
        'current_ip': current_ip,
        'target_cidr': target_cidr,
        'target_ip': target_ip,
        'needs_ip_switch': needs_ip_switch,
        'target_ip_occupied': target_ip_occupied,
        'occupancy_reason': occupancy_reason,
        'checked_at': str(host_check.get('checked_at') or appliance_state.get('updated_at') or ''),
        'can_promote_sftp': can_promote_sftp,
        'desired_cf_tunnel_enabled': bool(appliance_state.get('desired_cf_tunnel_enabled', False)),
        'has_cf_token': bool(appliance_state.get('has_cf_token', False)),
        'desired_cf_tunnel_name': str(appliance_state.get('desired_cf_tunnel_name') or 'sophon'),
        'desired_snapshot_interval': str(appliance_state.get('desired_snapshot_interval') or '6h'),
        'restored_snapshot': str(appliance_state.get('restored_snapshot') or ''),
        'live_cert_expires_at': str(appliance_state.get('live_cert_expires_at') or ''),
        'cert_disk': {
            'present': cert_disk_present,
            'valid': cert_disk_valid,
            'expires_at': cert_disk_expires_at,
            'error': cert_disk_error,
            'checked_at': cert_disk_checked_at,
        },
        'cert_refresh': {
            'state': ref_state,
            'message': str(refresh_certs_status.get('message') or ''),
            'requested_by': str(refresh_certs_status.get('requested_by') or ''),
            'updated_at': str(refresh_certs_status.get('updated_at') or ''),
            'exit_code': int(refresh_certs_status.get('exit_code') or 0),
        },
        'refresh_certs_enabled': refresh_certs_enabled,
        'refresh_certs_disabled_reason': refresh_disabled_reason,
        'activation': {
            'state': act_state,
            'message': str(activate_status.get('message') or ''),
            'requested_by': str(activate_status.get('requested_by') or ''),
            'updated_at': str(activate_status.get('updated_at') or ''),
            'exit_code': int(activate_status.get('exit_code') or 0),
        },
        'promote_enabled': promote_enabled,
        'promote_disabled_reason': disabled_reason,
    }


def _parse_iso_age_seconds(ts: str) -> float:
    if not ts:
        return 999999.0
    try:
        parsed = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())
    except ValueError:
        return 999999.0


class StatusConfig:
    def __init__(
        self,
        *,
        domain: str,
        oidc_realm: str,
        oidc_client_id: str,
        admin_group: str,
        session_secret: bytes,
        control_dir: Path,
        verify_oidc_tls: bool = False,
        tcp_probe_fn=probe_tcp_ip_occupied,
    ):
        self.domain = domain.strip().lower().rstrip('.')
        self.oidc_realm = oidc_realm.strip() or 'sophon'
        self.oidc_client_id = oidc_client_id.strip() or 'sophon-status'
        self.admin_group = admin_group.strip() or 'arcane-admins'
        self.session_secret = session_secret
        self.control_dir = Path(control_dir)
        self.verify_oidc_tls = verify_oidc_tls
        self.tcp_probe_fn = tcp_probe_fn
        self._jwks_cache: tuple[float, dict] | None = None

    @property
    def issuer(self) -> str:
        return f'https://auth.{self.domain}/realms/{self.oidc_realm}'

    @property
    def allowed_hosts(self) -> set[str]:
        return {self.domain, f'status.{self.domain}'}

    def fetch_jwks(self, *, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._jwks_cache and (now - self._jwks_cache[0]) < 300:
            return self._jwks_cache[1]
        resp = requests.get(
            f'{self.issuer}/protocol/openid-connect/certs',
            timeout=10,
            verify=self.verify_oidc_tls,
        )
        resp.raise_for_status()
        jwks = resp.json()
        self._jwks_cache = (now, jwks)
        return jwks

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> dict:
        resp = requests.post(
            f'{self.issuer}/protocol/openid-connect/token',
            data={
                'grant_type': 'authorization_code',
                'client_id': self.oidc_client_id,
                'code': code,
                'redirect_uri': redirect_uri,
                'code_verifier': code_verifier,
            },
            timeout=10,
            verify=self.verify_oidc_tls,
        )
        resp.raise_for_status()
        return resp.json()

    def read_appliance_state(self) -> dict:
        state_path = self.control_dir / 'appliance-state.json'
        if not state_path.is_file():
            return {'standby': False, 'mode': 'unknown', 'domain': self.domain}
        try:
            return json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'standby': False, 'mode': 'unknown', 'domain': self.domain}

    def read_activate_status(self) -> dict:
        status_path = self.control_dir / 'activate-status.json'
        if not status_path.is_file():
            return {'state': 'idle'}
        try:
            return json.loads(status_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'state': 'idle'}

    def read_activate_log_tail(self, max_lines: int = 40) -> str:
        log_path = self.control_dir / 'activate.log'
        if not log_path.is_file():
            return ''
        try:
            lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
            return '\n'.join(lines[-max_lines:])
        except OSError:
            return ''

    def read_refresh_certs_status(self) -> dict:
        status_path = self.control_dir / 'refresh-certs-status.json'
        if not status_path.is_file():
            return {'state': 'idle'}
        try:
            return json.loads(status_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'state': 'idle'}

    def read_refresh_certs_log_tail(self, max_lines: int = 40) -> str:
        log_path = self.control_dir / 'refresh-certs.log'
        if not log_path.is_file():
            return ''
        try:
            lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
            return '\n'.join(lines[-max_lines:])
        except OSError:
            return ''

    def request_host_probe(self, *, force: bool = False, wait_sec: float = 1.5) -> dict:
        state = self.read_appliance_state()
        if not force:
            standby = bool(state.get('standby'))
            target_ip = str(state.get('target_ip') or '')
            current_ip = str(state.get('current_ip') or '')
            if not (standby and target_ip and target_ip != current_ip):
                return state
            checked_at = str((state.get('target_ip_check') or {}).get('checked_at') or '')
            if _parse_iso_age_seconds(checked_at) < 4.0:
                return state
        state_path = self.control_dir / 'appliance-state.json'
        probe_req = self.control_dir / 'probe.request'
        before_mtime = state_path.stat().st_mtime_ns if state_path.is_file() else 0
        try:
            self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            probe_req.write_text('probe\n', encoding='utf-8')
        except OSError:
            return state
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            if not probe_req.exists():
                now_mtime = state_path.stat().st_mtime_ns if state_path.is_file() else 0
                if now_mtime != before_mtime:
                    break
            time.sleep(0.08)
        return self.read_appliance_state()

    def queue_activation(self, *, requested_by: str, tunnel_mode: str = 'default') -> None:
        if tunnel_mode not in ALLOWED_TUNNEL_MODES:
            raise ValueError(f'Unsupported tunnel_mode: {tunnel_mode!r}')
        self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        req_path = self.control_dir / 'activate.request'
        tmp_path = self.control_dir / f'activate.request.tmp.{os.getpid()}'
        payload = {
            'requested_by': requested_by,
            'tunnel_mode': tunnel_mode,
            'requested_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        tmp_path.write_text(json.dumps(payload), encoding='utf-8')
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, req_path)

    def queue_refresh_certs(self, *, requested_by: str) -> None:
        self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        req_path = self.control_dir / 'refresh-certs.request'
        tmp_path = self.control_dir / f'refresh-certs.request.tmp.{os.getpid()}'
        payload = {
            'requested_by': requested_by,
            'requested_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        tmp_path.write_text(json.dumps(payload), encoding='utf-8')
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, req_path)


def render_forbidden_html(username: str, admin_group: str) -> str:
    safe_user = html.escape(username or 'authenticated user')
    safe_group = html.escape(admin_group)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Access Denied — Sophon Appliance</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; display: grid; place-items: center; min-height: 100vh; }}
    .card {{ max-width: 480px; background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 28px; box-shadow: 0 12px 32px rgba(0,0,0,.35); }}
    h1 {{ margin: 0 0 12px; font-size: 1.25rem; color: #f8fafc; }}
    p {{ line-height: 1.55; color: #cbd5e1; margin: 0 0 18px; }}
    code {{ background: #0f172a; padding: 2px 6px; border-radius: 4px; color: #93c5fd; }}
    a.btn {{ display: inline-block; background: #334155; color: #f8fafc; text-decoration: none; padding: 9px 16px; border-radius: 8px; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Administrator group required</h1>
    <p>Signed in as <code>{safe_user}</code>, which is not a member of the <code>{safe_group}</code> group in Keycloak/OpenLDAP.</p>
    <a class="btn" href="/logout">Sign out</a>
  </div>
</body>
</html>"""


def render_dashboard_html(*, domain: str, username: str, csrf_token: str, initial_status: dict) -> str:
    safe_domain = html.escape(domain)
    safe_user = html.escape(username)
    initial_json = json.dumps(initial_status).replace('</', '<\\/')
    csrf_json = json.dumps(csrf_token)
    init_enabled = bool(initial_status.get('promote_enabled'))
    btn_attrs = 'aria-disabled="false"' if init_enabled else 'disabled aria-disabled="true"'
    init_banner_cls = 'banner ready' if init_enabled else 'banner warn'
    init_banner_txt = html.escape(str(initial_status.get('promote_disabled_reason') or ''))
    cutover_style = '' if initial_status.get('standby') else ' style="display:none;"'
    cert_enabled = bool(initial_status.get('refresh_certs_enabled'))
    cert_btn_attrs = 'aria-disabled="false"' if cert_enabled else 'disabled aria-disabled="true"'
    cert_banner_cls = 'banner ready' if cert_enabled else 'banner info'
    cert_banner_txt = html.escape(
        'Renewed certificate disk detected and validated — ready to install, snapshot to Kopia, and eject.'
        if cert_enabled
        else str(initial_status.get('refresh_certs_disabled_reason') or '')
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sophon Appliance — {safe_domain}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1120;
      --panel: #131c31;
      --panel-alt: #0f172a;
      --border: #263452;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.14);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.14);
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.14);
      --blue: #3b82f6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 56px; }}
    header {{
      display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
      gap: 12px; padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 24px;
    }}
    .brand {{ display: flex; align-items: center; gap: 12px; }}
    .brand h1 {{ margin: 0; font-size: 1.35rem; letter-spacing: -0.01em; }}
    .badge {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 11px; border-radius: 999px; font-size: 0.8rem; font-weight: 600;
    }}
    .badge.active {{ background: var(--green-bg); color: #34d399; border: 1px solid rgba(16,185,129,.35); }}
    .badge.standby {{ background: var(--amber-bg); color: #fbbf24; border: 1px solid rgba(245,158,11,.35); }}
    .badge.conflict {{ background: var(--red-bg); color: #fca5a5; border: 1px solid rgba(239,68,68,.38); }}
    .userbar {{ display: flex; align-items: center; gap: 12px; font-size: 0.88rem; color: var(--muted); }}
    .userbar a {{ color: #cbd5e1; text-decoration: none; border: 1px solid var(--border); padding: 5px 11px; border-radius: 7px; }}
    .userbar a:hover {{ background: #1e293b; }}
    .card {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 22px; margin-bottom: 20px;
    }}
    .card h2 {{ margin: 0 0 14px; font-size: 1.08rem; }}
    .kv-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px;
    }}
    .kv {{
      background: var(--panel-alt); border: 1px solid var(--border); border-radius: 9px; padding: 12px 14px;
    }}
    .kv .label {{ font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }}
    .kv .val {{ font-size: 0.96rem; font-weight: 600; margin-top: 3px; word-break: break-all; }}
    .banner {{
      border-radius: 9px; padding: 13px 15px; margin: 16px 0; font-size: 0.92rem;
    }}
    .banner.warn {{ background: var(--red-bg); border: 1px solid rgba(239,68,68,.4); color: #fecaca; }}
    .banner.ready {{ background: var(--green-bg); border: 1px solid rgba(16,185,129,.38); color: #bbf7d0; }}
    .banner.info {{ background: rgba(59,130,246,.14); border: 1px solid rgba(59,130,246,.38); color: #bfdbfe; }}
    .actions {{ display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 16px; }}
    button {{
      font: inherit; font-weight: 600; font-size: 0.93rem; border-radius: 8px; padding: 10px 18px;
      border: 1px solid transparent; cursor: pointer; transition: background 0.15s, opacity 0.15s;
    }}
    button.primary {{ background: #10b981; color: #052e1f; }}
    button.primary:hover:not(:disabled) {{ background: #34d399; }}
    button.primary:disabled {{
      background: #334155; color: #94a3b8; border-color: #475569;
      cursor: not-allowed; opacity: 0.65;
    }}
    button.secondary {{ background: #1e293b; color: var(--text); border-color: var(--border); }}
    button.secondary:hover:not(:disabled) {{ background: #283548; }}
    select {{
      font: inherit; font-size: 0.9rem; background: var(--panel-alt); color: var(--text);
      border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px;
    }}
    pre.log {{
      background: #070b14; border: 1px solid var(--border); border-radius: 8px;
      padding: 12px 14px; font-size: 0.8rem; color: #cbd5e1; overflow-x: auto;
      max-height: 240px; margin: 14px 0 0; white-space: pre-wrap;
    }}
    .services {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px;
    }}
    a.svc {{
      display: block; background: var(--panel-alt); border: 1px solid var(--border); border-radius: 9px;
      padding: 14px; text-decoration: none; color: var(--text); transition: border-color 0.15s, transform 0.1s;
    }}
    a.svc:hover {{ border-color: var(--blue); transform: translateY(-1px); }}
    a.svc .svc-name {{ font-weight: 600; font-size: 0.96rem; }}
    a.svc .svc-host {{ font-size: 0.8rem; color: var(--muted); margin-top: 3px; word-break: break-all; }}
    code {{ background: #070b14; padding: 2px 6px; border-radius: 4px; font-size: 0.86em; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <h1>Sophon Appliance</h1>
        <span id="mode-badge" class="badge">Loading…</span>
      </div>
      <div class="userbar">
        <span>Signed in as <strong id="user-label">{safe_user}</strong></span>
        <a href="/logout">Sign out</a>
      </div>
    </header>

    <section class="card">
      <h2>Appliance Overview</h2>
      <div class="kv-grid">
        <div class="kv"><div class="label">Domain</div><div class="val" id="kv-domain">—</div></div>
        <div class="kv"><div class="label">Current Appliance IP</div><div class="val" id="kv-current-ip">—</div></div>
        <div class="kv"><div class="label">Kopia Backups</div><div class="val" id="kv-kopia">—</div></div>
        <div class="kv"><div class="label">Restored Snapshot</div><div class="val" id="kv-snapshot">—</div></div>
      </div>
    </section>

    <section id="cutover-card" class="card"{cutover_style}>
      <h2>Production Disaster-Recovery Cutover</h2>
      <div class="kv-grid">
        <div class="kv"><div class="label">Target Production CIDR</div><div class="val" id="kv-target-cidr">—</div></div>
        <div class="kv"><div class="label">Production IP Status</div><div class="val" id="kv-ip-status">—</div></div>
        <div class="kv"><div class="label">Cloudflare Tunnel</div><div class="val" id="kv-tunnel">—</div></div>
      </div>

      <div id="cutover-banner" class="{init_banner_cls}">{init_banner_txt}</div>

      <div class="actions">
        <label for="tunnel-select" style="font-size:0.88rem;color:var(--muted);">Cloudflare tunnel on promotion:</label>
        <select id="tunnel-select">
          <option value="default">Use saved setting</option>
          <option value="enable">Enable Cloudflare tunnel</option>
          <option value="disable">Keep Cloudflare tunnel disabled</option>
        </select>
        <button id="promote-btn" class="primary" type="button" {btn_attrs}>
          Promote to Active Production
        </button>
        <button id="recheck-btn" class="secondary" type="button">
          Re-check Production IP
        </button>
      </div>

      <pre id="activate-log" class="log" style="display:none;"></pre>
    </section>

    <section id="cert-card" class="card">
      <h2>TLS Certificate Management</h2>
      <div class="kv-grid">
        <div class="kv"><div class="label">Live Wildcard Certificate</div><div class="val" id="kv-live-cert">—</div></div>
        <div class="kv"><div class="label">Certificate Disk (SOPHON_CERTS)</div><div class="val" id="kv-cert-disk">—</div></div>
      </div>

      <div id="cert-banner" class="{cert_banner_cls}">{cert_banner_txt}</div>

      <div class="actions">
        <button id="refresh-certs-btn" class="primary" type="button" {cert_btn_attrs}>
          Update Certificates
        </button>
        <button id="recheck-certs-btn" class="secondary" type="button">
          Scan Certificate Disk
        </button>
      </div>

      <pre id="refresh-certs-log" class="log" style="display:none;"></pre>
    </section>

    <section class="card">
      <h2>Core Services</h2>
      <div class="services">
        <a class="svc" href="https://git.{safe_domain}" target="_blank" rel="noopener">
          <div class="svc-name">Gitea</div><div class="svc-host">git.{safe_domain}</div>
        </a>
        <a class="svc" href="https://nexus.{safe_domain}" target="_blank" rel="noopener">
          <div class="svc-name">Sonatype Nexus</div><div class="svc-host">nexus.{safe_domain}</div>
        </a>
        <a class="svc" href="https://arcane.{safe_domain}" target="_blank" rel="noopener">
          <div class="svc-name">Arcane</div><div class="svc-host">arcane.{safe_domain}</div>
        </a>
        <a class="svc" href="https://auth.{safe_domain}" target="_blank" rel="noopener">
          <div class="svc-name">Keycloak SSO</div><div class="svc-host">auth.{safe_domain}</div>
        </a>
        <a class="svc" href="https://kopia.{safe_domain}" target="_blank" rel="noopener">
          <div class="svc-name">Kopia Backups</div><div class="svc-host">kopia.{safe_domain}</div>
        </a>
      </div>
    </section>
  </div>

  <script>
    const CSRF_TOKEN = {csrf_json};
    let currentState = {initial_json};
    let switchingNetwork = false;
    let refreshingCerts = false;

    function renderCertCard(data) {{
      const liveCertEl = document.getElementById('kv-live-cert');
      liveCertEl.textContent = data.live_cert_expires_at
        ? `Valid until ${{data.live_cert_expires_at}}`
        : 'Active (Traefik managed)';

      const disk = data.cert_disk || {{}};
      const diskEl = document.getElementById('kv-cert-disk');
      if (!disk.present) {{
        diskEl.textContent = 'Not attached';
      }} else if (disk.valid) {{
        diskEl.innerHTML = `<span class="badge active">Ready — Valid until ${{disk.expires_at || 'verified'}}</span>`;
      }} else {{
        diskEl.innerHTML = '<span class="badge conflict">Invalid Certificate Disk</span>';
      }}

      const ref = data.cert_refresh || {{}};
      if (ref.state !== 'running') {{
        refreshingCerts = false;
      }}
      const certBanner = document.getElementById('cert-banner');
      if (ref.state === 'running' || refreshingCerts) {{
        certBanner.className = 'banner info';
        certBanner.textContent = ref.message || 'Updating Traefik & OpenLDAP certificates, snapshotting Kopia, and ejecting ISO…';
      }} else if (ref.state === 'failed') {{
        certBanner.className = 'banner warn';
        certBanner.textContent = `Last certificate update failed: ${{ref.message || 'inspect log below.'}}`;
      }} else if (data.refresh_certs_enabled) {{
        certBanner.className = 'banner ready';
        const copyNote = data.standby
          ? 'install the renewed certificate into Traefik & OpenLDAP, update host registry trust, and eject SOPHON_CERTS.'
          : 'install the renewed certificate into Traefik & OpenLDAP, snapshot to Kopia, and eject SOPHON_CERTS.';
        certBanner.textContent = `Certificate disk validated (expires ${{disk.expires_at || 'soon'}}). Clicking Update Certificates will ${{copyNote}}`;
      }} else if (ref.state === 'succeeded' && !disk.present) {{
        certBanner.className = 'banner ready';
        certBanner.textContent = ref.message || 'Wildcard TLS certificate updated, backed up to Kopia, and certificate ISO ejected.';
      }} else if (disk.present && !disk.valid) {{
        certBanner.className = 'banner warn';
        certBanner.textContent = data.refresh_certs_disabled_reason || 'Attached certificate disk failed validation.';
      }} else {{
        certBanner.className = 'banner info';
        certBanner.textContent = data.refresh_certs_disabled_reason || "Attach sophon-secrets.iso with 'sh sophon-mount-secrets.sh' to update certificates.";
      }}

      const certBtn = document.getElementById('refresh-certs-btn');
      const certEnabled = Boolean(data.refresh_certs_enabled) && !refreshingCerts && !switchingNetwork;
      certBtn.disabled = !certEnabled;
      certBtn.setAttribute('aria-disabled', certEnabled ? 'false' : 'true');

      const certLog = document.getElementById('refresh-certs-log');
      if (data.refresh_certs_log) {{
        certLog.style.display = 'block';
        certLog.textContent = data.refresh_certs_log;
      }} else {{
        certLog.style.display = 'none';
      }}
    }}

    function render(data) {{
      currentState = data;
      const badge = document.getElementById('mode-badge');
      if (data.standby) {{
        badge.className = 'badge standby';
        badge.textContent = 'Safe Standby (Read-Only Drill)';
      }} else {{
        badge.className = 'badge active';
        badge.textContent = 'Active Production';
      }}

      document.getElementById('kv-domain').textContent = data.domain || '—';
      document.getElementById('kv-current-ip').textContent = data.current_ip || '—';
      document.getElementById('kv-kopia').textContent = data.standby
        ? 'Read-Only Standby (scheduled backups paused)'
        : `Active Scheduled (${{data.desired_snapshot_interval || '6h'}})`;
      document.getElementById('kv-snapshot').textContent = data.restored_snapshot || (data.standby ? 'Latest' : 'Live');

      renderCertCard(data);

      const cutoverCard = document.getElementById('cutover-card');
      const act = data.activation || {{}};
      if (!data.standby && act.state !== 'running') {{
        cutoverCard.style.display = 'none';
        return;
      }}
      cutoverCard.style.display = 'block';

      document.getElementById('kv-target-cidr').textContent = data.target_cidr || (data.current_ip ? `${{data.current_ip}} (in-place)` : '—');
      const ipStatusEl = document.getElementById('kv-ip-status');
      if (!data.needs_ip_switch) {{
        ipStatusEl.textContent = 'In-place (already using production IP)';
      }} else if (data.target_ip_occupied) {{
        ipStatusEl.innerHTML = '<span class="badge conflict">Occupied — Production VM Active</span>';
      }} else {{
        ipStatusEl.innerHTML = '<span class="badge active">Available — Ready for Cutover</span>';
      }}

      document.getElementById('kv-tunnel').textContent = data.desired_cf_tunnel_enabled
        ? `Enabled on promotion (${{data.desired_cf_tunnel_name || 'sophon'}})`
        : 'Disabled (LAN / Air-Gapped)';

      const banner = document.getElementById('cutover-banner');
      const promoteBtn = document.getElementById('promote-btn');

      if (act.state === 'running' || switchingNetwork) {{
        banner.className = 'banner info';
        const ipNote = data.needs_ip_switch
          ? ` Switching appliance NIC from ${{data.current_ip}} to ${{data.target_ip}} — if you pointed DNS or /etc/hosts at ${{data.current_ip}} for testing, update it to ${{data.target_ip}} to stay connected.`
          : '';
        banner.textContent = (act.message || 'Promoting standby appliance to active production…') + ipNote;
      }} else if (act.state === 'failed') {{
        banner.className = 'banner warn';
        banner.textContent = `Last promotion attempt failed: ${{act.message || 'inspect activation log below.'}}`;
      }} else if (!data.promote_enabled) {{
        banner.className = 'banner warn';
        banner.textContent = data.promote_disabled_reason;
      }} else {{
        banner.className = 'banner ready';
        const moveMsg = data.needs_ip_switch
          ? `Target production IP ${{data.target_ip}} is free. Promoting will move this VM from ${{data.current_ip}} to ${{data.target_cidr}}, enable scheduled Kopia backups, and activate production routing.`
          : 'Ready to promote this standby appliance in place and enable scheduled Kopia backups.';
        banner.textContent = moveMsg;
      }}

      const enabled = Boolean(data.promote_enabled) && !switchingNetwork;
      promoteBtn.disabled = !enabled;
      promoteBtn.setAttribute('aria-disabled', enabled ? 'false' : 'true');

      const logEl = document.getElementById('activate-log');
      if (data.activate_log) {{
        logEl.style.display = 'block';
        logEl.textContent = data.activate_log;
      }} else {{
        logEl.style.display = 'none';
      }}
    }}

    async function pollStatus(forceProbe = false) {{
      try {{
        const res = await fetch(forceProbe ? '/api/recheck-ip' : '/api/status', {{
          method: forceProbe ? 'POST' : 'GET',
          headers: forceProbe ? {{ 'X-Sophon-CSRF': CSRF_TOKEN }} : {{}},
          credentials: 'same-origin'
        }});
        if (res.status === 401) {{
          window.location.href = '/login';
          return;
        }}
        if (res.ok) {{
          const payload = await res.json();
          if (!payload.standby) {{
            switchingNetwork = false;
          }}
          render(payload);
        }}
      }} catch (_err) {{
        if (switchingNetwork) {{
          const banner = document.getElementById('cutover-banner');
          banner.className = 'banner info';
          banner.textContent = `Appliance NIC is transitioning to production IP ${{currentState.target_ip || ''}} and running 5-activate.yml. Ensure your client resolves ${{currentState.domain}} to ${{currentState.target_ip || 'the production IP'}} — waiting to reconnect…`;
        }} else if (refreshingCerts) {{
          const certBanner = document.getElementById('cert-banner');
          certBanner.className = 'banner info';
          certBanner.textContent = 'Traefik is reloading the new wildcard TLS certificate — reconnecting automatically…';
        }}
      }}
    }}

    document.getElementById('recheck-btn').addEventListener('click', () => pollStatus(true));
    document.getElementById('recheck-certs-btn').addEventListener('click', () => pollStatus(true));

    document.getElementById('refresh-certs-btn').addEventListener('click', async () => {{
      if (!currentState.refresh_certs_enabled) return;
      const disk = currentState.cert_disk || {{}};
      const ok = window.confirm(
        `Update wildcard TLS certificates for ${{currentState.domain}} now?\\n\\nThis will install the certificate valid until ${{disk.expires_at || 'verified'}}, reload Traefik and OpenLDAP, snapshot Kopia, and eject the SOPHON_CERTS disk.`
      );
      if (!ok) return;

      const btn = document.getElementById('refresh-certs-btn');
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
      refreshingCerts = true;
      try {{
        const res = await fetch('/api/refresh-certs', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-Sophon-CSRF': CSRF_TOKEN
          }},
          credentials: 'same-origin',
          body: JSON.stringify({{}})
        }});
        const payload = await res.json();
        if (!res.ok) {{
          refreshingCerts = false;
          if (payload.readiness) render(payload.readiness);
          window.alert(payload.error || 'Certificate refresh preflight rejected the request.');
          return;
        }}
        if (payload.readiness) render(payload.readiness);
      }} catch (_err) {{
        // Traefik may reload within milliseconds of queueing refresh-certs; next poll reconnects cleanly.
      }}
    }});

    document.getElementById('promote-btn').addEventListener('click', async () => {{
      if (!currentState.promote_enabled) return;
      const tunnelMode = document.getElementById('tunnel-select').value;
      const ipDesc = currentState.needs_ip_switch
        ? `switch IP ${{currentState.current_ip}} -> ${{currentState.target_cidr}}, `
        : '';
      const ok = window.confirm(
        `Promote ${{currentState.domain}} to ACTIVE PRODUCTION now?\\n\\nThis will ${{ipDesc}}enable scheduled Kopia write backups (${{currentState.desired_snapshot_interval || '6h'}}), and reconcile Cloudflare tunnel settings.`
      );
      if (!ok) return;

      const btn = document.getElementById('promote-btn');
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
      try {{
        const res = await fetch('/api/promote', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-Sophon-CSRF': CSRF_TOKEN
          }},
          credentials: 'same-origin',
          body: JSON.stringify({{ tunnel_mode: tunnelMode }})
        }});
        const payload = await res.json();
        if (!res.ok) {{
          if (payload.readiness) render(payload.readiness);
          window.alert(payload.error || 'Promotion preflight rejected the request.');
          return;
        }}
        if (currentState.needs_ip_switch) {{
          switchingNetwork = true;
        }}
        if (payload.readiness) render(payload.readiness);
      }} catch (err) {{
        switchingNetwork = Boolean(currentState.needs_ip_switch);
      }}
    }});

    render(currentState);
    setInterval(() => pollStatus(false), 5000);
  </script>
</body>
</html>"""


SECURITY_HEADERS = (
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'),
    ('Cache-Control', 'no-store'),
    (
        'Content-Security-Policy',
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
    ),
)


def make_handler(config: StatusConfig):
    class StatusRequestHandler(BaseHTTPRequestHandler):
        server_version = 'SophonStatus/1.0'

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write(f'[sophon-status] {self.address_string()} - {fmt % args}\n')

        def _send_common_headers(self) -> None:
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)

        def _send_json(self, status_code: int, payload: dict, extra_headers: list[tuple[str, str]] | None = None) -> None:
            raw = json.dumps(payload).encode('utf-8')
            self.send_response(status_code)
            self._send_common_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            for k, v in extra_headers or ():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, status_code: int, body: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
            raw = body.encode('utf-8')
            self.send_response(status_code)
            self._send_common_headers()
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            for k, v in extra_headers or ():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _redirect(self, location: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
            self.send_response(302)
            self._send_common_headers()
            self.send_header('Location', location)
            for k, v in extra_headers or ():
                self.send_header(k, v)
            self.end_headers()

        def _request_host(self) -> str:
            raw_host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '').split(',')[0].strip()
            host = raw_host.split(':')[0].lower().rstrip('.')
            if host in config.allowed_hosts:
                return host
            return config.domain

        def _parse_cookies(self) -> dict[str, str]:
            jar = cookies.SimpleCookie()
            raw = self.headers.get('Cookie', '')
            if raw:
                jar.load(raw)
            return {k: morsel.value for k, morsel in jar.items()}

        def _get_session(self) -> dict | None:
            token = self._parse_cookies().get(SESSION_COOKIE, '')
            return verify_cookie(token, config.session_secret)

        def _build_status_payload(self, *, force_probe: bool = False) -> dict:
            app_state = config.request_host_probe(force=force_probe)
            act_status = config.read_activate_status()
            ref_status = config.read_refresh_certs_status()
            readiness = evaluate_cutover_readiness(
                app_state,
                act_status,
                ref_status,
                tcp_probe_fn=config.tcp_probe_fn,
            )
            readiness['activate_log'] = config.read_activate_log_tail()
            readiness['refresh_certs_log'] = config.read_refresh_certs_log_tail()
            return readiness

        def _verify_csrf(self, session: dict) -> bool:
            supplied = self.headers.get('X-Sophon-CSRF', '')
            expected = str(session.get('csrf') or '')
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                return False
            origin = self.headers.get('Origin', '')
            if origin:
                parsed = urlparse(origin)
                if parsed.hostname and parsed.hostname.lower().rstrip('.') not in config.allowed_hosts:
                    return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == '/healthz':
                self._send_json(200, {'ok': True})
                return

            host = self._request_host()
            callback_uri = f'https://{host}/oauth2/callback'

            if path == '/login':
                verifier, challenge = make_pkce_pair()
                state = _b64url_encode(secrets.token_bytes(24))
                nonce = _b64url_encode(secrets.token_bytes(24))
                login_cookie = sign_cookie(
                    {'state': state, 'nonce': nonce, 'verifier': verifier, 'exp': time.time() + 600},
                    config.session_secret,
                )
                auth_query = urlencode({
                    'client_id': config.oidc_client_id,
                    'response_type': 'code',
                    'scope': 'openid profile email groups',
                    'redirect_uri': callback_uri,
                    'state': state,
                    'nonce': nonce,
                    'code_challenge': challenge,
                    'code_challenge_method': 'S256',
                })
                self._redirect(
                    f'{config.issuer}/protocol/openid-connect/auth?{auth_query}',
                    extra_headers=[
                        ('Set-Cookie', f'{LOGIN_COOKIE}={login_cookie}; Max-Age=600; Path=/; HttpOnly; Secure; SameSite=Lax'),
                    ],
                )
                return

            if path == '/oauth2/callback':
                params = parse_qs(parsed.query)
                code = (params.get('code') or [''])[0]
                state = (params.get('state') or [''])[0]
                login_state = verify_cookie(self._parse_cookies().get(LOGIN_COOKIE, ''), config.session_secret)
                clear_login = f'{LOGIN_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax'
                if not code or not login_state or not hmac.compare_digest(state, str(login_state.get('state', ''))):
                    self._send_html(400, '<h1>Invalid or expired login state</h1><p><a href="/login">Try again</a></p>',
                                    extra_headers=[('Set-Cookie', clear_login)])
                    return
                try:
                    token_res = config.exchange_code(code, callback_uri, str(login_state['verifier']))
                    id_token = str(token_res.get('id_token') or '')
                    try:
                        claims = verify_oidc_id_token(
                            id_token,
                            config.fetch_jwks(),
                            expected_issuer=config.issuer,
                            expected_client_id=config.oidc_client_id,
                            expected_nonce=str(login_state['nonce']),
                        )
                    except ValueError:
                        claims = verify_oidc_id_token(
                            id_token,
                            config.fetch_jwks(force=True),
                            expected_issuer=config.issuer,
                            expected_client_id=config.oidc_client_id,
                            expected_nonce=str(login_state['nonce']),
                        )
                except Exception as exc:
                    sys.stderr.write(f'[sophon-status] OIDC callback failed: {exc}\n')
                    self._send_html(502, '<h1>Authentication exchange failed</h1><p><a href="/login">Retry sign-in</a></p>',
                                    extra_headers=[('Set-Cookie', clear_login)])
                    return

                username = str(claims.get('preferred_username') or claims.get('email') or claims.get('sub') or 'user')
                if not user_has_admin_group(claims, config.admin_group):
                    self._send_html(
                        403,
                        render_forbidden_html(username, config.admin_group),
                        extra_headers=[('Set-Cookie', clear_login)],
                    )
                    return

                session_cookie = sign_cookie(
                    {
                        'user': username,
                        'csrf': _b64url_encode(secrets.token_bytes(24)),
                        'exp': time.time() + 8 * 3600,
                    },
                    config.session_secret,
                )
                self._redirect(
                    '/',
                    extra_headers=[
                        ('Set-Cookie', clear_login),
                        ('Set-Cookie', f'{SESSION_COOKIE}={session_cookie}; Max-Age=28800; Path=/; HttpOnly; Secure; SameSite=Lax'),
                    ],
                )
                return

            if path == '/logout':
                clear_sess = f'{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax'
                logout_url = f'{config.issuer}/protocol/openid-connect/logout?' + urlencode({
                    'client_id': config.oidc_client_id,
                    'post_logout_redirect_uri': f'https://{host}/',
                })
                self._redirect(logout_url, extra_headers=[('Set-Cookie', clear_sess)])
                return

            session = self._get_session()
            if not session:
                if path.startswith('/api/'):
                    self._send_json(401, {'error': 'Authentication required.'})
                else:
                    self._redirect('/login')
                return

            if path == '/api/status':
                self._send_json(200, self._build_status_payload(force_probe=False))
                return

            if path == '/':
                status_payload = self._build_status_payload(force_probe=True)
                self._send_html(
                    200,
                    render_dashboard_html(
                        domain=config.domain,
                        username=str(session.get('user') or 'admin'),
                        csrf_token=str(session.get('csrf') or ''),
                        initial_status=status_payload,
                    ),
                )
                return

            self._send_json(404, {'error': 'Not found.'})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            session = self._get_session()
            if not session:
                self._send_json(401, {'error': 'Authentication required.'})
                return
            if not self._verify_csrf(session):
                self._send_json(403, {'error': 'Invalid CSRF token.'})
                return

            if path in ('/api/recheck-ip', '/api/recheck-certs'):
                self._send_json(200, self._build_status_payload(force_probe=True))
                return

            if path == '/api/refresh-certs':
                readiness = self._build_status_payload(force_probe=True)
                if not readiness['refresh_certs_enabled']:
                    self._send_json(
                        409,
                        {
                            'error': readiness['refresh_certs_disabled_reason'] or 'Certificate disk is not ready.',
                            'readiness': readiness,
                        },
                    )
                    return
                requested_by = str(session.get('user') or 'admin')
                config.queue_refresh_certs(requested_by=requested_by)
                readiness['cert_refresh'] = {
                    'state': 'running',
                    'message': 'Certificate update queued on CoreOS host…',
                    'requested_by': requested_by,
                    'updated_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'exit_code': 0,
                }
                readiness['refresh_certs_enabled'] = False
                readiness['refresh_certs_disabled_reason'] = 'Certificate update is currently in progress.'
                self._send_json(202, {'ok': True, 'readiness': readiness})
                return

            if path == '/api/promote':
                length = int(self.headers.get('Content-Length') or '0')
                if length > 8192:
                    self._send_json(413, {'error': 'Request body too large.'})
                    return
                raw_body = self.rfile.read(length) if length > 0 else b'{}'
                try:
                    req_data = json.loads(raw_body.decode('utf-8') or '{}')
                except ValueError:
                    self._send_json(400, {'error': 'Invalid JSON payload.'})
                    return
                tunnel_mode = str(req_data.get('tunnel_mode') or 'default')
                if tunnel_mode not in ALLOWED_TUNNEL_MODES:
                    self._send_json(400, {'error': 'Invalid tunnel_mode.'})
                    return

                # Always run a fresh network occupancy probe immediately before queuing host activation.
                readiness = self._build_status_payload(force_probe=True)
                if not readiness['promote_enabled']:
                    self._send_json(
                        409,
                        {
                            'error': readiness['promote_disabled_reason'] or 'Appliance is not eligible for promotion.',
                            'readiness': readiness,
                        },
                    )
                    return

                config.queue_activation(
                    requested_by=str(session.get('user') or 'admin'),
                    tunnel_mode=tunnel_mode,
                )
                readiness['activation'] = {
                    'state': 'running',
                    'message': 'Promotion queued on CoreOS host…',
                    'requested_by': str(session.get('user') or 'admin'),
                    'updated_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'exit_code': 0,
                }
                readiness['promote_enabled'] = False
                readiness['promote_disabled_reason'] = 'Production cutover is currently in progress.'
                self._send_json(202, {'ok': True, 'readiness': readiness})
                return

            self._send_json(404, {'error': 'Not found.'})

    return StatusRequestHandler


def main(argv=None) -> int:
    domain = os.environ.get('SOPHON_STATUS_DOMAIN', '').strip()
    if not domain:
        print('SOPHON_STATUS_DOMAIN is required.', file=sys.stderr)
        return 2
    secret_raw = os.environ.get('SOPHON_STATUS_SESSION_SECRET', '').strip()
    if len(secret_raw) < 32:
        print('SOPHON_STATUS_SESSION_SECRET must be at least 32 characters.', file=sys.stderr)
        return 2

    port = int(os.environ.get('SOPHON_STATUS_PORT', '8080'))
    bind_host = os.environ.get('SOPHON_STATUS_BIND', '0.0.0.0')
    config = StatusConfig(
        domain=domain,
        oidc_realm=os.environ.get('SOPHON_STATUS_OIDC_REALM', 'sophon'),
        oidc_client_id=os.environ.get('SOPHON_STATUS_OIDC_CLIENT_ID', 'sophon-status'),
        admin_group=os.environ.get('SOPHON_STATUS_ADMIN_GROUP', 'arcane-admins'),
        session_secret=secret_raw.encode('utf-8'),
        control_dir=Path(os.environ.get('SOPHON_STATUS_CONTROL_DIR', '/control')),
        verify_oidc_tls=os.environ.get('SOPHON_STATUS_VERIFY_TLS', 'false').lower() in ('1', 'true', 'yes'),
    )
    server = ThreadingHTTPServer((bind_host, port), make_handler(config))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


if __name__ == '__main__':
    sys.exit(main())
