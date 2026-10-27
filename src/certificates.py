"""Issue, validate and transport Traefik's private ACME certificate store."""

import argparse
import base64
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError


SERVICES = ('traefik', 'portainer', 'dns', 'ldap', 'auth', 'git')
MAX_PAYLOAD = 2 * 1024 * 1024


def validate(source, domain):
    return validate_data(Path(source).read_bytes(), domain)


def validate_data(data, domain):
    domain = domain.lower().rstrip('.')
    payload = json.loads(data)
    if (not isinstance(payload, dict) or set(payload) != {'letsencrypt'}
            or not isinstance(payload['letsencrypt'], dict)
            or set(payload['letsencrypt']) - {'Account', 'Certificates'}):
        raise ValueError('Use a Traefik acme.json containing only the letsencrypt resolver, not other settings or tokens.')
    account = payload['letsencrypt'].get('Account')
    if account is not None and (not isinstance(account, dict)
                              or set(account) - {'Email', 'Registration', 'PrivateKey', 'KeyType'}):
        raise ValueError('Unexpected ACME account settings; do not include API tokens in the handoff.')
    entries = payload['letsencrypt'].get('Certificates')
    if not isinstance(entries, list):
        raise ValueError('The ACME store must contain its issued Certificates, not only an account key.')
    records = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'domain', 'certificate', 'key', 'Store'}:
            raise ValueError('Unexpected data in the certificate store; only ACME identity state may cross the boundary.')
        if (not isinstance(entry.get('domain'), dict) or set(entry['domain']) - {'main', 'sans'}
                or not isinstance(entry['domain'].get('main'), str)
                or not all(isinstance(entry.get(field), str) for field in ('certificate', 'key'))):
            raise ValueError('Unexpected certificate domain settings.')
        name = entry['domain']['main']
        if name in records:
            raise ValueError(f'Duplicate certificate for {name}.')
        records[name] = entry
    roots = [x509.load_der_x509_certificate(der)
             for der in ssl.create_default_context().get_ca_certs(binary_form=True)]
    store = Store(roots)
    now = datetime.now(timezone.utc)
    expiries = {}
    for service in SERVICES:
        name = f'{service}.{domain}'
        if name not in records:
            raise ValueError(f'Missing pre-issued certificate for {name}. Issue it on the connected laptop first.')
        entry = records[name]
        chain = x509.load_pem_x509_certificates(base64.b64decode(entry['certificate'], validate=True))
        if not chain:
            raise ValueError(f'Missing PEM certificate chain for {name}.')
        try:
            key = serialization.load_pem_private_key(base64.b64decode(entry['key'], validate=True), password=None)
        except UnsupportedAlgorithm as error:
            raise ValueError(f'Unsupported private key algorithm for {name}.') from error
        encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        if key.public_key().public_bytes(encoding, form) != chain[0].public_key().public_bytes(encoding, form):
            raise ValueError(f'The certificate and private key for {name} do not match.')
        try:
            PolicyBuilder().store(store).time(now).build_server_verifier(x509.DNSName(name)).verify(chain[0], chain[1:])
        except VerificationError as error:
            raise ValueError(f'{name} does not have a trusted, currently valid certificate: {error}') from error
        expiries[name] = chain[0].not_valid_after_utc.isoformat()
    return expiries


def decode_payload(encoded, domain):
    if not isinstance(encoded, str) or len(encoded) > MAX_PAYLOAD:
        raise ValueError('Certificate payload exceeds the 2 MiB limit.')
    try:
        data = base64.b64decode(''.join(encoded.split()), validate=True)
        validate_data(data, domain)
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError('Invalid certificate payload: supply base64 of a complete, trusted acme.json for this domain.') from error
    return data


def import_payload(encoded, domain, destination):
    data = decode_payload(encoded, domain)
    destination = Path(destination)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError('Certificate identity paths must not be symlinks.')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.acme-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ready_to_export(source, domain):
    if not source.exists():
        return False
    try:
        expiries = validate(source, domain)
    except (ValueError, KeyError, TypeError, UnicodeError):
        return False
    now = datetime.now(timezone.utc)
    return all((datetime.fromisoformat(value) - now).total_seconds() > 30 * 86400 for value in expiries.values())


def issue(source, domain, email, token, timeout):
    directory = source.parent
    dynamic = directory / 'routers.json'
    dynamic.write_text(json.dumps({'http': {'routers': {
        service: {'rule': f'Host(`{service}.{domain}`)', 'entryPoints': ['certificates'],
                  'service': 'noop@internal', 'tls': {'certResolver': 'letsencrypt'}}
        for service in SERVICES}}}))
    dynamic.chmod(0o600)
    environment = dict(os.environ, CF_DNS_API_TOKEN=token)
    # The token is inherited through the environment, never included in argv or the handoff.
    environment.pop('CF_DNS_API_TOKEN_FILE', None)
    environment.pop('CF_API_KEY', None)
    environment.pop('CF_API_EMAIL', None)
    environment.pop('CF_API_KEY_FILE', None)
    environment.pop('CF_API_EMAIL_FILE', None)
    environment.pop('CF_ZONE_API_TOKEN', None)
    environment.pop('CF_ZONE_API_TOKEN_FILE', None)
    arguments = [
        'traefik', '--log.level=INFO', '--entrypoints.certificates.address=127.0.0.1:0',
        f'--providers.file.filename={directory}/routers.json',
        f'--certificatesresolvers.letsencrypt.acme.email={email}',
        f'--certificatesresolvers.letsencrypt.acme.storage={directory}/acme.json',
        '--certificatesresolvers.letsencrypt.acme.dnschallenge.provider=cloudflare',
    ]
    log_path = directory / 'issuer.log'
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as log:
        os.fchmod(log.fileno(), 0o600)
        process = subprocess.Popen(arguments, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError(f'Certificate issuer exited. Inspect the private log: {log_path}')
                if ready_to_export(source, domain):
                    return
                time.sleep(2)
            raise ValueError(f'Certificate issuance timed out. Inspect the private log: {log_path}')
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def issue_main(argv):
    parser = argparse.ArgumentParser(description='Issue/renew certificates on a connected machine; stdout is one secret base64 payload.')
    parser.add_argument('--domain')
    parser.add_argument('--email')
    default_state = Path(os.environ.get('SOPHON_STATE_DIR') or Path.home() / '.local/state/sophon') / 'certificates'
    parser.add_argument('--state-dir', type=Path, default=default_state)
    parser.add_argument('--timeout', type=int, default=600)
    options = parser.parse_args(argv)

    def prompt(label):
        print(label, end='', file=sys.stderr, flush=True)
        return input().strip()

    try:
        domain = (options.domain or os.environ.get('SOPHON_DOMAIN') or prompt('Domain: ')).lower().rstrip('.')
        if (not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', domain)
                or '.' not in domain or domain.replace('.', '').isdigit()
                or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                       for label in domain.split('.'))):
            raise ValueError('Supply a public DNS domain, without a URL, wildcard or path.')
        if not 1 <= options.timeout <= 3600:
            raise ValueError('Timeout must be between 1 and 3600 seconds.')
        directory = options.state_dir.expanduser().resolve() / domain
        if directory.is_symlink():
            raise ValueError('The private certificate directory must not be a symlink.')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_mode & 0o077:
            raise ValueError(f'Certificate directory must have mode 0700: {directory}')
        source = directory / 'acme.json'
        if source.is_symlink():
            raise ValueError('The private ACME store must not be a symlink.')
        if source.exists():
            source.chmod(0o600)
        if not ready_to_export(source, domain):
            email = options.email or os.environ.get('SOPHON_ACME_EMAIL') or prompt('ACME contact email: ')
            if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
                raise ValueError('Supply a valid ACME contact email.')
            token = os.environ.get('CF_DNS_API_TOKEN') or getpass.getpass('Cloudflare DNS API token: ', stream=sys.stderr)
            if not token.strip():
                raise ValueError('A Cloudflare API token is required for issuance.')
            if not shutil.which('traefik'):
                raise ValueError('Run this through ./sophon certificates issue (the traefik binary is in the tools image).')
            print(f'Issuing/renewing certificates for {domain}. Private state: {directory}', file=sys.stderr)
            issue(source, domain, email, token, options.timeout)
        source.chmod(0o600)
        data = source.read_bytes()
        validate_data(data, domain)
        encoded = base64.b64encode(data).decode()
        if len(encoded) > MAX_PAYLOAD:
            raise ValueError('The ACME store exceeds the 2 MiB import limit; use a dedicated store for this domain.')
        print(f'Exporting complete certificate store. Keep this payload private. State: {directory}', file=sys.stderr)
        print(encoded)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Certificate issuance failed: {error}\n')
    except (EOFError, KeyboardInterrupt):
        parser.exit(130, 'Certificate issuance cancelled; no payload exported.\n')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['issue']:
        return issue_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, epilog='Connected issuance/renewal: python3 certificates.py issue')
    parser.add_argument('command', choices=['check'], help='Validate an acme.json file')
    parser.add_argument('--source', type=Path, required=True, help='Traefik-format acme.json')
    parser.add_argument('--domain', required=True)
    options = parser.parse_args(argv)
    try:
        expiries = validate(options.source, options.domain)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Certificate check failed: {error}\n')
    print(json.dumps({'certificate_expiries': expiries}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
