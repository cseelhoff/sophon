import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from deployment import new_control, validate_settings
from library.vcenter_vm import MARKER, connection_environment, provision


class VCenterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.iso = directory / 'stock.iso'
        self.iso.write_bytes(b'unchanged installer')
        self.bundle = directory / 'bundle.iso'
        self.bundle.write_bytes(b'reusable secret-free bundle')
        self.params = dict(name='sophon-test', url='https://vcenter.example', username='administrator',
                           password='PRIVATE_MANAGEMENT_PASSWORD', ca_file='/private/ca.pem',
                           datacenter='DC', cluster='Cluster', datastore='Shared', network='VLAN51',
                           folder='', cores=8, memory=16384, disk_gb=150,
                           coreos_iso=str(self.iso), bundle_iso=str(self.bundle))
        self.calls = []
        self.paths = []
        self.listing = [{'file': []}]
        self.vm = {'config': {'annotation': MARKER, 'template': False},
                   'runtime': {'powerState': 'poweredOn'}}

    def run_govc(self, *arguments):
        self.calls.append(arguments)
        if arguments[0] == 'find':
            return '\n'.join(self.paths)
        if arguments[0] == 'vm.info':
            return json.dumps({'virtualMachines': [self.vm]})
        if arguments[0] == 'datastore.ls':
            return json.dumps(self.listing)
        if arguments[0] == 'device.cdrom.add':
            return 'cdrom-3001\n'
        return ''

    def test_fresh_vm_hardware_and_unchanged_two_isos(self):
        result = provision(self.params, self.run_govc)
        self.assertEqual(result, {'changed': True, 'created': True, 'vm_path': '/DC/vm/sophon-test'})
        uploads = [call for call in self.calls if call[0] == 'datastore.upload']
        self.assertEqual([call[1] for call in uploads], [str(self.iso), str(self.bundle)])
        self.assertTrue(all(call[2].startswith('sophon-isos/sophon-') for call in uploads))
        create = next(call for call in self.calls if call[0] == 'vm.create')
        for value in ('-on=false', '-disk.thick=false', 'vmxnet3', 'pvscsi', '150G', '16384', 'VLAN51'):
            self.assertIn(value, create)
        self.assertNotIn(self.params['password'], json.dumps(self.calls))
        self.assertEqual(self.iso.read_bytes(), b'unchanged installer')
        boot = next(call for call in self.calls if call[0] == 'device.boot')
        self.assertEqual(boot[-1], 'disk,cdrom')
        self.assertEqual(self.calls[-2], ('vm.change', '-vm', result['vm_path'], '-annotation', MARKER))
        self.assertEqual(self.calls[-1], ('vm.power', '-on', result['vm_path']))

    def test_existing_vm_preserves_disks_and_ejected_media(self):
        self.paths = ['/DC/vm/sophon-test']
        result = provision(self.params, self.run_govc)
        self.assertFalse(result['changed'])
        self.assertEqual([call[0] for call in self.calls], ['find', 'vm.info'])
        self.vm['runtime']['powerState'] = 'poweredOff'
        result = provision(self.params, self.run_govc)
        self.assertTrue(result['changed'])
        self.assertEqual(self.calls[-1], ('vm.power', '-on', self.paths[0]))

    def test_never_adopts_unmanaged_templates_or_partial_vms(self):
        self.paths = ['/DC/vm/sophon-test']
        for annotation, template in [('', False), ('other', False), (MARKER + ':preparing', False), (MARKER, True)]:
            with self.subTest(annotation=annotation, template=template):
                self.calls.clear()
                self.vm['config'].update(annotation=annotation, template=template)
                with self.assertRaisesRegex(ValueError, 'not been changed'):
                    provision(self.params, self.run_govc)
                self.assertEqual([call[0] for call in self.calls], ['find', 'vm.info'])

    def test_ambiguous_names_and_suspended_vms_are_not_changed(self):
        self.paths = ['/DC/vm/one/sophon-test', '/DC/vm/two/sophon-test']
        with self.assertRaisesRegex(ValueError, 'Multiple'):
            provision(self.params, self.run_govc)
        self.assertEqual(len(self.calls), 1)
        self.paths = self.paths[:1]
        self.vm['runtime']['powerState'] = 'suspended'
        with self.assertRaisesRegex(ValueError, 'suspended'):
            provision(self.params, self.run_govc)

    def test_hash_named_media_is_reused_but_never_overwritten(self):
        provision(self.params, self.run_govc)
        names = [call[2].split('/')[-1] for call in self.calls if call[0] == 'datastore.upload']
        self.listing = [{'file': [{'path': name, 'fileSize': path.stat().st_size}
                                  for name, path in zip(names, (self.iso, self.bundle))]}]
        self.calls.clear()
        provision(self.params, self.run_govc)
        self.assertNotIn('datastore.upload', [call[0] for call in self.calls])
        self.listing[0]['file'][0]['fileSize'] += 1
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            provision(self.params, self.run_govc)
        self.assertNotIn('vm.create', [call[0] for call in self.calls])

    def test_missing_media_and_invalid_names_fail_before_vm_creation(self):
        for changes in ({'name': '../existing'}, {'disk_gb': 39}, {'coreos_iso': '/missing.iso'}):
            self.calls.clear()
            with self.assertRaises(ValueError):
                provision(self.params | changes, self.run_govc)
            self.assertNotIn('vm.create', [call[0] for call in self.calls])

    def test_credentials_are_environment_only_and_tls_is_verified(self):
        environment = connection_environment(self.params)
        self.assertEqual(environment['GOVC_PASSWORD'], self.params['password'])
        self.assertEqual(environment['GOVC_INSECURE'], 'false')
        self.assertEqual(environment['GOVC_PERSIST_SESSION'], 'false')
        self.assertEqual(environment['GOVC_TLS_CA_CERTS'], '/private/ca.pem')
        for url in ('http://vcenter.example', 'https://user:secret@vcenter.example', 'https://vcenter.example/?token=secret'):
            with self.assertRaises(ValueError):
                connection_environment(self.params | {'url': url})

    def test_provider_specific_settings_and_step_boundary(self):
        values = new_control({}, 'provision')['values']
        changes = {'SOPHON_HYPERVISOR': 'vcenter', 'SOPHON_VCENTER_URL': self.params['url'],
                   'SOPHON_VCENTER_PASSWORD': self.params['password'],
                   'SOPHON_VCENTER_DATACENTER': 'DC', 'SOPHON_VCENTER_CLUSTER': 'Cluster',
                   'SOPHON_VCENTER_DATASTORE': 'Shared', 'SOPHON_VCENTER_NETWORK': 'VLAN51'}
        validated, error = validate_settings(values, changes)
        self.assertIsNone(error)
        self.assertEqual(validated['SOPHON_PROXMOX_PASSWORD'], '')
        for field in changes:
            invalid = changes | {field: ''}
            if field == 'SOPHON_HYPERVISOR':
                invalid[field] = 'unsupported'
            self.assertIsNotNone(validate_settings(values, invalid)[1], field)
        services = new_control(changes, 'configure')
        self.assertNotIn(self.params['password'], json.dumps(services))
        self.assertFalse(any(key.startswith('SOPHON_VCENTER_') for key in services['environment']))
        self.assertIsNotNone(validate_settings(values, changes | {'SOPHON_VCENTER_URL': 'http://untrusted'})[1])

    def test_selected_provider_stage_is_reported(self):
        from test_deployment import run_fixture
        import yaml
        bootstrap = yaml.safe_load((ROOT / '2-provision.yml').read_text())[-1]['tasks']
        tasks = [{'ansible.builtin.set_fact': {'hypervisor_provider': 'vcenter'}},
                 *bootstrap[0:2],
                 {'ansible.builtin.debug': {'msg': 'Done'}, 'tags': ['sophon_stage_end']}]
        result, report = run_fixture(Path(self.temporary.name), tasks, {
            'vcenter': '- ansible.builtin.debug:\n    msg: Authenticated\n'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report['stages']['vcenter']['state'], 'completed', report)
        self.assertEqual(report['stages']['proxmox']['state'], 'skipped', report)

    def test_invalid_provider_is_rejected_before_starting_the_dashboard(self):
        result = subprocess.run([sys.executable, str(ROOT / 'src/deployment.py'), '--step', 'provision', '--serve'],
                                env=dict(os.environ, SOPHON_HYPERVISOR='unsupported'),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('SOPHON_HYPERVISOR must be proxmox, vcenter or workstation', result.stderr)

    def test_ansible_module_documentation_is_valid(self):
        result = subprocess.run(['ansible-doc', '-M', str(ROOT / 'library'), 'vcenter_vm'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('thin-provisioned', result.stdout)


if __name__ == '__main__':
    unittest.main()
