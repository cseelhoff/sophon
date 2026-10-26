"""Build the non-secret SOPHON_DEPLOY ISO on the connected laptop.

Run after the prestage bootstrap tasks, or supply --runner, --collections and
one --container per required archive explicitly. No downloads occur here.
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


ROOT = Path(__file__).resolve().parent
SNAPSHOT = (
    'scripts', 'roles', 'group_vars', 'callback_plugins', 'dashboard', 'inventory',
    'ansible.cfg', 'requirements.yml', 'flake.nix', 'flake.lock', 'site.yml',
    'services.yml', 'bootstrap.yml', 'deployment.py', 'console.py', 'bundle.py', 'certificates.py', 'sophon.svg',
)
REQUIRED_FILES = ('ansible.cfg', 'requirements.yml', 'flake.nix', 'site.yml',
                  'bootstrap.yml', 'deployment.py', 'console.py', 'bundle.py', 'certificates.py', 'sophon.svg',
                  'scripts/install.sh', 'scripts/host-bootstrap.sh', 'scripts/sophon-controller')
ESSENTIAL_COLLECTIONS = (
    'community.proxmox', 'community.general', 'community.docker', 'ansible.posix',
)
SITE_ENV = frozenset()
EXCLUDED = frozenset((
    'artifacts', 'deployment', 'logs', 'log', 'keys', 'credentials', 'secrets',
    'tailscale', '__pycache__', 'node_modules', 'result', 'results',
))


def site_settings(environment):
    """Site inputs are collected at the live console, never from the laptop."""
    return {}


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
            or re.search(r'(^|[._-])(secrets?|credentials?|tailscale|passwords?|tokens?|keys?)([._-]|$)', lower)
            or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.log', '.pyc', '~')))


def copy_tree(source, destination, snapshot=False, file_link_root=None):
    """Only vendored collection files may resolve links within their input tree."""
    regular_path(source, directory=True)
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        if snapshot and excluded(child.name):
            continue
        if child.is_symlink():
            target = child.resolve()
            if file_link_root is None or not target.is_relative_to(file_link_root) or not target.is_file():
                raise ValueError(f'Symlinks are not permitted in bundle inputs: {child}')
            shutil.copy2(target, destination / child.name)
            continue
        if child.is_dir():
            copy_tree(child, destination / child.name, snapshot, file_link_root)
        elif stat.S_ISREG(child.stat().st_mode):
            shutil.copy2(child, destination / child.name)
        else:
            raise ValueError(f'Only regular files are permitted: {child}')


def collection_names(requirements):
    # requirements.yml uses Galaxy's simple "- name: namespace.collection" form.
    names = set(ESSENTIAL_COLLECTIONS)
    names.update(re.findall(r'^\s*-\s*name:\s*[\'"]?([a-z0-9_]+\.[a-z0-9_]+)',
                            requirements.read_text(), re.MULTILINE))
    return sorted(names)


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


def create_bundle(root, output, runner, collections, containers, environment=None, *,
                  bootstrap_tools=None, portainer_image='docker.io/portainer/portainer-ee:2.41.0',
                  portainer_archive='portainer-portainer-ee_2.41.0.tar'):
    root = regular_path(root, directory=True)
    for name in REQUIRED_FILES:
        regular_path(root / name)
    runner = regular_path(runner)
    tools = regular_path(bootstrap_tools or root / 'artifacts/bootstrap/busybox')
    collections = regular_path(Path(collections) / 'ansible_collections', directory=True)
    for name in collection_names(root / 'requirements.yml'):
        regular_path(collections.joinpath(*name.split('.')), directory=True)
    if not containers:
        raise ValueError('An explicit container list or prestage manifest is required')
    containers = [regular_path(path) for path in containers]
    if len({path.name for path in containers}) != len(containers):
        raise ValueError('Container archive basenames must be unique')
    if any(not path.name.endswith('.tar') for path in containers):
        raise ValueError('Container archives must use their versioned .tar filenames')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./_:@-]+', portainer_image)
            or portainer_archive not in {path.name for path in containers}):
        raise ValueError('Portainer metadata must identify a supplied image archive')
    settings = site_settings(os.environ if environment is None else environment)
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
        for name in SNAPSHOT:
            source = root / name
            if source.is_symlink():
                raise ValueError(f'Symlinks are not permitted in bundle inputs: {source}')
            if source.is_dir():
                copy_tree(source, project / name, snapshot=True)
            elif source.exists():
                shutil.copy2(regular_path(source), project / name)
        (project / 'site.json').write_text(json.dumps(settings, indent=2, sort_keys=True) + '\n')
        metadata = project / 'bootstrap'
        metadata.mkdir()
        (metadata / 'portainer-image').write_text(portainer_image + '\n')
        (metadata / 'portainer-archive').write_text(portainer_archive + '\n')
        container_dir = project / 'artifacts' / 'containers'
        container_dir.mkdir(parents=True)
        for archive in containers:
            shutil.copyfile(archive, container_dir / archive.name)
        copy_tree(collections, project / 'artifacts' / 'collections' / 'ansible_collections',
                  file_link_root=collections)
        shutil.copyfile(runner, stage / 'runner.tar')
        (stage / 'tools').mkdir()
        shutil.copyfile(tools, stage / 'tools/busybox')
        (stage / 'tools/busybox').chmod(0o755)
        shutil.copyfile(root / 'scripts/install.sh', stage / 'install.sh')
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
    parser.add_argument('--collections', type=Path,
                        help='Root containing ansible_collections/')
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
            args.runner or root / 'artifacts/bootstrap/runner.tar',
            args.collections or root / 'artifacts/collections', containers,
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
