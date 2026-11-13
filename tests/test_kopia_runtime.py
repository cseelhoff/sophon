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
    def test_scheduled_partial_run_is_reported_without_repeating_immediately(self):
        for pending in (False, True):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = dict(
                    os.environ, KOPIA_PASSWORD="fixture-repository-password",
                    KOPIA_SERVER_PASSWORD="fixture-ui-password", KOPIA_SFTP_HOST="dev.invalid",
                    KOPIA_SFTP_USERNAME="backup", KOPIA_SFTP_PATH="/dev-repository", KOPIA_SFTP_PORT="22",
                    KOPIA_CLIENT_USERNAME="backup", KOPIA_CLIENT_HOSTNAME="dev",
                    KOPIA_SFTP_PRIVATE_KEY_B64=base64.b64encode(b"fixture-key").decode(),
                    KOPIA_SFTP_KNOWN_HOSTS_B64=base64.b64encode(b"fixture-pins").decode(),
                    KOPIA_BACKUPS_ENABLED="true", KOPIA_SNAPSHOT_INTERVAL="6h", SOURCE_ROOT=temporary,
                    ENGINE_URL="http://engine:2375",
                )
                callbacks, sleeps = {}, []
                server, worker = Mock(), Mock()
                server.poll.return_value = None
                worker.poll.return_value = 1
                worker.returncode = 1

                def sleep(_):
                    sleeps.append(True)
                    if len(sleeps) == 1 and pending:
                        (root / "journal.json").write_text("{}")
                    if len(sleeps) >= 2:
                        callbacks[signal.SIGTERM](signal.SIGTERM, None)

                with patch.object(runtime, "Path", side_effect=lambda path:
                                  root / "credentials" if path == "/tmp/credentials" else Path(path)), \
                        patch.object(runtime.backup, "recover"), \
                        patch.object(runtime.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], 0, b"", b"")) as commands, \
                        patch.object(runtime.subprocess, "Popen", side_effect=[server, worker]) as processes, \
                        patch.object(runtime.signal, "signal", side_effect=lambda sig, fn: callbacks.update({sig: fn})), \
                        patch.object(runtime.time, "sleep", side_effect=sleep), \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    if pending:
                        with self.assertRaisesRegex(RuntimeError, "recovery is pending"):
                            runtime.start(environment)
                    else:
                        runtime.start(environment)
                    self.assertEqual(processes.call_count, 2)
                    self.assertEqual(processes.call_args_list[1].args[0], ["python3", "/app/backup.py", "run"])
                    self.assertIn("ERROR backup run exited 1", errors.getvalue())
                    server.terminate.assert_called_once()
                    self.assertFalse(any("create" in call.args[0] for call in commands.call_args_list))


if __name__ == "__main__":
    unittest.main()
