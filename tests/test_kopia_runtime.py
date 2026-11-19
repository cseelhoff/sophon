import base64
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

FILES = Path(__file__).resolve().parents[1] / "roles/kopia/files"
sys.path.insert(0, str(FILES))
spec = importlib.util.spec_from_file_location("kopia_runtime", FILES / "runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def test_active_start_configures_native_policy_actions_and_triggers_initial_snapshot(self):
        for enabled in ("true", "false"):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "last-run.json").write_text('{"status":"stale"}')
                environment = dict(
                    os.environ, KOPIA_PASSWORD="fixture-repository-password",
                    KOPIA_SERVER_PASSWORD="fixture-ui-password", KOPIA_SFTP_HOST="dev.invalid",
                    KOPIA_SFTP_USERNAME="backup", KOPIA_SFTP_PATH="/dev-repository", KOPIA_SFTP_PORT="22",
                    KOPIA_CLIENT_USERNAME="backup", KOPIA_CLIENT_HOSTNAME="dev",
                    KOPIA_SFTP_PRIVATE_KEY_B64=base64.b64encode(b"fixture-key").decode(),
                    KOPIA_SFTP_KNOWN_HOSTS_B64=base64.b64encode(b"fixture-pins").decode(),
                    KOPIA_BACKUPS_ENABLED=enabled, KOPIA_SNAPSHOT_INTERVAL="6h", SOURCE_ROOT=temporary,
                    ENGINE_URL="http://dump-proxy:2375",
                )
                callbacks = {}
                server = Mock()
                server.poll.return_value = None

                def sleep(_):
                    callbacks[signal.SIGTERM](signal.SIGTERM, None)

                with patch.object(runtime, "Path", side_effect=lambda path:
                                  root / "credentials" if path == "/tmp/credentials" else Path(path)), \
                        patch.object(runtime.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], 0, b"", b"")) as commands, \
                        patch.object(runtime.subprocess, "Popen", return_value=server) as processes, \
                        patch.object(runtime.signal, "signal", side_effect=lambda sig, fn: callbacks.update({sig: fn})), \
                        patch.object(runtime.time, "sleep", side_effect=sleep):
                    runtime.start(environment)

                self.assertEqual(processes.call_count, 1)
                server_argv = processes.call_args.args[0]
                self.assertIn("--enable-actions", server_argv)
                self.assertNotIn("fixture-ui-password", " ".join(server_argv))
                self.assertEqual(processes.call_args.kwargs["env"]["KOPIA_SERVER_CONTROL_PASSWORD"], "fixture-ui-password")

                argvs = [call.args[0] for call in commands.call_args_list]
                for argv in argvs:
                    self.assertNotIn("fixture-ui-password", " ".join(argv))
                    self.assertNotIn("fixture-repository-password", " ".join(argv))
                self.assertIn("--enable-actions", argvs[0])
                source_policy = next(a for a in argvs if "policy" in a and "/sophon-state" in a)
                self.assertIn("--before-snapshot-root-action=/app/backup.py", source_policy)
                self.assertIn("--after-snapshot-root-action=/app/backup.py", source_policy)
                self.assertIn("--action-command-mode=essential", source_policy)
                self.assertIn(f"--snapshot-interval={'6h' if enabled == 'true' else '0s'}", source_policy)

                triggered = any("server" in a and "snapshot" in a for a in argvs)
                self.assertEqual(triggered, enabled == "true")
                server.terminate.assert_called_once()

    def test_readonly_mode_connects_readonly_and_skips_repository_mutations_and_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_env = dict(
                os.environ, KOPIA_PASSWORD="fixture-repository-password",
                KOPIA_SERVER_PASSWORD="fixture-ui-password", KOPIA_SFTP_HOST="dev.invalid",
                KOPIA_SFTP_USERNAME="sophon-restore", KOPIA_SFTP_PATH="/repository", KOPIA_SFTP_PORT="22",
                KOPIA_CLIENT_USERNAME="backup", KOPIA_CLIENT_HOSTNAME="sophon",
                KOPIA_SFTP_PRIVATE_KEY_B64=base64.b64encode(b"fixture-key").decode(),
                KOPIA_SFTP_KNOWN_HOSTS_B64=base64.b64encode(b"fixture-pins").decode(),
                KOPIA_BACKUPS_ENABLED="false", KOPIA_READONLY="true",
                KOPIA_SNAPSHOT_INTERVAL="6h", SOURCE_ROOT=temporary,
                ENGINE_URL="http://dump-proxy:2375",
            )
            with self.assertRaisesRegex(ValueError, "KOPIA_READONLY"):
                runtime.start(base_env | {"KOPIA_BACKUPS_ENABLED": "true"})

            callbacks = {}
            server = Mock()
            server.poll.return_value = None

            def sleep(_):
                callbacks[signal.SIGTERM](signal.SIGTERM, None)

            with patch.object(runtime, "Path", side_effect=lambda path:
                              root / "credentials" if path == "/tmp/credentials" else Path(path)), \
                    patch.object(runtime.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0, b"", b"")) as commands, \
                    patch.object(runtime.subprocess, "Popen", return_value=server) as processes, \
                    patch.object(runtime.signal, "signal", side_effect=lambda sig, fn: callbacks.update({sig: fn})), \
                    patch.object(runtime.time, "sleep", side_effect=sleep):
                runtime.start(base_env)
            self.assertEqual(processes.call_count, 1)
            self.assertNotIn("--enable-actions", processes.call_args.args[0])
            self.assertEqual(len(commands.call_args_list), 1)
            connect_argv = commands.call_args_list[0].args[0]
            self.assertIn("--readonly", connect_argv)
            self.assertNotIn("--enable-actions", connect_argv)
            self.assertFalse(any("maintenance" in call.args[0] or "policy" in call.args[0]
                                 or "set-client" in call.args[0] for call in commands.call_args_list))


if __name__ == "__main__":
    unittest.main()
