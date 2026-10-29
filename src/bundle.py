"""Build the non-secret SOPHON_DEPLOY ISO on the connected laptop.

It carries everything InfraVM needs offline: the service playbook and roles,
the runner image, container archives and the installer. No downloads occur here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
# Only what step 4 runs; hypervisor roles and credentials never reach the VM.
PROJECT = (
    '4-configure.yml', 'ansible.cfg', 'inventory.yml', 'group_vars',
    *(f'roles/{role}' for role in ('portainer', 'portainer_stack', 'coredns', 'traefik',
                                   'openldap', 'keycloak', 'gitea')),
    'src/console.py', 'src/deployment.py', 'src/certificates.py',
)
GUEST = ('host-bootstrap.sh', 'sophon-deploy', 'sophon-network', 'sophon-set-password')
EXCLUDED = frozenset((
    'artifacts', 'deployment', 'logs', 'log', 'keys', 'credentials', 'secrets',
    '__pycache__', 'node_modules', 'result', 'results',
))


def regular_path(path, directory=False):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Symlinks are not permitted in bundle inputs: {path}')
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f'Missing {"directory" if directory else "file"}: {path}')
    return path


def excluded(name):
    lower = name.lower()
    return (lower.startswith('.') or lower in EXCLUDED
            or lower.startswith(('id_rsa', 'id_ed25519'))
            or re.search(r'(^|[._-])(secrets?|credentials?|passwords?|tokens?|keys?)([._-]|$)', lower)
            or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.log', '.pyc', '~')))


def copy_tree(source, destination):
    regular_path(source, directory=True)
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        if excluded(child.name):
            continue
        if child.is_symlink():
            raise ValueError(f'Symlinks are not permitted in bundle inputs: {child}')
        if child.is_dir():
            copy_tree(child, destination / child.name)
        elif stat.S_ISREG(child.stat().st_mode):
            shutil.copy2(child, destination / child.name)
        else:
            raise ValueError(f'Only regular files are permitted: {child}')


CHUNK = 4 * 1024 * 1024


def copy_hashed(source, destination):
    """Copy a file and return its SHA-256 from the same single read."""
    digest = hashlib.sha256()
    with source.open('rb') as reader, destination.open('wb') as writer:
        for chunk in iter(lambda: reader.read(CHUNK), b''):
            digest.update(chunk)
            writer.write(chunk)
    shutil.copymode(source, destination)
    return digest.hexdigest()


def write_checksums(stage, known=None):
    """`known` maps relative paths to digests already computed while copying."""
    known = known or {}
    with (stage / 'SHA256SUMS').open('w', encoding='utf-8') as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and path != stage / 'SHA256SUMS':
                relative = path.relative_to(stage).as_posix()
                if any(ord(char) < 32 or ord(char) == 127 for char in relative) or '\\' in relative:
                    raise ValueError(f'Unsafe checksum filename: {relative!r}')
                if relative in known:
                    output.write(f'{known[relative]}  {relative}\n')
                    continue
                digest = hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda: source.read(CHUNK), b''):
                        digest.update(chunk)
                output.write(f'{digest.hexdigest()}  {relative}\n')


def project_files(source):
    """Files copy_tree would copy; raises on symlinks so a stale stamp can never hide them."""
    if source.is_file() and not source.is_symlink():
        yield source
        return
    for directory, directories, files in os.walk(source):
        directories[:] = sorted(name for name in directories if not excluded(name))
        for name in (*directories, *files):
            if (Path(directory) / name).is_symlink():
                raise ValueError(f'Symlinks are not permitted in bundle inputs: {Path(directory) / name}')
        for name in sorted(files):
            if not excluded(name):
                yield Path(directory) / name


def fingerprint(root, output, runner, containers, tools, portainer_image, portainer_archive):
    """Digest of every bundle input, or None if it cannot be computed (then the bundle is simply rebuilt).

    Small files are hashed by content; the large archives by name, size and mtime so no gigabytes are read.
    """
    try:
        summary = hashlib.sha256(json.dumps([portainer_image, portainer_archive, str(output)]).encode())
        small = [Path(__file__).resolve(), root / 'guest/install.sh',
                 *(root / 'guest' / name for name in GUEST)]
        for name in PROJECT:
            small.extend(project_files(root / name))
        for path in small:
            summary.update(f'{path.relative_to(root) if path.is_relative_to(root) else path.name}\0'.encode())
            with path.open('rb') as stream:
                summary.update(hashlib.file_digest(stream, 'sha256').digest())
        for path in sorted((runner, tools, *containers)):
            info = regular_path(path).stat()
            summary.update(f'{path.name}\0{info.st_size}\0{info.st_mtime_ns}\0'.encode())
        return summary.hexdigest()
    except (ValueError, OSError):
        return None


def create_bundle(root, output, runner, containers, *, bootstrap_tools=None,
                  portainer_image='docker.io/portainer/portainer-ee:3.0.0',
                  portainer_archive='portainer-portainer-ee_3.0.0.tar'):
    root = regular_path(root, directory=True)
    for name in (*PROJECT, *(f'guest/{name}' for name in (*GUEST, 'install.sh'))):
        path = root / name
        regular_path(path, directory=path.is_dir())
    runner = regular_path(runner)
    tools = regular_path(bootstrap_tools or root / 'artifacts/bootstrap/busybox-1.38.0-musl')
    if not containers:
        raise ValueError('An explicit container list or manifest is required')
    containers = [regular_path(path) for path in containers]
    if len({path.name for path in containers}) != len(containers):
        raise ValueError('Container archive basenames must be unique')
    if any(not path.name.endswith('.tar') for path in containers):
        raise ValueError('Container archives must use their versioned .tar filenames')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./_:@-]+', portainer_image)
            or portainer_archive not in {path.name for path in containers}):
        raise ValueError('Portainer metadata must identify a supplied image archive')
    output = Path(os.path.abspath(output))
    if output in (runner, tools) or output in containers:
        raise ValueError('ISO output must not overwrite an input')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build on the local temp disk: xorriso reads files in small pieces, which is about 5x slower than a plain copy
    # over a Windows bind mount (WSL, Docker Desktop). Honour TMPDIR if it needs more room.
    with tempfile.TemporaryDirectory(prefix='sophon-bundle-') as temporary:
        work = Path(temporary)
        stage = work / 'contents'
        project = stage / 'project'
        project.mkdir(parents=True)
        for name in PROJECT:
            source = root / name
            if source.is_dir():
                copy_tree(source, project / name)
            else:
                (project / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, project / name)
        metadata = project / 'bootstrap'
        metadata.mkdir()
        (metadata / 'portainer-image').write_text(portainer_image + '\n')
        (metadata / 'portainer-archive').write_text(portainer_archive + '\n')
        container_dir = project / 'artifacts' / 'containers'
        container_dir.mkdir(parents=True)
        known = {}

        def place(source, destination):
            # Hardlink the large archives (no copy); if the filesystem refuses, copy and hash in one read.
            try:
                os.link(source, destination)
            except OSError:
                known[destination.relative_to(stage).as_posix()] = copy_hashed(source, destination)

        for archive in containers:
            place(archive, container_dir / archive.name)
        place(runner, stage / 'runner.tar')
        (stage / 'tools').mkdir()
        shutil.copyfile(tools, stage / 'tools/busybox')
        (stage / 'tools/busybox').chmod(0o755)
        (stage / 'guest').mkdir()
        for name in GUEST:
            shutil.copyfile(root / 'guest' / name, stage / 'guest' / name)
            (stage / 'guest' / name).chmod(0o755)
        shutil.copyfile(root / 'guest/install.sh', stage / 'install.sh')
        (stage / 'install.sh').chmod(0o755)
        write_checksums(stage, known)
        image = work / 'deployment.iso'
        subprocess.run([
            'xorriso', '-as', 'mkisofs', '-R', '-J', '-iso-level', '3',
            '-V', 'SOPHON_DEPLOY', '-o', str(image), str(stage),
        ], check=True)
        if not image.is_file() or not image.stat().st_size:
            raise ValueError('xorriso did not produce a nonempty ISO')
        # The ISO is moved next to the output under a temporary name, then renamed, so a failure never leaves a partial ISO.
        partial = tempfile.NamedTemporaryFile(prefix='.sophon-bundle-', suffix='.iso', dir=output.parent, delete=False)
        partial.close()
        try:
            shutil.copyfile(image, partial.name)
            os.replace(partial.name, output)
        finally:
            if os.path.exists(partial.name):
                os.unlink(partial.name)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--runner', type=Path)
    parser.add_argument('--bootstrap-tools', type=Path)
    parser.add_argument('--portainer-image', default='docker.io/portainer/portainer-ee:3.0.0')
    parser.add_argument('--portainer-archive', default='portainer-portainer-ee_3.0.0.tar')
    parser.add_argument('--container', type=Path, action='append',
                        help='Expected container archive; repeat for every required image')
    parser.add_argument('--manifest', type=Path,
                        help='JSON list of expected archives (relative paths are repo-relative)')
    parser.add_argument('--force', action='store_true', help='Rebuild even when no input changed')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        containers = args.container
        if containers is None:
            manifest = args.manifest or root / 'artifacts/containers/manifest.json'
            containers = json.loads(regular_path(manifest).read_text())
            if not isinstance(containers, list) or not all(isinstance(path, str) for path in containers):
                raise ValueError('Container manifest must be a JSON list of paths')
            containers = [root / path for path in containers]
        output = Path(os.path.abspath(args.output or root / 'artifacts/bootstrap/sophon-deployment.iso'))
        runner = args.runner or root / 'artifacts/bootstrap/runner.tar'
        tools = args.bootstrap_tools or root / 'artifacts/bootstrap/busybox-1.38.0-musl'
        stamp = output.with_name(output.name + '.stamp')
        current = fingerprint(root, output, runner, containers, tools, args.portainer_image, args.portainer_archive)
        if (current and not args.force and output.is_file() and stamp.is_file()
                and stamp.read_text().strip() == current):
            print(f'Deployment ISO is up to date (inputs unchanged): {output}')
            return 0
        output = create_bundle(
            root, output, runner, containers,
            bootstrap_tools=args.bootstrap_tools, portainer_image=args.portainer_image,
            portainer_archive=args.portainer_archive,
        )
        if current:
            stamp.write_text(current + '\n')
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Bundle creation failed: {error}', file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
