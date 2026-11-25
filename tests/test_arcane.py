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
            self.assertIn('/run/user/1001/podman/podman.sock:/var/run/docker.sock', service['volumes'])
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

    def test_configure_gitops_ci_enforces_main_branch_protection_and_removes_runner_token_secret(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            'configure_gitops_ci', ROOT / 'roles/arcane/files/configure_gitops_ci.py'
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        cfg = {
            'gitea_url': 'https://git.step4.example',
            'gitea_org': 'ops',
            'gitea_repo': 'workloads',
            'gitea_admin_user': 'sophon-admin',
            'gitea_admin_password': 'secret',
            'ci_client_id': 'ops-gitops-ci',
        }

        # 1) Empty Day-1 repository must NOT create branch protection yet (so initial seed push succeeds),
        #    but MUST enforce private org/repo and restrict `ops` `Owners` strictly to Gitea admins.
        calls_empty: list[tuple[str, str, object]] = []

        def fake_http_empty(method, url, *, ctx, headers=None, json_body=None, form_body=None, expected=(200, 201, 204)):
            calls_empty.append((method, url, json_body))
            if method == 'GET' and url.endswith('/api/v1/orgs/ops'):
                return 200, {'username': 'ops', 'visibility': 'private', 'repo_admin_change_team_access': False}
            if method == 'GET' and url.endswith('/api/v1/repos/ops/workloads'):
                return 200, {'name': 'workloads', 'empty': True, 'private': True}
            if method == 'GET' and '/api/v1/admin/users' in url:
                return 200, [
                    {'login': 'sophon-admin', 'is_admin': True, 'active': True},
                    {'login': 'john.doe.admin', 'is_admin': True, 'active': True},
                    {'login': 'john.doe', 'is_admin': False, 'active': True},
                ]
            if method == 'GET' and '/api/v1/orgs/ops/teams' in url:
                return 200, [
                    {'id': 1, 'name': 'Owners'},
                    {'id': 2, 'name': 'Developers'},
                ]
            if method == 'DELETE' and url.endswith('/api/v1/teams/2'):
                return 204, None
            if method == 'GET' and '/api/v1/teams/1/members' in url:
                return 200, [{'login': 'sophon-admin'}, {'login': 'john.doe'}]
            if method == 'PUT' and url.endswith('/api/v1/teams/1/members/john.doe.admin'):
                return 204, None
            if method == 'DELETE' and url.endswith('/api/v1/teams/1/members/john.doe'):
                return 204, None
            if method == 'GET' and '/api/v1/repos/ops/workloads/collaborators' in url:
                return 200, [{'login': 'john.doe'}]
            if method == 'DELETE' and url.endswith('/api/v1/repos/ops/workloads/collaborators/john.doe'):
                return 204, None
            if method == 'POST' and url.endswith('/actions/runners/registration-token'):
                return 200, {'token': 'reg-tok-1'}
            if method == 'GET' and '/actions/secrets' in url:
                return 200, [{'name': 'ACT_RUNNER_REGISTRATION_TOKEN'}]
            raise AssertionError(f'Unexpected call: {method} {url}')

        actions_empty: list[str] = []
        with patch.object(mod, 'http_json', side_effect=fake_http_empty):
            reg_tok, secrets = mod.reconcile_gitea_org_and_token(cfg, None, actions_empty)
        self.assertEqual(reg_tok, 'reg-tok-1')
        self.assertIn('ACT_RUNNER_REGISTRATION_TOKEN', secrets)
        self.assertEqual(
            actions_empty,
            [
                'gitea:deleted_non_owner_team:ops/Developers',
                'gitea:added_ops_owner:john.doe.admin',
                'gitea:removed_non_admin_ops_owner:john.doe',
                'gitea:removed_direct_collaborator:ops/workloads:john.doe',
            ],
        )
        self.assertFalse(any('/branch_protections' in u for _, u, _ in calls_empty))

        # 2) Non-empty repository creates `main` branch protection (whitelisting `Owners` approvals)
        #    and updates drifted rules idempotently.
        bp_state: dict | None = None

        def fake_http_populated(method, url, *, ctx, headers=None, json_body=None, form_body=None, expected=(200, 201, 204)):
            nonlocal bp_state
            if method == 'GET' and url.endswith('/api/v1/orgs/ops'):
                return 200, {'username': 'ops', 'visibility': 'private', 'repo_admin_change_team_access': False}
            if method == 'GET' and url.endswith('/api/v1/repos/ops/workloads'):
                return 200, {'name': 'workloads', 'empty': False, 'private': True}
            if method == 'GET' and '/api/v1/admin/users' in url:
                return 200, [{'login': 'sophon-admin', 'is_admin': True, 'active': True}]
            if method == 'GET' and '/api/v1/orgs/ops/teams' in url:
                return 200, [{'id': 1, 'name': 'Owners'}]
            if method == 'GET' and '/api/v1/teams/1/members' in url:
                return 200, [{'login': 'sophon-admin'}]
            if method == 'GET' and '/api/v1/repos/ops/workloads/collaborators' in url:
                return 200, []
            if method == 'GET' and url.endswith('/branch_protections/main'):
                return (404, None) if bp_state is None else (200, bp_state)
            if method in ('POST', 'PATCH') and '/branch_protections' in url:
                bp_state = dict(json_body)
                return 200, bp_state
            if method == 'POST' and url.endswith('/actions/runners/registration-token'):
                return 200, {'token': 'reg-tok-2'}
            if method == 'GET' and '/actions/secrets' in url:
                return 200, []
            raise AssertionError(f'Unexpected call: {method} {url}')

        actions_create: list[str] = []
        with patch.object(mod, 'http_json', side_effect=fake_http_populated):
            mod.reconcile_gitea_org_and_token(cfg, None, actions_create)
        self.assertEqual(actions_create, ['gitea:created_branch_protection:ops/workloads:main'])
        self.assertIsNotNone(bp_state)
        self.assertFalse(bp_state['enable_push'])
        self.assertEqual(bp_state['required_approvals'], 1)
        self.assertTrue(bp_state['enable_approvals_whitelist'])
        self.assertEqual(bp_state['approvals_whitelist_teams'], ['Owners'])
        self.assertTrue(bp_state['enable_status_check'])
        self.assertEqual(bp_state['status_check_contexts'], mod.GITEA_MAIN_REQUIRED_CHECKS)

        # Second run with identical protection is a clean no-op; drifted protection triggers PATCH.
        actions_noop: list[str] = []
        with patch.object(mod, 'http_json', side_effect=fake_http_populated):
            mod.reconcile_gitea_org_and_token(cfg, None, actions_noop)
        self.assertEqual(actions_noop, [])

        bp_state['enable_push'] = True
        actions_drift: list[str] = []
        with patch.object(mod, 'http_json', side_effect=fake_http_populated):
            mod.reconcile_gitea_org_and_token(cfg, None, actions_drift)
        self.assertEqual(actions_drift, ['gitea:updated_branch_protection:ops/workloads:main'])
        self.assertFalse(bp_state['enable_push'])


if __name__ == '__main__':
    unittest.main()
