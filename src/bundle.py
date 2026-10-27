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
    '4-configure.yml', 'ansible.cfg', 'inventory.yml', 'group_vars', 'callback_plugins',
    *(f'roles/{role}' for role in ('portainer', 'portainer_stack', 'coredns', 'traefik',
                                   'openldap', 'keycloak', 'gitea')),
    'src/console.py', 'src/deployment.py', 'src/certificates.py', 'src/dashboard',
)
GUEST = ('host-bootstrap.sh', 'sophon-controller')
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


def write_checksums(stage):
    with (stage / 'SHA256SUMS').open('w', encoding='utf-8') as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and path != stage / 'SHA256SUMS':
                relative = path.relative_to(stage).as_posix()
                if any(ord(char) < 32 or ord(char) == 127 for char in relative) or '\\' in relative:
                    raise ValueError(f'Unsafe checksum filename: {relative!r}')
                digest = hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(chunk)
                output.write(f'{digest.hexdigest()}  {relative}\n')


def create_bundle(root, output, runner, containers, *, bootstrap_tools=None,
                  portainer_image='docker.io/portainer/portainer-ee:2.41.0',
                  portainer_archive='portainer-portainer-ee_2.41.0.tar'):
    root = regular_path(root, directory=True)
    for name in (*PROJECT, *(f'guest/{name}' for name in (*GUEST, 'install.sh'))):
        path = root / name
        regular_path(path, directory=path.is_dir())
    runner = regular_path(runner)
    tools = regular_path(bootstrap_tools or root / 'artifacts/bootstrap/busybox')
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
    # All scratch data stays alongside the output, never in a system temp directory.
    with tempfile.TemporaryDirectory(prefix='.sophon-bundle-', dir=output.parent) as temporary:
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
        for archive in containers:
            shutil.copyfile(archive, container_dir / archive.name)
        shutil.copyfile(runner, stage / 'runner.tar')
        (stage / 'tools').mkdir()
        shutil.copyfile(tools, stage / 'tools/busybox')
        (stage / 'tools/busybox').chmod(0o755)
        (stage / 'guest').mkdir()
        for name in GUEST:
            shutil.copyfile(root / 'guest' / name, stage / 'guest' / name)
            (stage / 'guest' / name).chmod(0o755)
        shutil.copyfile(root / 'guest/install.sh', stage / 'install.sh')
        (stage / 'install.sh').chmod(0o755)
        write_checksums(stage)
        image = work / 'deployment.iso'
        subprocess.run([
            'xorriso', '-as', 'mkisofs', '-R', '-J', '-iso-level', '3',
            '-V', 'SOPHON_DEPLOY', '-o', str(image), str(stage),
        ], check=True)
        if not image.is_file() or not image.stat().st_size:
            raise ValueError('xorriso did not produce a nonempty ISO')
        os.replace(image, output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--runner', type=Path)
    parser.add_argument('--bootstrap-tools', type=Path)
    parser.add_argument('--portainer-image', default='docker.io/portainer/portainer-ee:2.41.0')
    parser.add_argument('--portainer-archive', default='portainer-portainer-ee_2.41.0.tar')
    parser.add_argument('--container', type=Path, action='append',
                        help='Expected container archive; repeat for every required image')
    parser.add_argument('--manifest', type=Path,
                        help='JSON list of expected archives (relative paths are repo-relative)')
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
        output = create_bundle(
            root, args.output or root / 'artifacts/bootstrap/sophon-deployment.iso',
            args.runner or root / 'artifacts/bootstrap/runner.tar', containers,
            bootstrap_tools=args.bootstrap_tools, portainer_image=args.portainer_image,
            portainer_archive=args.portainer_archive,
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Bundle creation failed: {error}', file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
