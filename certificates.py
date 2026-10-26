"""Validate pre-issued service certificates and build a separate, private handoff ISO."""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError


SERVICES = ('traefik', 'portainer', 'dns', 'ldap', 'auth', 'git')
IMPORT_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
    echo "Run this certificate handoff with sudo after installing InfraVM." >&2
    exit 1
fi
project=/var/lib/sophon/project
destination="$project/artifacts/traefik"
if [[ ! -f "$project/site.yml" || $(id -u core) != 1000 ||
      -L "$project/artifacts" || -L "$destination" ]]; then
    echo "An installed Sophon project without redirected artifact directories is required." >&2
    exit 1
fi
install -d -m 0700 -o 1000 -g 1000 "$destination"
temporary=$(mktemp "$destination/.acme.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
install -m 0600 -o 1000 -g 1000 "$(dirname -- "${BASH_SOURCE[0]}")/acme.json" "$temporary"
mv -f -- "$temporary" "$destination/acme.json"
echo "Certificate handoff imported. Unmount and eject this private ISO."
echo "Run sophon-controller; for a certificate refresh, add --refresh-certificates."
'''


def validate(source, domain):
    domain = domain.lower().rstrip('.')
    payload = json.loads(Path(source).read_bytes())
    if set(payload) != {'letsencrypt'} or set(payload['letsencrypt']) - {'Account', 'Certificates'}:
        raise ValueError('Use a Traefik acme.json containing only the letsencrypt resolver, not other settings or tokens.')
    if set(payload['letsencrypt'].get('Account') or {}) - {'Email', 'Registration', 'PrivateKey', 'KeyType'}:
        raise ValueError('Unexpected ACME account settings; do not include API tokens in the handoff.')
    entries = payload['letsencrypt']['Certificates']
    records = {}
    for entry in entries:
        if set(entry) - {'domain', 'certificate', 'key', 'Store'}:
            raise ValueError('Unexpected data in the certificate store; only ACME identity state may cross the boundary.')
        if set(entry['domain']) - {'main', 'sans'}:
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
        key = serialization.load_pem_private_key(base64.b64decode(entry['key'], validate=True), password=None)
        encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        if key.public_key().public_bytes(encoding, form) != chain[0].public_key().public_bytes(encoding, form):
            raise ValueError(f'The certificate and private key for {name} do not match.')
        try:
            PolicyBuilder().store(store).time(now).build_server_verifier(x509.DNSName(name)).verify(chain[0], chain[1:])
        except VerificationError as error:
            raise ValueError(f'{name} does not have a trusted, currently valid certificate: {error}') from error
        expiries[name] = chain[0].not_valid_after_utc.isoformat()
    return expiries


def build_iso(source, output):
    output = Path(output).resolve()
    if output == Path(source).resolve():
        raise ValueError('The private ISO must not replace its source acme.json.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sophon-certificates-', dir=output.parent) as temporary:
        directory = Path(temporary)
        certificate = directory / 'acme.json'
        certificate.write_bytes(Path(source).read_bytes())
        certificate.chmod(0o600)
        importer = directory / 'import.sh'
        importer.write_text(IMPORT_SCRIPT)
        importer.chmod(0o500)
        iso = directory / 'handoff.iso'
        iso.touch(mode=0o600)
        subprocess.run(['xorriso', '-as', 'mkisofs', '-quiet', '-V', 'SOPHON_CERTS',
                        '-o', str(iso), str(certificate), str(importer)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.replace(iso, output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Private Traefik-format acme.json')
    parser.add_argument('--domain', required=True)
    parser.add_argument('--output', type=Path, help='Build private SOPHON_CERTS ISO; omit to validate only')
    options = parser.parse_args(argv)
    try:
        expiries = validate(options.source, options.domain)
        if options.output:
            build_iso(options.source, options.output)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Certificate handoff failed: {error}\n')
    print(json.dumps({'certificate_expiries': expiries, 'private_iso': str(options.output) if options.output else None}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
