"""Bake Sophon's unattended confined-SFTP backup server installer into the stock Alpine virt ISO.

Produces a single hybrid BIOS + UEFI ISO (`artifacts/backup/sophon-sftp-server.iso`) carrying:
- `localhost.apkovl.tar.gz`: boots into OpenRC and runs `guest/backup-appliance/install.sh --auto` on a single blank disk
- `sophon-backup-os.tar.gz`: the prebuilt Alpine 3.24.4 OS rootfs archive with confined `internal-sftp` preconfigured
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = ROOT / 'guest/backup-appliance/install.sh'

WORLD = '\n'.join([
    'alpine-base',
    'e2fsprogs',
    'e2fsprogs-extra',
    'libblkid',
    'partx',
    'sfdisk',
]) + '\n'

ISSUE = (
    'Sophon Backup Server installer: installs automatically onto a single blank disk, then reboots.\n'
    'Manual install / disk selection: /usr/local/libexec/sophon-backup-install\n\n'
)

AUTOSTART = (
    '#!/bin/sh\n'
    '# Run the unattended Sophon Backup Server installer on the primary console.\n'
    'exec >/dev/console 2>&1 </dev/console\n'
    'printf "\\n== Starting Sophon Backup Server unattended install ==\\n"\n'
    'if ! /usr/local/libexec/sophon-backup-install --auto; then\n'
    '    printf "\\nAutomatic install stopped. Log in as root (no password in live installer) and run:\\n"\n'
    '    printf "  /usr/local/libexec/sophon-backup-install\\n\\n"\n'
    'fi\n'
)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int):
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = 'root'
    info.gname = 'root'
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def _add_dir(tar: tarfile.TarFile, name: str, mode: int = 0o755):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = 'root'
    info.gname = 'root'
    info.mtime = 0
    tar.addfile(info)


def _add_symlink(tar: tarfile.TarFile, name: str, target: str):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mode = 0o777
    info.uid = 0
    info.gid = 0
    info.uname = 'root'
    info.gname = 'root'
    info.mtime = 0
    tar.addfile(info)


def build_apkovl(script: Path = INSTALL_SCRIPT) -> bytes:
    """Return deterministic gzip-compressed `localhost.apkovl.tar.gz` bytes."""
    installer = Path(script).read_bytes()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w') as tar:
        for directory in (
            'etc', 'etc/apk', 'etc/local.d', 'etc/runlevels', 'etc/runlevels/default',
            'usr', 'usr/local', 'usr/local/libexec',
        ):
            _add_dir(tar, directory)
        _add_bytes(tar, 'etc/.default_boot_services', b'', 0o644)
        _add_bytes(tar, 'etc/hostname', b'localhost\n', 0o644)
        _add_bytes(tar, 'etc/apk/world', WORLD.encode(), 0o644)
        _add_bytes(tar, 'etc/issue', ISSUE.encode(), 0o644)
        _add_symlink(tar, 'etc/runlevels/default/local', '/etc/init.d/local')
        _add_bytes(tar, 'etc/local.d/sophon-backup-install.start', AUTOSTART.encode(), 0o755)
        _add_bytes(tar, 'usr/local/libexec/sophon-backup-install', installer, 0o755)
    return gzip.compress(raw.getvalue(), mtime=0)


def fingerprint(stock: Path, os_archive: Path, apkovl: bytes) -> str:
    stock_stat = stock.stat()
    os_stat = os_archive.stat()
    digest = hashlib.sha256(apkovl)
    digest.update(f'{stock.name}\0{stock_stat.st_size}\0{stock_stat.st_mtime_ns}\0'.encode())
    digest.update(f'{os_archive.name}\0{os_stat.st_size}\0{os_stat.st_mtime_ns}'.encode())
    return digest.hexdigest()


def build(stock: Path, os_archive: Path, output: Path, force: bool = False, script: Path = INSTALL_SCRIPT) -> bool:
    """Write the customised Alpine backup installer ISO to `output`; returns False when up to date."""
    stock = Path(stock)
    os_archive = Path(os_archive)
    output = Path(os.path.abspath(output))
    for label, path in (('stock Alpine ISO', stock), ('backup OS archive', os_archive)):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing {label}: {path} (run step 1 first).')
    if not shutil.which('xorriso'):
        raise ValueError('xorriso is missing; run this through the sophon-tools container.')
    if output in (stock, os_archive):
        raise ValueError('The backup installer ISO must not overwrite its input files.')
    apkovl = build_apkovl(script)
    current = fingerprint(stock, os_archive, apkovl)
    stamp = output.with_name(output.name + '.stamp')
    if not force and output.is_file() and stamp.is_file() and stamp.read_text().strip() == current:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sophon-backup-iso-') as work:
        ovl_path = Path(work) / 'localhost.apkovl.tar.gz'
        ovl_path.write_bytes(apkovl)
        image = Path(work) / 'backup-installer.iso'
        subprocess.run([
            'xorriso',
            '-indev', str(stock),
            '-outdev', str(image),
            '-boot_image', 'any', 'replay',
            '-volid', 'SOPHON_BACKUP_INSTALL',
            '-map', str(ovl_path), '/localhost.apkovl.tar.gz',
            '-map', str(os_archive), '/sophon-backup-os.tar.gz',
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if not image.is_file() or not image.stat().st_size:
            raise ValueError('xorriso did not produce a nonempty backup installer ISO')
        partial = tempfile.NamedTemporaryFile(
            prefix='.sophon-sftp-server-', suffix='.iso', dir=output.parent, delete=False)
        partial.close()
        try:
            shutil.copyfile(image, partial.name)
            os.replace(partial.name, output)
        finally:
            if os.path.exists(partial.name):
                os.unlink(partial.name)
    stamp.write_text(current + '\n')
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock', type=Path, default=ROOT / 'artifacts/backup/alpine-virt-3.24.4-x86_64.iso')
    parser.add_argument('--os-archive', type=Path, default=ROOT / 'artifacts/backup/sophon-backup-os.tar.gz')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/backup/sophon-sftp-server.iso')
    parser.add_argument('--force', action='store_true', help='Rebuild even when inputs are unchanged')
    options = parser.parse_args(argv)
    try:
        built = build(options.stock, options.os_archive, options.output, options.force)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'output', '') or str(error)
        print(f'Backup installer ISO failed: {detail}', file=sys.stderr)
        return 1
    print(f'Backup installer ISO {"built" if built else "is up to date (inputs unchanged)"}: {options.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
