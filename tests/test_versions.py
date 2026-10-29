from pathlib import Path
import re
import json
import shutil
import tempfile
import unittest

import yaml

from test_deployment import run_fixture


ROOT = Path(__file__).resolve().parents[1]


def defaults(role):
    return yaml.safe_load((ROOT / 'roles' / role / 'defaults/main.yml').read_text())


class VersionTests(unittest.TestCase):
    def test_prepare_caches_exactly_the_deployed_upstream_images(self):
        variables = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
        traefik, gitea, keycloak, ldap = map(defaults, ('traefik', 'gitea', 'keycloak', 'openldap'))
        images = {
            variables['portainer_image_repo'] + ':' + variables['portainer_image_tag'],
            'docker.io/library/traefik:' + traefik['traefik_image_tag'],
            'docker.io/' + gitea['gitea_image_name'] + ':' + gitea['gitea_image_tag'],
            gitea['gitea_postgres_image'],
            keycloak['keycloak_image_name'] + ':' + keycloak['keycloak_image_tag'],
            keycloak['keycloak_postgres_image'],
            'docker.io/' + ldap['openldap_image_name'] + ':' + ldap['openldap_image_tag'],
            'docker.io/' + ldap['openldap_certs_dumper_image_name'] + ':' + ldap['openldap_certs_dumper_image_tag'],
        }
        prepared = set(defaults('prepare')['prepare_container_images'])
        prepared.remove('{{ portainer_image }}')
        prepared.add(variables['portainer_image_repo'] + ':' + variables['portainer_image_tag'])
        self.assertEqual(prepared, images)
        for image in images:
            pattern = (r'^\d+\.\d+-alpine\d+\.\d+$' if '/postgres:' in image
                       else r'^v?\d+\.\d+\.\d+$')
            self.assertRegex(image.rsplit(':', 1)[1], pattern)

    def test_toolchain_tags_are_exact_and_traefik_matches_the_service(self):
        text = (ROOT / 'Containerfile').read_text()
        images = re.findall(r'^(?:FROM |COPY --from=)(docker\.io/\S+)', text, re.MULTILINE)
        self.assertEqual(len(images), 4)
        for image in images:
            self.assertRegex(image.rsplit(':', 1)[1], r'^v?\d+\.\d+\.\d+(?:-[\w.-]+)?$')
        self.assertIn('docker.io/library/traefik:' + defaults('traefik')['traefik_image_tag'], images)

    def test_postgres_18_uses_the_supported_volume_root(self):
        for role in ('gitea', 'keycloak'):
            text = (ROOT / 'roles' / role / 'templates/docker-compose.yml.j2').read_text()
            self.assertIn(f'{role}_db_data:/var/lib/postgresql\n', text)
            self.assertNotIn(':/var/lib/postgresql/data', text)
            self.assertEqual(defaults(role)[role + '_postgres_image'], 'docker.io/library/postgres:18.6-alpine3.24')

    def test_custom_build_and_os_inputs_do_not_follow_release_channels(self):
        variables = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
        prepare = defaults('prepare')
        tasks = (ROOT / 'roles/prepare/tasks/main.yml').read_text()
        self.assertRegex(variables['coredns_plugin_revision'], r'^[0-9a-f]{40}$')
        self.assertIn('version: "{{ coredns_plugin_revision }}"', tasks)
        self.assertNotIn('streams/stable.json', prepare['prepare_coreos_metadata_url'])
        self.assertIn('{{ prepare_coreos_version }}', prepare['prepare_coreos_metadata_url'])
        self.assertIn('{{ coredns_image_tag }}', prepare['prepare_coredns_tar'])
        self.assertIn("prepare_busybox_image.split(':')[-1]", prepare['prepare_busybox_path'])
        self.assertNotIn(':latest', prepare['prepare_coredns_image'])

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required to resolve archive names')
    def test_every_deployed_archive_is_in_the_prepare_manifest(self):
        variables = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
        for role in ('prepare', 'traefik', 'gitea', 'keycloak', 'openldap', 'coredns'):
            variables.update(defaults(role))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / 'archives.json'
            result = run_fixture(directory, [{
                'ansible.builtin.copy': {
                    'dest': str(output),
                    'content': "{{ {'prepared': prepare_image_tars + [prepare_coredns_tar | basename], "
                               "'deployed': [portainer_image_tarname] + traefik_image_tars + gitea_image_tars "
                               "+ keycloak_image_tars + openldap_image_tars + coredns_image_tars} | to_json }}",
                },
                'vars': variables,
            }])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archives = json.loads(output.read_text())
            self.assertEqual(set(archives['prepared']), {Path(path).name for path in archives['deployed']})


if __name__ == '__main__':
    unittest.main()
