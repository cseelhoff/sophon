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
MAX_FILE = 2 * 1024 * 1024
KEY_TYPE = 'EC256'
# The secret disk `issue` writes and the InfraVM reads: one ISO holding acme.json (it contains a private key).
CERTIFICATE_LABEL = 'SOPHON_CERTS'
CERTIFICATE_ISO = Path(__file__).resolve().parents[1] / 'artifacts/secrets/sophon-certificates.iso'


def validate(source, domain):
    return validate_data(Path(source).read_bytes(), domain)


def normalize_domain(domain):
    return domain.lower().rstrip('.')


def validate_data(data, domain):
    """Check the store holds one trusted certificate for `domain` and `*.domain` covering every service name."""
    domain = normalize_domain(domain)
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
                or not all(isinstance(name, str) for name in entry['domain'].get('sans', []))
                or not all(isinstance(entry.get(field), str) for field in ('certificate', 'key'))):
            raise ValueError('Unexpected certificate domain settings.')
        name = normalize_domain(entry['domain']['main'])
        if name in records:
            raise ValueError(f'Duplicate certificate for {name}.')
        records[name] = entry
    # One certificate named after the domain itself: the certs dumper and OpenLDAP find it by that name.
    entry = records.get(domain)
    if entry is None:
        raise ValueError(f'Missing the pre-issued certificate for {domain} and *.{domain}. Issue it on the connected laptop first.')
    listed = {normalize_domain(name) for name in entry['domain'].get('sans', [])} | {domain}
    if f'*.{domain}' not in listed:
        raise ValueError(f'The certificate for {domain} must also list *.{domain}.')
    roots = [x509.load_der_x509_certificate(der)
             for der in ssl.create_default_context().get_ca_certs(binary_form=True)]
    store = Store(roots)
    now = datetime.now(timezone.utc)
    chain = x509.load_pem_x509_certificates(base64.b64decode(entry['certificate'], validate=True))
    if not chain:
        raise ValueError(f'Missing PEM certificate chain for {domain}.')
    try:
        key = serialization.load_pem_private_key(base64.b64decode(entry['key'], validate=True), password=None)
    except UnsupportedAlgorithm as error:
        raise ValueError(f'Unsupported private key algorithm for {domain}.') from error
    encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(encoding, form) != chain[0].public_key().public_bytes(encoding, form):
        raise ValueError(f'The certificate and private key for {domain} do not match.')
    expiries = {}
    for name in (domain, *(f'{service}.{domain}' for service in SERVICES)):
        try:
            PolicyBuilder().store(store).time(now).build_server_verifier(x509.DNSName(name)).verify(chain[0], chain[1:])
        except VerificationError as error:
            raise ValueError(f'{name} does not have a trusted, currently valid certificate: {error}') from error
        expiries[name] = chain[0].not_valid_after_utc.isoformat()
    return expiries


def minimal_store(data, domain):
    """The smallest store Traefik needs offline: the domain's certificate (full chain) and key, no ACME account."""
    domain = normalize_domain(domain)
    entry = next(entry for entry in json.loads(data)['letsencrypt']['Certificates']
                 if normalize_domain(entry['domain']['main']) == domain)
    kept = {'domain': {'main': entry['domain']['main'], 'sans': entry['domain'].get('sans', [])},
            'certificate': entry['certificate'], 'key': entry['key'], 'Store': entry.get('Store') or 'default'}
    return json.dumps({'letsencrypt': {'Certificates': [kept]}}, separators=(',', ':')).encode()


def import_file(source, domain, destination):
    """Validate a certificate file (from the secret disk) and store it privately for Traefik to start from."""
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'Certificate file not found: {source}')
    if source.stat().st_size > MAX_FILE:
        raise ValueError('The certificate file exceeds the 2 MiB limit.')
    try:
        data = source.read_bytes()
        validate_data(data, domain)
        data = minimal_store(data, domain)
    except (ValueError, KeyError, TypeError, UnicodeError, UnsupportedAlgorithm) as error:
        raise ValueError(f'Invalid certificate file {source}: {error}') from error
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


def write_certificate_iso(data, output):
    """Write the secret disk: a small ISO (label SOPHON_CERTS) holding acme.json, mode 0600 on this machine."""
    if not shutil.which('xorriso'):
        raise ValueError('xorriso is missing; run this through the sophon-tools container.')
    output = Path(os.path.abspath(output))
    if output.is_symlink():
        raise ValueError('The certificate ISO path must not be a symlink.')
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='sophon-certs-') as work:
        contents = Path(work) / 'contents'
        contents.mkdir()
        (contents / 'acme.json').write_bytes(data)
        image = Path(work) / 'certificates.iso'
        subprocess.run(['xorriso', '-as', 'mkisofs', '-R', '-J', '-iso-level', '3', '-V', CERTIFICATE_LABEL,
                        '-o', str(image), str(contents)], check=True, capture_output=True)
        descriptor, partial = tempfile.mkstemp(prefix='.sophon-certs-', suffix='.iso', dir=output.parent)
        try:
            with os.fdopen(descriptor, 'wb') as target, image.open('rb') as source:
                os.fchmod(target.fileno(), 0o600)
                shutil.copyfileobj(source, target)
            os.replace(partial, output)
        finally:
            Path(partial).unlink(missing_ok=True)
    return output


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
    # One wildcard certificate (the domain and *.domain) covers every service name.
    dynamic.write_text(json.dumps({'http': {'routers': {'wildcard': {
        'rule': f'Host(`traefik.{domain}`)', 'entryPoints': ['certificates'], 'service': 'noop@internal',
        'tls': {'certResolver': 'letsencrypt', 'domains': [{'main': domain, 'sans': [f'*.{domain}']}]}}}}}))
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
        f'--certificatesresolvers.letsencrypt.acme.keyType={KEY_TYPE}',
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
    parser = argparse.ArgumentParser(description='Issue/renew the wildcard certificate on a connected machine and write '
                                     'it to a secret ISO for the InfraVM.')
    parser.add_argument('--domain')
    parser.add_argument('--email')
    default_state = Path(os.environ.get('SOPHON_STATE_DIR') or Path.home() / '.local/state/sophon') / 'certificates'
    parser.add_argument('--state-dir', type=Path, default=default_state)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--output-iso', type=Path, default=CERTIFICATE_ISO,
                        help=f'secret ISO to write (label {CERTIFICATE_LABEL}; default: %(default)s)')
    parser.add_argument('--output-file', type=Path, help='also write the acme.json itself to this file')
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
                raise ValueError('Run this through the sophon-tools container (the traefik binary is in that image).')
            print(f'Issuing/renewing certificates for {domain}. Private state: {directory}', file=sys.stderr)
            issue(source, domain, email, token, options.timeout)
        source.chmod(0o600)
        data = source.read_bytes()
        validate_data(data, domain)
        data = minimal_store(data, domain)
        if len(data) > MAX_FILE:
            raise ValueError('The ACME store exceeds the 2 MiB import limit; use a dedicated store for this domain.')
        iso = write_certificate_iso(data, options.output_iso)
        print(f'Secret certificate disk written: {iso} ({os.path.getsize(iso)} bytes, label {CERTIFICATE_LABEL}). '
              'It holds a private key: attach it to the InfraVM, run sophon-controller, then detach and delete it.',
              file=sys.stderr)
        if options.output_file:
            descriptor = os.open(options.output_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
            print(f'Certificate file written: {options.output_file}', file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Certificate issuance failed: {error}\n')
    except (EOFError, KeyboardInterrupt):
        parser.exit(130, 'Certificate issuance cancelled; nothing exported.\n')
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
