import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.bundle-test-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'repo'
        self.root.mkdir()
        for name in bundle.REQUIRED_FILES:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text('fixture\n')
        self.tools = self.write('artifacts/bootstrap/busybox', 'static-helper-fixture')
        (self.root / 'requirements.yml').write_text(
            'collections:\n' + ''.join(f'  - name: {name}\n' for name in bundle.ESSENTIAL_COLLECTIONS))
        self.collections = self.directory / 'collections'
        for name in bundle.ESSENTIAL_COLLECTIONS:
            collection = self.collections / 'ansible_collections' / Path(*name.split('.'))
            collection.mkdir(parents=True)
            (collection / 'MANIFEST.json').write_text('{}\n')
        self.runner = self.directory / 'runner.tar'
        self.runner.write_bytes(b'runner-image')
        self.containers = [self.directory / name for name in (
            'portainer-portainer-ee_2.41.0.tar', 'keycloak-keycloak_26.5.tar',
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

    def create(self, environment=None):
        return bundle.create_bundle(self.root, self.output, self.runner, self.collections,
                                    self.containers, environment or {})

    def test_complete_allowlisted_snapshot_and_checksums(self):
        for name in ('scripts/run.sh', 'roles/example/tasks/main.yml', 'group_vars/all.yml',
                     'callback_plugins/progress.py', 'dashboard/index.html', 'inventory/localhost.yml',
                     'flake.lock', 'services.yml', 'roles/example/files/SHA256SUMS'):
            self.write(name)
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.assertEqual(self.create({'SOPHON_DOMAIN': 'example.test'}), self.output)
        self.assertEqual(self.output.read_bytes(), b'valid-iso')
        self.assertIn('project/bootstrap.yml', self.captured)
        self.assertIn('project/services.yml', self.captured)
        self.assertIn('project/roles/example/tasks/main.yml', self.captured)
        for archive in self.containers:
            self.assertIn('project/artifacts/containers/' + archive.name, self.captured)
        self.assertIn('project/artifacts/collections/ansible_collections/community/proxmox/MANIFEST.json',
                      self.captured)
        self.assertEqual(json.loads(self.captured['project/site.json']),
                         {})
        checksums = {}
        for line in self.captured['SHA256SUMS'].decode().splitlines():
            digest, name = line.split('  ', 1)
            checksums[name] = digest
        self.assertEqual(set(checksums), self.captured.keys() - {'SHA256SUMS'})
        for name, digest in checksums.items():
            self.assertEqual(digest, hashlib.sha256(self.captured[name]).hexdigest())
        self.assertFalse(list(self.output.parent.glob('.sophon-bundle-*')))

    def test_secrets_and_unallowlisted_artifacts_never_copied(self):
        for name in (
            '.env', '.git/config', 'unknown.txt', 'site.json', 'artifacts/deployment/events.json',
            'artifacts/controller-vm/controller.qcow2', 'artifacts/containers/not-declared.tar',
            'roles/test/.env', 'roles/test/credentials.json', 'roles/test/secrets.yml',
            'roles/test/id_ed25519', 'roles/test/config.pem', 'roles/test/artifacts/deployment/key',
            'roles/test/logs/run.json', 'roles/test/tailscale/state', 'dashboard/.git/config',
        ):
            self.write(name, 'PRIVATE_MARKER')
        environment = {name: 'safe' for name in bundle.SITE_ENV}
        environment.update({
            'SOPHON_PROXMOX_HOST': 'PRIVATE_MARKER',
            'SOPHON_PROXMOX_PASSWORD': 'PRIVATE_MARKER',
            'SOPHON_CF_TOKEN': 'PRIVATE_MARKER',
            'SOPHON_PORTAINER_LICENSE_KEY': 'PRIVATE_MARKER',
            'SOPHON_CONTROLLER_SSH_PUBLIC_KEY': 'PRIVATE_MARKER',
            'SOPHON_DOMAIN': 'PRIVATE_MARKER',
        })
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.create(environment)
        self.assertNotIn(b'PRIVATE_MARKER', b''.join(self.captured.values()))
        self.assertEqual(set(json.loads(self.captured['project/site.json'])), bundle.SITE_ENV)

    def test_laptop_settings_are_never_serialized(self):
        for value in ('line\nbreak', 'tab\tvalue', 'del\x7f', 'control\x85', 'a' * 4097, 12):
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(bundle.site_settings({'SOPHON_INFRAVM_IP': value}), {})
        self.assertEqual(bundle.site_settings({'SOPHON_PROXMOX_PASSWORD': '\n'}), {})

    def test_installed_site_fields_do_not_imply_iso_settings(self):
        import deployment
        self.assertFalse(bundle.SITE_ENV)
        self.assertIn('SOPHON_INFRAVM_IP', deployment.SITE_FIELDS)

    def test_missing_inputs_never_create_iso(self):
        inputs = [self.runner, self.containers[1], self.root / 'bootstrap.yml']
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
        for name in bundle.ESSENTIAL_COLLECTIONS:
            path = self.collections / 'ansible_collections' / Path(*name.split('.'))
            backup = path.with_name(path.name + '-backup')
            path.rename(backup)
            with self.assertRaises(ValueError):
                self.create()
            backup.rename(path)
        self.assertFalse(self.output.exists())

    def test_additional_required_collection_is_checked(self):
        self.write('requirements.yml', 'collections:\n  - name: example.extra\n')
        with self.assertRaisesRegex(ValueError, 'example/extra'):
            self.create()

    def test_symlink_files_directories_and_archives_rejected(self):
        target = self.write('unlisted-secret', 'PRIVATE')
        for name in ('roles/example/tasks/main.yml', 'dashboard', 'group_vars'):
            link = self.root / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target if link.suffix else target.parent, target_is_directory=not link.suffix)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Symlinks'):
                self.create()
            link.unlink()
        link = self.directory / 'linked.tar'
        link.symlink_to(self.containers[0])
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            bundle.create_bundle(self.root, self.output, self.runner, self.collections, [link], {})
        self.assertFalse(self.output.exists())

    def test_collection_symlinks_rejected(self):
        link = self.collections / 'ansible_collections/community/proxmox/leaked.py'
        link.symlink_to(self.runner)
        with patch('bundle.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'Symlinks'):
                self.create()
            run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_internal_collection_file_links_are_copied_as_regular_files(self):
        collection = self.collections / 'ansible_collections/community/proxmox'
        (collection / 'COPYING').write_text('Upstream collection licence\n')
        (collection / 'LICENSES').mkdir()
        (collection / 'LICENSES/GPL-3.0-or-later.txt').symlink_to('../COPYING')
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.create()
        self.assertEqual(
            self.captured['project/artifacts/collections/ansible_collections/community/proxmox/LICENSES/GPL-3.0-or-later.txt'],
            b'Upstream collection licence\n')

    def test_explicit_nonempty_unique_archive_list_required(self):
        for paths in ([], [self.containers[0], self.containers[0]], [self.runner.with_suffix('.gz')]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                bundle.create_bundle(self.root, self.output, self.runner, self.collections, paths, {})

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

    def test_cli_manifest_defaults_and_explicit_containers(self):
        self.write('artifacts/containers/manifest.json', json.dumps([str(path) for path in self.containers]))
        arguments = ['--root', str(self.root), '--output', str(self.output),
                     '--runner', str(self.runner), '--collections', str(self.collections)]
        with patch('bundle.subprocess.run', side_effect=self.encode_iso):
            self.assertEqual(bundle.main(arguments), 0)
            self.assertEqual(bundle.main(arguments + sum(
                (['--container', str(path)] for path in self.containers), [])), 0)
        self.write('artifacts/containers/manifest.json', '{"bad": "manifest"}')
        with patch('bundle.subprocess.run') as run:
            self.assertEqual(bundle.main(arguments), 1)
            run.assert_not_called()

    def test_controller_builder_runs_setup_in_rootfs_not_line_parser(self):
        spec = importlib.util.spec_from_file_location(
            'controller_builder', ROOT / 'roles/prestage/files/build-controller-image.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = module.adapted_builder(
            (ROOT / 'nfs_vm_build/build-alpine-image').read_text(),
            Path('/repo/roles/controller/files'), Path('/repo/work'))
        self.assertNotIn('mktemp', source)
        self.assertNotIn('while IFS= read', source)
        self.assertIn('cp -R /repo/roles/controller/files/.', source)
        self.assertIn('proot -0 -w / -r "$ROOTFS_DIR"', source)
        self.assertIn('/bin/sh /usr/local/share/sophon-controller/controller-setup.sh', source)
        result = subprocess.run(['bash', '-n'], input=source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is not installed')
    def test_prestage_task_entrypoints_parse_without_credentials(self):
        for entrypoint in ('main', 'bootstrap'):
            playbook = self.directory / (entrypoint + '.yml')
            playbook.write_text(
                '- hosts: localhost\n  connection: local\n  gather_facts: false\n'
                '  tasks:\n    - ansible.builtin.import_role:\n'
                f'        name: prestage\n        tasks_from: {entrypoint}\n')
            environment = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / 'roles'))
            result = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', str(playbook), '--syntax-check'],
                env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('ansible-playbook') and shutil.which('xorriso'),
                         'Ansible and xorriso are required')
    def test_cached_bootstrap_never_invokes_network_tools(self):
        shutil.copytree(ROOT / 'roles/prestage', self.root / 'roles/prestage')
        self.write('bundle.py', (ROOT / 'bundle.py').read_text())
        self.write('ansible.cfg', '[defaults]\nhost_key_checking=False\n')
        self.write('roles/controller/files/controller-setup.sh', '#!/bin/sh\nexit 99\n')
        live_iso = self.write('artifacts/coreos/fedora-coreos-live.iso', 'cached-stock-live-iso')
        self.write('artifacts/coreos/stable.json', json.dumps({
            'architectures': {'x86_64': {'artifacts': {'metal': {'formats': {'iso': {'disk': {
                'location': 'https://builds.coreos.fedoraproject.org/cached.iso',
                'sha256': hashlib.sha256(live_iso.read_bytes()).hexdigest(),
            }}}}}}}}))
        runner = self.write('artifacts/bootstrap/runner.tar', 'cached-runner')
        self.write('flake.lock', 'cached-lock')
        revision = hashlib.sha256(json.dumps([
            (self.root / 'flake.nix').read_text().rstrip(),
            (self.root / 'flake.lock').read_text().rstrip(),
        ]).encode()).hexdigest()
        self.write('artifacts/bootstrap/toolchain.sha256', revision)
        for archive in self.containers:
            self.write('artifacts/containers/' + archive.name, archive.read_text())
        shutil.copytree(self.collections, self.root / 'artifacts/collections')
        self.write('artifacts/collections/requirements.yml', (self.root / 'requirements.yml').read_text())
        forbidden = self.directory / 'forbidden-network-tools'
        forbidden.mkdir()
        attempted = self.directory / 'network-attempted'
        for name in ('nix', 'ansible-galaxy', 'podman', 'git', 'xz'):
            executable = forbidden / name
            executable.write_text(f'#!/bin/sh\necho {name} >> "{attempted}"\nexit 99\n')
            executable.chmod(0o755)
        import yaml
        task_file = self.root / 'roles/prestage/tasks/bootstrap.yml'
        fixture_tasks = yaml.safe_load(task_file.read_text())
        galaxy_task = next(task for task in fixture_tasks
                           if task['name'] == 'Vendor collections for disconnected service deployment')
        galaxy_task['ansible.builtin.command']['argv'][0] = str(forbidden / 'ansible-galaxy')
        build_task = next(task for task in fixture_tasks
                          if task['name'] == 'Build offline runner and static installer helper')
        build_task['ansible.builtin.command']['argv'][0] = str(forbidden / 'nix')
        task_file.write_text(json.dumps(fixture_tasks))
        playbook = self.write('cached-prestage.yml', json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
            'vars': {
                'sophon_containers_dir': str(self.root / 'artifacts/containers'),
                'prestage_coreos_stream_url': 'https://must-not-contact.invalid/stable.json',
                'portainer_image': 'docker.io/portainer/portainer-ee:2.41.0',
                'portainer_image_tarname': 'portainer-portainer-ee_2.41.0.tar',
                'prestage_container_images': [
                    'docker.io/portainer/portainer-ee:2.41.0', 'quay.io/keycloak/keycloak:26.5'],
            },
            'tasks': [{'ansible.builtin.import_role': {'name': 'prestage', 'tasks_from': 'bootstrap'}}],
        }]))
        environment = dict(os.environ, ANSIBLE_CONFIG=str(self.root / 'ansible.cfg'),
                           ANSIBLE_ROLES_PATH=str(self.root / 'roles'),
                           PATH=str(forbidden) + os.pathsep + os.environ['PATH'],
                           SOPHON_DOMAIN='cached.example.test')
        result = subprocess.run(
            ['ansible-playbook', '-i', 'localhost,', str(playbook)],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(attempted.exists(), attempted.read_text() if attempted.exists() else '')
        self.assertTrue((runner.parent / 'sophon-deployment.iso').is_file())
        with (self.root / 'requirements.yml').open('a') as requirements:
            requirements.write('# Updated requirements must invalidate the collection cache.\n')
        changed = subprocess.run(
            ['ansible-playbook', '-i', 'localhost,', str(playbook)],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(changed.returncode, 0)
        self.assertTrue(attempted.exists(), changed.stdout + changed.stderr)
        self.assertEqual(attempted.read_text().splitlines(), ['ansible-galaxy'])
        (self.root / 'requirements.yml').write_text(
            (self.root / 'artifacts/collections/requirements.yml').read_text())
        (self.root / 'flake.nix').write_text('changed-toolchain\n')
        attempted.unlink()
        changed_toolchain = subprocess.run(
            ['ansible-playbook', '-i', 'localhost,', str(playbook)],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(changed_toolchain.returncode, 0)
        self.assertTrue(attempted.exists(), changed_toolchain.stdout + changed_toolchain.stderr)
        self.assertEqual(set(attempted.read_text().splitlines()), {'nix'})

    @unittest.skipUnless(shutil.which('ansible-galaxy'), 'Ansible Galaxy is required')
    def test_vendoring_installs_even_when_collection_exists_globally(self):
        import yaml
        source = self.directory / 'collection-source'
        source.mkdir()
        (source / 'README.md').write_text('Offline collection fixture\n')
        (source / 'galaxy.yml').write_text(json.dumps({
            'namespace': 'sophontest', 'name': 'fixture', 'version': '1.0.0',
            'readme': 'README.md', 'authors': ['Sophon'], 'license': ['MIT'],
        }))
        built = subprocess.run(
            ['ansible-galaxy', 'collection', 'build', str(source), '--output-path', str(self.directory)],
            capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        archive = self.directory / 'sophontest-fixture-1.0.0.tar.gz'
        global_path = self.directory / 'global-collections'
        installed = subprocess.run(
            ['ansible-galaxy', 'collection', 'install', str(archive), '-p', str(global_path)],
            capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        requirements = self.write('requirements.yml', json.dumps({'collections': [{'name': str(archive)}]}))
        target = self.directory / 'offline-target'
        tasks = yaml.safe_load((ROOT / 'roles/prestage/tasks/bootstrap.yml').read_text())
        command = next(task['ansible.builtin.command']['argv'] for task in tasks
                       if task['name'] == 'Vendor collections for disconnected service deployment')
        command = [argument.replace('{{ playbook_dir }}/requirements.yml', str(requirements))
                   .replace('{{ prestage_collections_dir }}', str(target)) for argument in command]
        result = subprocess.run(command, capture_output=True, text=True,
                                env=dict(os.environ, ANSIBLE_COLLECTIONS_PATH=str(global_path)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((target / 'ansible_collections/sophontest/fixture/MANIFEST.json').is_file(),
                        result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('xorriso'), 'xorriso is not installed')
    def test_real_iso_label_tree_and_checksum_verification(self):
        self.create({'SOPHON_DOMAIN': 'example.test'})
        extracted = self.directory / 'extracted'
        result = subprocess.run(
            ['xorriso', '-osirrox', 'on', '-indev', str(self.output),
             '-pvd_info', '-extract', '/', str(extracted)],
            capture_output=True, text=True, check=True)
        self.assertIn('SOPHON_DEPLOY', result.stdout + result.stderr)
        for line in (extracted / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(digest, hashlib.sha256((extracted / name).read_bytes()).hexdigest())
        self.assertTrue((extracted / 'project/bootstrap.yml').is_file())
        self.assertTrue((extracted / 'runner.tar').is_file())
        self.assertTrue((extracted / 'install.sh').stat().st_mode & 0o111)
        self.assertTrue((extracted / 'tools/busybox').stat().st_mode & 0o111)
        self.assertEqual((extracted / 'project/bootstrap/portainer-image').read_text(),
                         'docker.io/portainer/portainer-ee:2.41.0\n')


if __name__ == '__main__':
    unittest.main()
