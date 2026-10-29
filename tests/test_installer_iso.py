import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import installer_iso


class InstallerIsoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.stock = self.directory / 'fedora-coreos-live.iso'
        self.stock.write_bytes(b'stock live iso')
        self.output = self.directory / 'out/sophon-installer.iso'
        self.record = self.directory / 'record.json'
        bin_dir = self.directory / 'bin'
        bin_dir.mkdir()
        # Stands in for coreos-installer: records its arguments and the Ignition config, writes the "customised" ISO.
        fake = bin_dir / 'coreos-installer'
        fake.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys\nfrom pathlib import Path\n'
            'a = sys.argv[1:]\n'
            f'Path({str(self.record)!r}).write_text(json.dumps(dict(args=a, ignition=Path(a[a.index("--live-ignition") + 1]).read_text())))\n'
            'if os.environ.get("FAKE_FAIL"): sys.exit(3)\n'
            'Path(a[a.index("-o") + 1]).write_bytes(Path(a[-1]).read_bytes() + b"+ignition")\n')
        fake.chmod(0o755)
        environment = patch.dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
        environment.start()
        self.addCleanup(environment.stop)

    def test_live_ignition_runs_the_autoinstaller_as_an_enabled_unit(self):
        ignition = installer_iso.live_ignition()
        self.assertEqual(ignition['ignition']['version'], '3.4.0')
        files = {item['path']: item for item in ignition['storage']['files']}
        script = files['/usr/local/libexec/sophon-autoinstall']
        self.assertEqual(script['mode'], 0o755)
        self.assertEqual(base64.b64decode(script['contents']['source'].split(',', 1)[1]).decode(),
                         (ROOT / 'guest/sophon-autoinstall').read_text())
        unit, = ignition['systemd']['units']
        self.assertEqual(unit['name'], 'sophon-install.service')
        self.assertTrue(unit['enabled'])
        self.assertIn('ExecStart=/usr/local/libexec/sophon-autoinstall', unit['contents'])
        # Only the live system runs it, never an installed one.
        self.assertIn('ConditionPathExists=/run/ostree-live', unit['contents'])
        self.assertIn('StandardOutput=journal+console', unit['contents'])
        # Nothing secret and no network or password settings are baked into the ISO.
        text = json.dumps(ignition)
        for forbidden in ('passwordHash', 'sshAuthorizedKeys', 'acme', 'token'):
            self.assertNotIn(forbidden, text)

    def test_the_autoinstaller_waits_for_the_deployment_iso_and_runs_install_unattended(self):
        source = (ROOT / 'guest/sophon-autoinstall').read_text()
        self.assertIn('SOPHON_DEPLOY', source)
        self.assertIn('mount -o ro', source)
        self.assertIn('install.sh" --auto', source)

    def test_build_customises_the_stock_iso_atomically_and_leaves_the_stock_unchanged(self):
        self.assertTrue(installer_iso.build(self.stock, self.output))
        self.assertEqual(self.output.read_bytes(), b'stock live iso+ignition')
        self.assertEqual(self.stock.read_bytes(), b'stock live iso')
        recorded = json.loads(self.record.read_text())
        self.assertEqual(recorded['args'][:2], ['iso', 'customize'])
        self.assertEqual(recorded['args'][-1], str(self.stock))
        self.assertEqual(json.loads(recorded['ignition']), installer_iso.live_ignition())
        self.assertEqual([path.name for path in self.output.parent.iterdir() if path.name.startswith('.')], [])

    def test_unchanged_inputs_skip_the_rebuild_and_changes_or_force_rebuild(self):
        self.assertTrue(installer_iso.build(self.stock, self.output))
        self.record.unlink()
        self.assertFalse(installer_iso.build(self.stock, self.output))
        self.assertFalse(self.record.exists(), 'an up-to-date ISO must not run coreos-installer again')
        self.assertTrue(installer_iso.build(self.stock, self.output, force=True))
        self.record.unlink()
        self.stock.write_bytes(b'a newer stock iso')
        self.assertTrue(installer_iso.build(self.stock, self.output))
        self.assertEqual(self.output.read_bytes(), b'a newer stock iso+ignition')
        self.record.unlink()
        with patch.object(installer_iso, 'live_ignition', return_value={'ignition': {'version': '3.4.0'}}):
            self.assertTrue(installer_iso.build(self.stock, self.output))

    def test_failure_keeps_the_previous_iso_and_never_stamps(self):
        self.assertTrue(installer_iso.build(self.stock, self.output))
        self.stock.write_bytes(b'changed')
        with patch.dict(os.environ, FAKE_FAIL='1'), self.assertRaises(subprocess.CalledProcessError):
            installer_iso.build(self.stock, self.output)
        self.assertEqual(self.output.read_bytes(), b'stock live iso+ignition')
        self.assertTrue(installer_iso.build(self.stock, self.output))

    def test_missing_stock_symlink_overwrite_and_missing_tool_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Missing stock'):
            installer_iso.build(self.directory / 'absent.iso', self.output)
        link = self.directory / 'link.iso'
        link.symlink_to(self.stock)
        with self.assertRaisesRegex(ValueError, 'Missing stock'):
            installer_iso.build(link, self.output)
        with self.assertRaisesRegex(ValueError, 'must not overwrite'):
            installer_iso.build(self.stock, self.stock)
        with patch('installer_iso.shutil.which', return_value=None), self.assertRaisesRegex(ValueError, 'coreos-installer is missing'):
            installer_iso.build(self.stock, self.output)
        self.assertFalse(self.output.exists())

    def test_command_line_reports_built_then_up_to_date_and_failures(self):
        arguments = ['--stock', str(self.stock), '--output', str(self.output)]
        with patch('builtins.print') as shown:
            self.assertEqual(installer_iso.main(arguments), 0)
            self.assertEqual(installer_iso.main(arguments), 0)
        messages = [call.args[0] for call in shown.call_args_list]
        self.assertIn('built', messages[0])
        self.assertIn('up to date', messages[1])
        with patch('builtins.print'), patch.dict(os.environ, FAKE_FAIL='1'):
            self.assertEqual(installer_iso.main(arguments + ['--force']), 1)


if __name__ == '__main__':
    unittest.main()
