import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from test_deployment import ROOT, run_fixture

sys.path.insert(0, str(ROOT / 'src'))
import deployment


class ArcaneAndComposeTests(unittest.TestCase):
    def test_encryption_key_is_generated_once_with_private_permissions_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            first = deployment.ensure_arcane_encryption_key(root)
            self.assertRegex(first, r'^hex:[0-9a-f]{64}$')
            key_file = root / 'credentials/arcane/encryption_key'
            self.assertEqual(stat.S_IMODE(key_file.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
            self.assertEqual(deployment.ensure_arcane_encryption_key(root), first)

            key_file.write_text('not-a-valid-hex-key\n')
            with self.assertRaisesRegex(ValueError, 'malformed'):
                deployment.ensure_arcane_encryption_key(root)

            outside = Path(temporary) / 'outside'
            outside.write_text(first + '\n')
            key_file.unlink()
            key_file.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                deployment.ensure_arcane_encryption_key(root)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required')
    def test_arcane_stack_writes_declarative_compose_and_escaped_private_dotenv(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stacks = directory / 'stacks'
            archive = directory / 'getarcaneapp-manager_v3.0.1.tar'
            archive.write_bytes(b'fixture-archive')
            calls = directory / 'docker-calls.jsonl'
            docker = directory / 'bin/docker'
            docker.parent.mkdir()
            docker.write_text(
                f'#!{sys.executable}\n'
                'import json, sys\n'
                f'with open({str(calls)!r}, "a") as out:\n'
                '    out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                'if "inspect" in sys.argv:\n'
                '    print("healthy")\n'
                'elif "compose" in sys.argv:\n'
                '    sys.stderr.write("Container arcane Creating\\n")\n'
            )
            docker.chmod(0o755)
            variables = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
            variables.update(yaml.safe_load((ROOT / 'roles/arcane/defaults/main.yml').read_text()))
            tricky_secret = 'p$w"quote\'hash#space=slash\\end'
            variables.update(
                domain_name='Step4.Example.',
                keycloak_hostname='auth',
                keycloak_domain='step4.example',
                keycloak_realm='sophon',
                arcane_admin_password=tricky_secret,
                keycloak_arcane_client_secret='oidc-client-secret',
                arcane_encryption_key='hex:' + 'a' * 64,
                arcane_stacks_dir=str(stacks),
                compose_stacks_dir=str(stacks),
                compose_engine_host='unix:///fixture/podman.sock',
                arcane_image_tars=[str(archive)],
            )
            env = dict(os.environ, PATH=str(docker.parent) + os.pathsep + os.environ['PATH'],
                       ANSIBLE_ROLES_PATH=str(ROOT / 'roles'))
            with patch.dict(os.environ, env, clear=True):
                result = run_fixture(directory, [{'ansible.builtin.include_role': {'name': 'arcane'},
                                                  'vars': variables}])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('oidc-client-secret', result.stdout + result.stderr)
            self.assertNotIn('hex:' + 'a' * 64, result.stdout + result.stderr)

            stack_dir = stacks / 'arcane'
            self.assertEqual(stat.S_IMODE(stack_dir.stat().st_mode), 0o700)
            compose_file = stack_dir / 'compose.yaml'
            env_file = stack_dir / '.env'
            self.assertEqual(stat.S_IMODE(compose_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

            compose = yaml.safe_load(compose_file.read_text())
            service = compose['services']['arcane']
            self.assertEqual(service['pull_policy'], 'never')
            self.assertEqual(service['environment']['ARCANE_APP_URL'], 'https://arcane.step4.example')
            self.assertEqual(service['environment']['ARCANE_PROJECTS_DIRECTORY'], str(stacks))
            self.assertEqual(service['environment']['ARCANE_OIDC_ENABLED'], 'true')
            self.assertEqual(service['environment']['ARCANE_OIDC_ISSUER_URL'],
                             'https://auth.step4.example/realms/sophon')
            self.assertEqual(service['environment']['ARCANE_ANALYTICS_ENABLED'], 'false')
            self.assertEqual(service['environment']['ARCANE_UPDATE_CHECK_ENABLED'], 'false')
            self.assertIn('traefik.http.routers.arcane.rule=Host(`arcane.step4.example`)', service['labels'])

            dotenv = env_file.read_text()
            self.assertIn('ARCANE_DEFAULT_ADMIN_PASSWORD="p\\$w\\"quote\'hash#space=slash\\\\end"', dotenv)
            self.assertIn('arcane-admins', dotenv)
            self.assertIn('role_admin', dotenv)

            recorded = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(recorded[0], ['--host', 'unix:///fixture/podman.sock', 'image', 'load',
                                           '--quiet', '--input', str(archive)])
            self.assertIn('--remove-orphans', recorded[1])
            self.assertIn('never', recorded[1])


if __name__ == '__main__':
    unittest.main()
