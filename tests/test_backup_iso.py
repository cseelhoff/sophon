import gzip
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import backup_iso


class BackupIsoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_apkovl_is_deterministic_secret_free_and_wires_autostart(self):
        first = backup_iso.build_apkovl()
        second = backup_iso.build_apkovl()
        self.assertEqual(first, second)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(first)), mode='r') as tar:
            members = {m.name: m for m in tar.getmembers()}
            self.assertIn('etc/.default_boot_services', members)
            self.assertIn('etc/apk/world', members)
            self.assertEqual(members['etc/runlevels/default/local'].linkname, '/etc/init.d/local')
            self.assertEqual(members['etc/local.d/sophon-backup-install.start'].mode & 0o777, 0o755)
            self.assertEqual(members['usr/local/libexec/sophon-backup-install'].mode & 0o777, 0o755)
            installer_bytes = tar.extractfile('usr/local/libexec/sophon-backup-install').read()
            self.assertEqual(installer_bytes, (ROOT / 'guest/backup-appliance/install.sh').read_bytes())
            for name, member in members.items():
                if member.isfile():
                    text = tar.extractfile(name).read().decode('utf-8', 'replace')
                    for forbidden in ('PRIVATE KEY', 'passwordHash', 'authorized_keys/root'):
                        self.assertNotIn(forbidden, text)

    @unittest.skipUnless(shutil.which('xorriso'), 'xorriso is required')
    def test_build_embeds_apkovl_and_os_archive_and_caches_with_stamp(self):
        seed = self.directory / 'seed'
        seed.mkdir()
        (seed / '.alpine-release').write_text('3.24.4\n')
        stock = self.directory / 'alpine-virt.iso'
        subprocess.run(['xorriso', '-as', 'mkisofs', '-quiet', '-o', str(stock), str(seed)], check=True)
        os_archive = self.directory / 'sophon-backup-os.tar.gz'
        os_archive.write_bytes(b'prebuilt-os-archive')
        output = self.directory / 'out/sophon-sftp-server.iso'

        self.assertTrue(backup_iso.build(stock, os_archive, output))
        self.assertFalse(backup_iso.build(stock, os_archive, output))
        self.assertTrue(backup_iso.build(stock, os_archive, output, force=True))

        extracted = self.directory / 'extracted'
        extracted.mkdir()
        subprocess.run([
            'xorriso', '-osirrox', 'on', '-indev', str(output),
            '-extract', '/sophon-backup-os.tar.gz', str(extracted / 'os.tar.gz'),
            '-extract', '/localhost.apkovl.tar.gz', str(extracted / 'ovl.tar.gz'),
        ], check=True, capture_output=True)
        self.assertEqual((extracted / 'os.tar.gz').read_bytes(), b'prebuilt-os-archive')
        self.assertEqual((extracted / 'ovl.tar.gz').read_bytes(), backup_iso.build_apkovl())

        os_archive.write_bytes(b'updated-os-archive')
        self.assertTrue(backup_iso.build(stock, os_archive, output))

    def test_guest_backup_scripts_parse_and_validate_ipv4_and_keys(self):
        scripts = ROOT / 'guest/backup-appliance'
        for path in scripts.iterdir():
            result = subprocess.run(['sh', '-n', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f'{path.name}: {result.stderr}')

        # Verify sophon-authorize-key + sophon-backup-apply key normalization and validation
        if not shutil.which('ssh-keygen'):
            self.skipTest('ssh-keygen required')
        key_file = self.directory / 'client_ed25519'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key_file)], check=True)
        pub = (key_file.with_suffix('.pub')).read_text().strip()

        fake_root = self.directory / 'fake-etc'
        auth_dir = fake_root / 'authorized_keys'
        auth_dir.mkdir(parents=True)
        bin_dir = self.directory / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'sshd').write_text('#!/bin/sh\nexit 0\n')
        (bin_dir / 'sshd').chmod(0o755)

        apply_src = (scripts / 'sophon-backup-apply').read_text()
        apply_src = apply_src.replace('/etc/ssh/authorized_keys', str(auth_dir))
        apply_src = apply_src.replace('/usr/sbin/sshd', str(bin_dir / 'sshd'))
        apply_src = apply_src.replace('chown root:root "$tmp"\n', '')
        apply_script = self.directory / 'sophon-backup-apply'
        apply_script.write_text(apply_src)
        apply_script.chmod(0o755)

        auth_src = (scripts / 'sophon-authorize-key').read_text()
        auth_src = auth_src.replace('/usr/local/sbin/sophon-backup-apply', str(apply_script))
        auth_src = auth_src.replace('doas ', '')
        auth_script = self.directory / 'sophon-authorize-key'
        auth_script.write_text(auth_src)
        auth_script.chmod(0o755)

        # Accepts bare ssh-ed25519 key and wraps with restrict ... sophon-backup
        ok = subprocess.run([str(auth_script), pub], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        written = (auth_dir / 'sophon-backup').read_text().strip()
        self.assertTrue(written.startswith('restrict ssh-ed25519 '))
        self.assertTrue(written.endswith(' sophon-backup'))

        # Rejects invalid key material leaving existing key intact
        bad = subprocess.run([str(auth_script), 'restrict ssh-ed25519 not-a-valid-key sophon-backup'],
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual((auth_dir / 'sophon-backup').read_text().strip(), written)

        # Authorizing a read-only restore key (via --readonly or sophon-restore comment) writes sophon-restore
        # without overwriting Production's sophon-backup authorized_keys file.
        restore_key_file = self.directory / 'restore_ed25519'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(restore_key_file)], check=True)
        restore_pub_parts = (restore_key_file.with_suffix('.pub')).read_text().strip().split()
        restore_line = f'restrict {restore_pub_parts[0]} {restore_pub_parts[1]} sophon-restore'

        ro_auto = subprocess.run([str(auth_script), restore_line], capture_output=True, text=True)
        self.assertEqual(ro_auto.returncode, 0, ro_auto.stderr)
        self.assertEqual((auth_dir / 'sophon-backup').read_text().strip(), written)
        self.assertEqual((auth_dir / 'sophon-restore').read_text().strip(), restore_line)

        restore_key_file2 = self.directory / 'restore2_ed25519'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(restore_key_file2)], check=True)
        restore_pub2 = (restore_key_file2.with_suffix('.pub')).read_text().strip()
        ro_flag = subprocess.run([str(auth_script), '--readonly', restore_pub2], capture_output=True, text=True)
        self.assertEqual(ro_flag.returncode, 0, ro_flag.stderr)
        self.assertEqual((auth_dir / 'sophon-backup').read_text().strip(), written)
        self.assertTrue((auth_dir / 'sophon-restore').read_text().strip().endswith(' sophon-restore'))

    def test_backup_appliance_containerfile_provisions_readonly_restore_sftp_account(self):
        containerfile = (ROOT / 'roles/prepare/files/backup-appliance.Containerfile').read_text()
        self.assertIn('sophon-restore', containerfile)
        self.assertIn('Match User sophon-restore', containerfile)
        self.assertIn('AuthorizedKeysFile /etc/ssh/authorized_keys/sophon-restore', containerfile)
        self.assertIn('ForceCommand internal-sftp -R', containerfile)
        self.assertIn('/etc/periodic/weekly/sophon-fstrim', containerfile)
        self.assertIn('/sbin/fstrim -av', containerfile)
        self.assertIn('default:crond', containerfile)

        installer_sh = (ROOT / 'guest/backup-appliance/install.sh').read_text()
        self.assertIn('mkfs.ext4 -q -F -m 1 -i 65536 -L SOPHON_BK_DATA "$data_part"', installer_sh)

        proxmox_bk = (ROOT / 'roles/proxmox/tasks/backup_vm.yml').read_text()
        self.assertIn('virtio0: "{{ proxmox_disk_storage_id }}:{{ backup_vm_disk_gb }},discard=on"', proxmox_bk)


if __name__ == '__main__':
    unittest.main()
