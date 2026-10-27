import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from deployment import new_control, validate_settings
from library import hyperv_vm
from library.hyperv_vm import MARKER, provision, script


def arguments_of(source):
    """Recover the JSON arguments a script was given (they travel as base64, never as PowerShell source)."""
    encoded = source.split("FromBase64String('")[1].split("'")[0]
    return json.loads(base64.b64decode(encoded))


class FakeHost:
    def __init__(self, found=None, sizes=None):
        self.found = found or {'count': 0, 'state': '', 'notes': ''}
        self.sizes = sizes or {}
        self.scripts = []
        self.uploads = []

    def run(self, source):
        self.scripts.append(source)
        args = arguments_of(source)
        if '$vms = @(Get-VM' in source:
            return self.found
        if 'Get-Item -LiteralPath' in source:
            return [{'name': name, 'size': self.sizes.get(name, -1)} for name in args['names']]
        return {}

    def upload(self, local, remote):
        self.uploads.append((local, remote))


class HyperVTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.iso = directory / 'stock.iso'
        self.iso.write_bytes(b'unchanged installer')
        self.bundle = directory / 'bundle.iso'
        self.bundle.write_bytes(b'reusable secret-free bundle')
        self.params = dict(name='sophon-test', host='hv.example', port=5985, username='Administrator',
                           password='PRIVATE_MANAGEMENT_PASSWORD', ca_file='', switch='External', path='',
                           cores=8, memory=16384, disk_gb=150,
                           coreos_iso=str(self.iso), bundle_iso=str(self.bundle))
        self.info = {'host': 'HV1', 'vm_path': 'D:\\Hyper-V'}

    def create_arguments(self, host):
        create = next(source for source in host.scripts if 'New-VM' in source)
        return arguments_of(create), create

    def test_fresh_vm_hardware_and_two_isos(self):
        host = FakeHost()
        result = provision(self.params, host, self.info)
        self.assertEqual(result, {'changed': True, 'created': True, 'vm_path': 'HV1/sophon-test'})
        self.assertEqual([upload[0] for upload in host.uploads], [str(self.iso), str(self.bundle)])
        for _, remote in host.uploads:
            self.assertTrue(remote.startswith('D:\\Hyper-V\\sophon-isos\\sophon-') and remote.endswith('.iso.part'))
        args, source = self.create_arguments(host)
        self.assertEqual((args['memory_mb'], args['disk_gb'], args['cores'], args['switch']), (16384, 150, 8, 'External'))
        self.assertEqual(args['path'], 'D:\\Hyper-V')
        self.assertEqual(args['marker'], MARKER)
        for fragment in ('-Generation 2', '-EnableSecureBoot Off', '-StaticMemory', '-BootOrder $disk, $installer',
                         "':preparing'", 'Start-VM'):
            self.assertIn(fragment, source)
        # The marker is final only after the hardware is complete.
        self.assertLess(source.index("':preparing'"), source.index('Set-VM -VM $vm -Notes $p.marker'))
        self.assertEqual(self.iso.read_bytes(), b'unchanged installer')

    def test_isos_are_hash_named_verified_then_attached(self):
        host = FakeHost()
        provision(self.params, host, self.info)
        finalize = [source for source in host.scripts if 'Get-FileHash' in source]
        self.assertEqual(len(finalize), 2)
        names = [arguments_of(source)['name'] for source in finalize]
        self.assertTrue(all(name.startswith('sophon-') and name.endswith('.iso') for name in names))
        self.assertEqual({len(arguments_of(source)['sha256']) for source in finalize}, {64})
        args, _ = self.create_arguments(host)
        self.assertEqual([args['coreos_name'], args['bundle_name']], names)
        self.assertNotEqual(names[0], names[1])

    def test_cached_isos_are_not_uploaded_again(self):
        first = FakeHost()
        provision(self.params, first, self.info)
        args, _ = self.create_arguments(first)
        second = FakeHost(sizes={args['coreos_name']: self.iso.stat().st_size,
                                 args['bundle_name']: self.bundle.stat().st_size})
        provision(self.params, second, self.info)
        self.assertEqual(second.uploads, [])
        self.assertFalse([source for source in second.scripts if 'Get-FileHash' in source])

    def test_a_wrong_sized_hash_named_iso_is_never_overwritten(self):
        first = FakeHost()
        provision(self.params, first, self.info)
        args, _ = self.create_arguments(first)
        host = FakeHost(sizes={args['coreos_name']: 3})
        with self.assertRaisesRegex(ValueError, 'unexpected size'):
            provision(self.params, host, self.info)
        self.assertEqual(host.uploads, [])
        self.assertFalse([source for source in host.scripts if 'New-VM' in source])

    def test_custom_folder_is_used_for_disk_and_isos(self):
        host = FakeHost()
        provision(dict(self.params, path='E:\\Sophon'), host, self.info)
        args, _ = self.create_arguments(host)
        self.assertEqual(args['path'], 'E:\\Sophon')
        self.assertEqual(args['iso_dir'], 'E:\\Sophon\\sophon-isos')

    def test_existing_marked_vm_is_only_started(self):
        host = FakeHost(found={'count': 1, 'state': 'Off', 'notes': MARKER})
        result = provision(self.params, host, self.info)
        self.assertEqual(result, {'changed': True, 'created': False, 'vm_path': 'HV1/sophon-test'})
        self.assertTrue(any('Start-VM' in source for source in host.scripts))
        self.assertFalse(any('New-VM' in source for source in host.scripts))
        self.assertEqual(host.uploads, [])

    def test_running_marked_vm_changes_nothing(self):
        host = FakeHost(found={'count': 1, 'state': 'Running', 'notes': MARKER})
        self.assertFalse(provision(self.params, host, self.info)['changed'])
        self.assertFalse(any('Start-VM' in source for source in host.scripts))

    def test_unmarked_incomplete_and_transitional_vms_are_not_adopted(self):
        for notes in ('', 'my production database', MARKER + ':preparing'):
            host = FakeHost(found={'count': 1, 'state': 'Off', 'notes': notes})
            with self.assertRaisesRegex(ValueError, 'unmanaged or an incomplete'):
                provision(self.params, host, self.info)
            self.assertFalse(any('Start-VM' in source for source in host.scripts))
        host = FakeHost(found={'count': 1, 'state': 'Saved', 'notes': MARKER})
        with self.assertRaisesRegex(ValueError, 'state is Saved'):
            provision(self.params, host, self.info)
        host = FakeHost(found={'count': 2, 'state': 'Off', 'notes': MARKER})
        with self.assertRaisesRegex(ValueError, 'Multiple VMs'):
            provision(self.params, host, self.info)

    def test_input_validation_happens_before_any_host_call(self):
        for change, message in ((dict(name='bad name; Stop-Computer'), 'VM name'), (dict(name='*'), 'VM name'),
                                (dict(disk_gb=10), '40 GiB'), (dict(memory=512), '40 GiB'),
                                (dict(path='relative\\folder'), 'absolute Windows path'),
                                (dict(path='D:\\x";Remove-Item'), 'absolute Windows path'),
                                (dict(coreos_iso='/nonexistent.iso'), 'Missing installation ISO')):
            host = FakeHost()
            with self.assertRaisesRegex(ValueError, message):
                provision(dict(self.params, **change), host, self.info)
            self.assertFalse(any('New-VM' in source or 'Start-VM' in source for source in host.scripts), change)
            self.assertEqual(host.uploads, [])

    def test_values_never_reach_powershell_source_and_password_never_does(self):
        host = FakeHost()
        provision(dict(self.params, switch="Ext'; Remove-Item C:\\ -Recurse; '"), host, self.info)
        for source in host.scripts:
            self.assertNotIn('Remove-Item C:', source)
            self.assertNotIn(self.params['password'], source)
        self.assertIn('Remove-Item C:', json.dumps([arguments_of(source) for source in host.scripts]))

    def test_script_helper_round_trips_arguments(self):
        source = script('$p.name', name="a'b\"c$(x)`d", n=1)
        self.assertEqual(arguments_of(source), {'name': "a'b\"c$(x)`d", 'n': 1})
        self.assertTrue(source.startswith("$ErrorActionPreference = 'Stop'"))

    def test_switch_name_validation(self):
        for good in ('External', 'Default Switch', 'vSwitch-1 (LAN)'):
            self.assertTrue(hyperv_vm.SWITCH.fullmatch(good), good)
        for bad in ('*', 'a?b', 'x[1]', '', 'tab\there'):
            self.assertFalse(hyperv_vm.SWITCH.fullmatch(bad), bad)


class HyperVSettingsTests(unittest.TestCase):
    def test_hyperv_settings_are_validated_and_scoped_to_the_provider(self):
        control = new_control({}, 'provision')
        current = control['values']
        base = {'SOPHON_HYPERVISOR': 'hyperv', 'SOPHON_HYPERV_HOST': 'hv.example',
                'SOPHON_HYPERV_PASSWORD': 'PRIVATE', 'SOPHON_HYPERV_SWITCH': 'External'}
        values, error = validate_settings(current, base, 'provision')
        self.assertIsNone(error, error)
        self.assertEqual(values['SOPHON_HYPERV_PORT'], '5985')
        self.assertEqual(values['SOPHON_HYPERV_USERNAME'], 'Administrator')
        _, error = validate_settings(current, dict(base, SOPHON_HYPERV_SWITCH=''), 'provision')
        self.assertTrue(error)
        _, error = validate_settings(current, dict(base, SOPHON_HYPERV_PORT='70000'), 'provision')
        self.assertTrue(error)
        _, error = validate_settings(current, dict(base, SOPHON_HYPERVISOR='xen'), 'provision')
        self.assertIn('hyperv', error)


if __name__ == '__main__':
    unittest.main()
