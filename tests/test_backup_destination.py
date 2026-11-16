import base64
import contextlib
import io
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import backup_destination
import deployment
from test_console import service_values


class DestinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / 'remote'
        self.remote.mkdir()
        self.values = service_values() | {
            'SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64': '', 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64': '',
            'SOPHON_KOPIA_SFTP_PORT': '2222'}
        self.hostkey = ('[backup.example.test]:2222 ssh-ed25519 '
                        'AAAAC3NzaC1lZDI1NTE5AAAAIAxbKk27AE7LRjceV9fbPsihoPZPu/60yUezahcwg2dv\n')
        self.sftp = Mock()
        self.sftp.__enter__ = Mock(return_value=self.sftp)
        self.sftp.__exit__ = Mock(return_value=False)
        self.sftp.stat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR)
        self.sftp.lstat.side_effect = lambda path: (self.remote / Path(path).name).lstat()
        self.sftp.open.side_effect = lambda path, mode: (self.remote / Path(path).name).open(
            'xb' if mode == 'wx' else mode)
        self.sftp.remove.side_effect = lambda path: (self.remote / Path(path).name).unlink()
        self.sftp.listdir.side_effect = lambda path: [p.name for p in self.remote.iterdir()]
        self.client = Mock()
        self.client.__enter__ = Mock(return_value=self.client)
        self.client.__exit__ = Mock(return_value=False)
        self.client.open_sftp.return_value = self.sftp
        self.initialized = False
        self.repository_error = None
        self.space_error = None
        self.operations = []
        self.answers = [True, True, True]
        self.before_create = lambda: None

    def process(self, argv, **kwargs):
        if argv[0] == 'ssh-keyscan':
            return subprocess.CompletedProcess(argv, 0, self.hostkey, '')
        if argv[0] == 'sftp':
            return subprocess.CompletedProcess(argv, int(bool(self.space_error)),
                                               'Size Used Avail (root) %Capacity\n8192 4096 4096 4096 50%\n',
                                               self.space_error or '')
        self.operations.append(argv[4])
        if argv[4] == 'create':
            self.initialized = True
            (self.remote / 'kopia.repository').touch()
        error = self.repository_error or ('' if self.initialized else backup_destination.NOT_INITIALIZED)
        return subprocess.CompletedProcess(argv, int(bool(error)), '', error)

    def prepare(self, unattended=False):
        def confirm(question):
            if 'encrypted repository?' in question:
                self.before_create()
            return self.answers.pop(0)
        with patch('backup_destination.paramiko.SSHClient', return_value=self.client), \
                patch('backup_destination.subprocess.run', side_effect=self.process), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            values = backup_destination.prepare(self.values, root=self.root,
                                               confirm=None if unattended else confirm)
        return values, output.getvalue()

    def test_pair_create_and_unattended_rerun_reuses_identity_and_never_recreates(self):
        values, output = self.prepare()
        self.assertIn('SHA256:JpOchlkuvzbCr/25m8YBvJycvLk3dVOlDYbHPvHMv2E', output)
        self.assertEqual(self.operations, ['connect', 'create', 'connect'])
        key = self.root / 'credentials/kopia/id_ed25519'
        identity = key.read_bytes()
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o700)
        self.assertNotIn(identity.decode(), output)
        self.assertEqual(base64.b64decode(values['SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64']), identity)
        self.assertFalse(list(self.remote.glob('.sophon-preflight-*')))
        self.operations.clear()
        self.prepare(unattended=True)
        self.assertEqual(self.operations, ['connect'])
        self.assertEqual(key.read_bytes(), identity)

    def test_declined_host_trust_never_authenticates(self):
        self.answers = [False]
        with self.assertRaisesRegex(ValueError, 'not approved'):
            self.prepare()
        self.client.connect.assert_not_called()

    def test_changed_host_key_cannot_be_approved_by_an_override(self):
        self.prepare()
        self.client.connect.reset_mock()
        key = backup_destination.Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH).decode()
        self.hostkey = '[backup.example.test]:2222 ' + public + '\n'
        self.values['SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64'] = base64.b64encode(self.hostkey.encode()).decode()
        with self.assertRaisesRegex(ValueError, 'override cannot replace'):
            self.prepare(unattended=True)
        self.client.connect.assert_not_called()

    def test_wrong_password_and_ambiguous_errors_never_create_even_if_approved(self):
        for error in ('invalid repository password FIXTURE_SECRET_VALUE',
                      backup_destination.NOT_INITIALIZED + '\nadditional failure'):
            with self.subTest(error=error):
                self.answers = [True, True]
                self.repository_error = error
                self.values['SOPHON_KOPIA_CREATE_REPOSITORY'] = 'true'
                with self.assertRaises(RuntimeError) as failure:
                    self.prepare()
                self.assertNotIn('FIXTURE_SECRET_VALUE', str(failure.exception))
                self.assertNotIn('create', self.operations)

    def test_new_repository_requires_confirmation_and_rechecks_empty_destination(self):
        self.answers = [True, True, False]
        with self.assertRaisesRegex(ValueError, 'explicit confirmation'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'explicit confirmation'):
            self.prepare(unattended=True)
        (self.remote / 'unrelated').touch()
        self.values['SOPHON_KOPIA_CREATE_REPOSITORY'] = 'true'
        with self.assertRaisesRegex(ValueError, 'not empty'):
            self.prepare(unattended=True)
        self.assertNotIn('create', self.operations)

    def test_unsupported_free_space_is_reported_but_auth_or_delete_failures_are_fatal(self):
        self.initialized = True
        self.space_error = 'Server does not support statvfs@openssh.com extension'
        _, output = self.prepare()
        self.assertIn('server does not report free space', output)
        self.sftp.remove.side_effect = PermissionError('delete denied')
        self.operations.clear()
        with self.assertRaisesRegex(PermissionError, 'delete denied'):
            self.prepare(unattended=True)
        self.assertEqual(self.operations, [])
        self.client.connect.side_effect = backup_destination.paramiko.AuthenticationException()
        with self.assertRaisesRegex(ValueError, 'authentication failed'):
            self.prepare(unattended=True)

    def test_repository_destination_changed_during_confirmation_is_not_initialized(self):
        self.before_create = lambda: (self.remote / 'unexpected').touch()
        with self.assertRaisesRegex(ValueError, 'changed during confirmation'):
            self.prepare()
        self.assertNotIn('create', self.operations)

    def test_imported_rsa4096_identity_is_validated_without_generation(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        material = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                     serialization.NoEncryption())
        self.values.update(SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64=base64.b64encode(material).decode(),
                           SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64=base64.b64encode(self.hostkey.encode()).decode())
        values, error = deployment.validate_settings(self.values, {}, 'configure')
        self.assertIsNone(error)
        self.values = values
        self.initialized = True
        _, output = self.prepare(unattended=True)
        self.assertFalse((self.root / 'credentials/kopia/id_ed25519').exists())
        self.assertNotIn(material.decode(), output)


if __name__ == '__main__':
    unittest.main()
