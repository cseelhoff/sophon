import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.bundle-test-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'repo'
        self.root.mkdir()
        for name in bundle.PROJECT:
            if Path(name).suffix:
                self.write(name)
            else:
                self.write(f'{name}/tasks/main.yml')
        for name in (*bundle.GUEST, 'install.sh'):
            self.write(f'guest/{name}', f'#!/bin/sh\n# {name}\n')
        self.tools = self.write('artifacts/bootstrap/busybox-1.38.0-musl', 'static-helper-fixture')
        self.runner = self.directory / 'runner.tar'
        self.runner.write_bytes(b'runner-image')
        self.containers = [self.directory / name for name in (
            'portainer-portainer-ee_3.0.0.tar', 'keycloak-keycloak_26.8.1.tar',
            'coredns-dockerdiscovery.tar',
        )]
        for container in self.containers:
            container.write_bytes(container.name.encode())
        self.output = self.directory / 'output' / 'sophon-deployment.iso'
        self.captured = {}

    def write(self, name, content='source'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def encode_iso(self, command, **kwargs):
        self.assertEqual(command[:3], ['xorriso', '-as', 'mkisofs'])
        self.assertIn('-R', command)
        self.assertIn('-J', command)
        self.assertEqual(command[command.index('-iso-level') + 1], '3')
        self.assertEqual(command[command.index('-V') + 1], 'SOPHON_DEPLOY')
        stage = Path(command[-1])
        self.captured = {str(path.relative_to(stage)): path.read_bytes()
                         for path in stage.rglob('*') if path.is_file()}
        Path(command[command.index('-o') + 1]).write_bytes(b'valid-iso')

    def create(self, containers=None):
        return bundle.create_bundle(self.root, self.output, self.runner,
                                    self.containers if containers is None else containers)

    def test_vm_gets_only_the_service_project_and_checksums(self):
        self.stage_locations = []
        original_encode = self.encode_iso
        self.encode_iso = lambda command, **kw: (self.stage_locations.append(Path(command[-1])),
                                                  original_encode(command, **kw))[1]
        self.write('roles/proxmox/tasks/main.yml', 'HYPERVISOR_ONLY')
        self.write('roles/prepare/tasks/main.yml', 'LAPTOP_ONLY')
        self.write('2-provision.yml', 'LAPTOP_ONLY')
        self.write('src/bundle.py', 'LAPTOP_ONLY')
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.assertEqual(self.create(), self.output)
        self.assertEqual(self.output.read_bytes(), b'valid-iso')
        self.assertIn('project/4-configure.yml', self.captured)
        self.assertIn('project/roles/gitea/tasks/main.yml', self.captured)
        self.assertIn('project/src/console.py', self.captured)
        for name in ('install.sh', 'runner.tar', 'tools/busybox', 'guest/sophon-deploy', 'guest/host-bootstrap.sh'):
            self.assertIn(name, self.captured)
        self.assertNotIn(b'HYPERVISOR_ONLY', b''.join(self.captured.values()))
        self.assertNotIn(b'LAPTOP_ONLY', b''.join(self.captured.values()))
        self.assertFalse([name for name in self.captured if 'collections' in name])
        for archive in self.containers:
            self.assertIn('project/artifacts/containers/' + archive.name, self.captured)
        checksums = {}
        for line in self.captured['SHA256SUMS'].decode().splitlines():
            digest, name = line.split('  ', 1)
            checksums[name] = digest
        self.assertEqual(set(checksums), self.captured.keys() - {'SHA256SUMS'})
        for name, digest in checksums.items():
            self.assertEqual(digest, hashlib.sha256(self.captured[name]).hexdigest())
        self.assertFalse(list(self.output.parent.glob('.sophon-bundle-*')))
        # Staging happens on the local temp disk, not beside the (possibly slow, mounted) output.
        self.assertTrue(self.stage_locations)
        self.assertFalse(any(self.output.parent in location.parents for location in self.stage_locations))

    def test_secrets_and_unallowlisted_artifacts_never_copied(self):
        for name in (
            '.env', '.git/config', 'unknown.txt', 'site.json', 'artifacts/deployment/events.json',
            'artifacts/containers/not-declared.tar', 'roles/gitea/.env', 'roles/gitea/credentials.json',
            'roles/gitea/secrets.yml', 'roles/gitea/id_ed25519', 'roles/gitea/config.pem',
            'roles/gitea/artifacts/deployment/key', 'roles/gitea/logs/run.json',
            'roles/gitea/.git/config', 'group_vars/vault.key',
        ):
            self.write(name, 'PRIVATE_MARKER')
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.create()
        self.assertNotIn(b'PRIVATE_MARKER', b''.join(self.captured.values()))
        self.assertNotIn('project/site.json', self.captured)

    def test_missing_inputs_never_create_iso(self):
        inputs = [self.runner, self.containers[1], self.root / '4-configure.yml',
                  self.root / 'guest/install.sh', self.root / 'src/console.py']
        for path in inputs:
            with self.subTest(path=path):
                data = path.read_bytes()
                path.unlink()
                with patch('bundle.subprocess.run') as run:
                    with self.assertRaises(ValueError):
                        self.create()
                    run.assert_not_called()
                self.assertFalse(self.output.exists())
                path.write_bytes(data)

    def test_symlink_files_directories_and_archives_rejected(self):
        target = self.write('unlisted-secret', 'PRIVATE')
        for name in ('roles/gitea/tasks/main.yml', 'roles/portainer', 'group_vars'):
            link = self.root / name
            if link.is_dir():
                shutil.rmtree(link)
            else:
                link.unlink(missing_ok=True)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target if link.suffix else target.parent, target_is_directory=not link.suffix)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Symlinks'):
                self.create()
            link.unlink()
            self.write(f'{name}/tasks/main.yml' if not link.suffix else name)
        link = self.directory / 'linked.tar'
        link.symlink_to(self.containers[0])
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            self.create([link])
        self.assertFalse(self.output.exists())

    def test_explicit_nonempty_unique_archive_list_required(self):
        for paths in ([], [self.containers[0], self.containers[0]], [self.runner.with_suffix('.gz')]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.create(paths)

    def test_atomic_failure_preserves_previous_iso(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'old-valid-iso')

        def fail(command, **kwargs):
            Path(command[command.index('-o') + 1]).write_bytes(b'incomplete')
            raise subprocess.CalledProcessError(1, command)

        with patch('bundle.subprocess.run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                self.create()
        self.assertEqual(self.output.read_bytes(), b'old-valid-iso')
        self.assertFalse(list(self.output.parent.glob('.sophon-bundle-*')))

    def cli_arguments(self):
        self.write('artifacts/containers/manifest.json', json.dumps([str(path) for path in self.containers]))
        return ['--root', str(self.root), '--output', str(self.output), '--runner', str(self.runner)]

    def test_unchanged_inputs_skip_the_rebuild_and_changes_or_force_rebuild(self):
        arguments = self.cli_arguments()
        with patch('bundle.subprocess.run', side_effect=self.encode_iso) as run:
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(run.call_count, 1, 'unchanged inputs must not rebuild')
            self.write('guest/install.sh', '#!/bin/sh\n# changed\n')
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(run.call_count, 2, 'a changed small file must rebuild')
            self.containers[0].write_bytes(b'different archive content')
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(run.call_count, 3, 'a changed archive must rebuild')
            self.assertEqual(bundle.main(arguments + ['--force']), 0)
            self.assertEqual(run.call_count, 4, '--force must rebuild')
            self.output.unlink()
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(run.call_count, 5, 'a missing ISO must rebuild')

    def test_failed_build_never_leaves_a_matching_stamp(self):
        arguments = self.cli_arguments()
        with patch('bundle.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'xorriso')):
            self.assertEqual(bundle.main(arguments), 1)
        self.assertFalse(list(self.output.parent.glob('*.stamp')))

    def test_archives_are_hardlinked_or_hashed_while_copying(self):
        for linked in (True, False):
            with self.subTest(linked=linked):
                real_link = os.link
                broken = OSError('no hardlinks here')

                def link(source, destination, real_link=real_link, linked=linked):
                    if not linked:
                        raise broken
                    return real_link(source, destination)

                with patch('bundle.os.link', side_effect=link), \
                        patch('bundle.subprocess.run', side_effect=self.encode_iso):
                    self.create()
                for line in self.captured['SHA256SUMS'].decode().splitlines():
                    digest, name = line.split('  ', 1)
                    self.assertEqual(digest, hashlib.sha256(self.captured[name]).hexdigest(), name)
                self.assertEqual(self.runner.read_bytes(), b'runner-image')

    def test_cli_manifest_defaults_and_explicit_containers(self):
        self.write('artifacts/containers/manifest.json', json.dumps([str(path) for path in self.containers]))
        arguments = ['--root', str(self.root), '--output', str(self.output), '--runner', str(self.runner)]
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(bundle.main(arguments + sum(
                (['--container', str(path)] for path in self.containers), [])), 0)
        self.write('artifacts/containers/manifest.json', '{"bad": "manifest"}')
        with patch('bundle.subprocess.run') as run:
            self.assertEqual(bundle.main(arguments), 1)
            run.assert_not_called()

    @unittest.skipUnless(shutil.which('xorriso'), 'xorriso is not installed')
    def test_real_iso_label_tree_and_checksum_verification(self):
        self.create()
        extracted = self.directory / 'extracted'
        result = subprocess.run(
            ['xorriso', '-osirrox', 'on', '-indev', str(self.output),
             '-pvd_info', '-extract', '/', str(extracted)],
            capture_output=True, text=True, check=True)
        self.assertIn('SOPHON_DEPLOY', result.stdout + result.stderr)
        for line in (extracted / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(digest, hashlib.sha256((extracted / name).read_bytes()).hexdigest())
        self.assertTrue((extracted / 'project/4-configure.yml').is_file())
        self.assertTrue((extracted / 'runner.tar').is_file())
        self.assertTrue((extracted / 'install.sh').stat().st_mode & 0o111)
        self.assertTrue((extracted / 'guest/sophon-deploy').stat().st_mode & 0o111)
        for name in ('sophon-network', 'sophon-set-password', 'host-bootstrap.sh'):
            self.assertTrue((extracted / 'guest' / name).stat().st_mode & 0o111, name)
        self.assertTrue((extracted / 'tools/busybox').stat().st_mode & 0o111)
        self.assertEqual((extracted / 'project/bootstrap/portainer-image').read_text(),
                         'docker.io/portainer/portainer-ee:3.0.0\n')


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is not installed')
class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.prepare-test-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, content='source'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_playbooks_parse_without_credentials(self):
        for playbook in ('1-prepare.yml', '2-provision.yml', '4-configure.yml'):
            result = subprocess.run(['ansible-playbook', playbook, '--syntax-check'], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, playbook + result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('xorriso'), 'xorriso is required')
    def test_cached_prepare_never_invokes_network_tools(self):
        shutil.copytree(ROOT / 'roles/prepare', self.root / 'roles/prepare')
        self.write('src/bundle.py', (ROOT / 'src/bundle.py').read_text())
        self.write('src/installer_iso.py', (ROOT / 'src/installer_iso.py').read_text())
        self.write('guest/sophon-autoinstall')
        for name in bundle.PROJECT:
            if Path(name).suffix:
                self.write(name)
            else:
                self.write(f'{name}/tasks/main.yml')
        for name in (*bundle.GUEST, 'install.sh'):
            self.write(f'guest/{name}')
        self.write('ansible.cfg', '[defaults]\nhost_key_checking=False\n')
        live_iso = self.write('artifacts/coreos/fedora-coreos-live.iso', 'cached-stock-live-iso')
        self.write('artifacts/coreos/44.20260928.3.2.json', json.dumps({
            'images': {'live-iso': {
                'path': 'fedora-coreos-44.20260928.3.2-live-iso.x86_64.iso',
                'sha256': hashlib.sha256(live_iso.read_bytes()).hexdigest(),
            }}}))
        self.write('Containerfile', 'FROM scratch\n')
        self.write('artifacts/bootstrap/runner.tar', 'cached-runner')
        self.write('artifacts/bootstrap/runner.tar.id', 'sha256:cached')
        self.write('artifacts/bootstrap/busybox-1.38.0-musl', 'cached-helper')
        for name in ('portainer-portainer-ee_3.0.0.tar', 'keycloak-keycloak_26.8.1.tar', 'coredns-dockerdiscovery.tar'):
            self.write('artifacts/containers/' + name, name)
        # Building and inspecting the local runner image is allowed; pulls, saves and git are not.
        forbidden = self.root / 'fake-bin'
        forbidden.mkdir()
        attempted = self.root / 'network-attempted'
        image_id = self.write('image-id', 'sha256:cached')
        (forbidden / 'docker').write_text('\n'.join([
            '#!/bin/sh',
            'case "$1 $2" in',
            f'  "image inspect") cat "{image_id}"; exit 0 ;;',
            '  "build "*) exit 0 ;;',
            'esac',
            f'echo "$1" >> "{attempted}"',
            'exit 99', '']))
        (forbidden / 'git').write_text(f'#!/bin/sh\necho git >> "{attempted}"\nexit 99\n')
        # coreos-installer only customises a local file; it never touches the network.
        (forbidden / 'coreos-installer').write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\necho customised > "$out"\n')
        for name in ('docker', 'git', 'coreos-installer'):
            (forbidden / name).chmod(0o755)
        playbook = self.write('cached-prepare.yml', json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
            'vars': {
                'artifacts_dir': str(self.root / 'artifacts'),
                'sophon_containers_dir': str(self.root / 'artifacts/containers'),
                'coreos_iso_path': str(live_iso),
                'installer_iso_path': str(self.root / 'artifacts/coreos/sophon-installer.iso'),
                'bundle_iso_path': str(self.root / 'artifacts/bootstrap/sophon-deployment.iso'),
                'portainer_image': 'docker.io/portainer/portainer-ee:3.0.0',
                'portainer_image_tarname': 'portainer-portainer-ee_3.0.0.tar',
                'prepare_coredns_tar': str(self.root / 'artifacts/containers/coredns-dockerdiscovery.tar'),
                'prepare_container_images': [
                    'docker.io/portainer/portainer-ee:3.0.0', 'quay.io/keycloak/keycloak:26.8.1'],
            },
            'tasks': [{'ansible.builtin.import_role': {'name': 'prepare'}}],
        }]))
        environment = dict(os.environ, ANSIBLE_CONFIG=str(self.root / 'ansible.cfg'),
                           ANSIBLE_ROLES_PATH=str(self.root / 'roles'),
                           PATH=str(forbidden) + os.pathsep + os.environ['PATH'])

        def run():
            return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(playbook)], cwd=self.root,
                                  env=environment, capture_output=True, text=True, timeout=60)

        result = run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(attempted.exists(), attempted.read_text() if attempted.exists() else '')
        self.assertTrue((self.root / 'artifacts/bootstrap/sophon-deployment.iso').is_file())
        self.assertEqual((self.root / 'artifacts/coreos/sophon-installer.iso').read_text().strip(), 'customised')
        self.assertEqual(live_iso.read_text(), 'cached-stock-live-iso', 'the stock ISO must stay unchanged')
        manifest = json.loads((self.root / 'artifacts/containers/manifest.json').read_text())
        self.assertEqual([Path(path).name for path in manifest], [
            'portainer-portainer-ee_3.0.0.tar', 'keycloak-keycloak_26.8.1.tar', 'coredns-dockerdiscovery.tar'])
        # A changed runner image must be re-saved (saving is the only thing that should be attempted).
        image_id.write_text('sha256:rebuilt')
        changed = run()
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(attempted.read_text().splitlines(), ['save'], changed.stdout + changed.stderr)


if __name__ == '__main__':
    unittest.main()
