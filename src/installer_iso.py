"""Bake Sophon's unattended installer into the stock Fedora CoreOS live ISO (step 1, no network needed).

The result boots the live system, finds the SOPHON_DEPLOY ISO attached as a second CD/DVD and runs its installer by
itself. Only a small live Ignition config is embedded; the images and playbooks stay on the deployment ISO.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
AUTOINSTALL = ROOT / 'guest/sophon-autoinstall'

UNIT = """[Unit]
Description=Sophon unattended install
ConditionPathExists=/run/ostree-live
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/sophon-autoinstall
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
"""
ISSUE = ('Sophon installer: it runs by itself once the deployment ISO (SOPHON_DEPLOY) is attached, then reboots.\n'
         'Progress: journalctl -fu sophon-install     Retry: sudo systemctl restart sophon-install\n')


def data_url(text):
    return 'data:;base64,' + base64.b64encode(text.encode()).decode()


def live_ignition(script=AUTOINSTALL):
    return {
        'ignition': {'version': '3.4.0'},
        'storage': {'files': [
            {'path': '/usr/local/libexec/sophon-autoinstall', 'mode': 0o755,
             'contents': {'source': data_url(Path(script).read_text())}},
            {'path': '/etc/issue.d/sophon-installer.issue', 'mode': 0o644, 'contents': {'source': data_url(ISSUE)}},
        ]},
        'systemd': {'units': [{'name': 'sophon-install.service', 'enabled': True, 'contents': UNIT}]},
    }


def fingerprint(stock, ignition):
    info = stock.stat()
    digest = hashlib.sha256(json.dumps(ignition, sort_keys=True).encode())
    # Size and mtime stand in for hashing the 800 MB stock ISO (prepare already verified its checksum on download).
    digest.update(f'{stock.name}\0{info.st_size}\0{info.st_mtime_ns}'.encode())
    return digest.hexdigest()


def build(stock, output, force=False):
    """Write the customised ISO to `output`; returns False when it was already up to date."""
    stock, output = Path(stock), Path(os.path.abspath(output))
    if not stock.is_file() or stock.is_symlink():
        raise ValueError(f'Missing stock CoreOS live ISO: {stock} (run step 1 first).')
    if not shutil.which('coreos-installer'):
        raise ValueError('coreos-installer is missing; run this through the sophon-tools container.')
    if output == stock:
        raise ValueError('The installer ISO must not overwrite the stock ISO.')
    ignition = live_ignition()
    current = fingerprint(stock, ignition)
    stamp = output.with_name(output.name + '.stamp')
    if not force and output.is_file() and stamp.is_file() and stamp.read_text().strip() == current:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build on the local disk, then move the result next to the output, so a failure never leaves a partial ISO.
    with tempfile.TemporaryDirectory(prefix='sophon-installer-') as work:
        config = Path(work) / 'live.ign'
        config.write_text(json.dumps(ignition))
        image = Path(work) / 'installer.iso'
        subprocess.run(['coreos-installer', 'iso', 'customize', '--live-ignition', str(config),
                        '-o', str(image), str(stock)], check=True)
        if not image.is_file() or not image.stat().st_size:
            raise ValueError('coreos-installer did not produce a nonempty ISO')
        partial = tempfile.NamedTemporaryFile(prefix='.sophon-installer-', suffix='.iso', dir=output.parent, delete=False)
        partial.close()
        try:
            shutil.copyfile(image, partial.name)
            os.replace(partial.name, output)
        finally:
            if os.path.exists(partial.name):
                os.unlink(partial.name)
    stamp.write_text(current + '\n')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock', type=Path, default=ROOT / 'artifacts/coreos/fedora-coreos-live.iso')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/coreos/sophon-installer.iso')
    parser.add_argument('--force', action='store_true', help='Rebuild even when nothing changed')
    options = parser.parse_args(argv)
    try:
        built = build(options.stock, options.output, options.force)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Installer ISO failed: {error}', file=sys.stderr)
        return 1
    print(f'Installer ISO {"built" if built else "is up to date (inputs unchanged)"}: {options.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
