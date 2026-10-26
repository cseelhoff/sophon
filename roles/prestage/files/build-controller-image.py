"""Adapt the existing Alpine builder, executing Controller setup in the rootfs."""

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def adapted_builder(source, files, work):
    start = source.index('# Process chroot script - apply file-based customizations')
    end = source.index('echo "==> Creating rootfs tarball"', start)
    customization = f'''
mkdir -p "$ROOTFS_DIR/usr/local/share/sophon-controller"
cp -R {shlex.quote(str(files))}/. "$ROOTFS_DIR/usr/local/share/sophon-controller/"
proot -0 -w / -r "$ROOTFS_DIR" \\
    /bin/sh /usr/local/share/sophon-controller/controller-setup.sh

'''
    source = source[:start] + customization + source[end:]
    return source.replace('WORK_DIR=$(mktemp -d)', f'WORK_DIR={shlex.quote(str(work))}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builder', type=Path, required=True)
    parser.add_argument('--files', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--branch', default='3.21')
    parser.add_argument('--image-size', default='10G')
    parser.add_argument('--packages', required=True)
    args = parser.parse_args()
    files = args.files.resolve()
    if not (files / 'controller-setup.sh').is_file():
        parser.error('Controller customization script is missing')
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.controller-build-', dir=output.parent) as temporary:
        work = Path(temporary)
        rootfs_work = work / 'alpine'
        rootfs_work.mkdir()
        builder = work / 'build-alpine-image'
        builder.write_text(adapted_builder(args.builder.read_text(), files, rootfs_work))
        image = work / 'controller.qcow2'
        subprocess.run([
            'bash', str(builder), '--image-size', args.image_size,
            '--image-format', 'qcow2', '--branch', 'v' + args.branch.lstrip('v'),
            '--packages', args.packages, str(image),
        ], check=True)
        if not image.is_file() or not image.stat().st_size:
            raise ValueError('Alpine builder did not produce a nonempty disk')
        os.replace(image, output)


if __name__ == '__main__':
    main()
