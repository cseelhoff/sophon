#!/usr/bin/env python3
"""Backup collector, native Kopia action, and dump-proxy security checks."""
from http.server import HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roles/kopia/files"))
import backup
import dump_proxy


def participant(number, name, labels=None, mounts=None):
    return {
        "Id": f"{number:064x}",
        "Name": "/" + name,
        "Image": "sha256:" + f"{number:064x}",
        "Config": {
            "Image": "fixture:1",
            "Env": ["SECRET=NEVER_CAPTURE_ENV"],
            "Labels": dict(labels or {}),
        },
        "State": {"Running": True, "ExitCode": 0},
        "Mounts": list(mounts or []),
    }


class MockPodmanTransport:
    def __init__(self, containers, dump_stdout=b"-- native fixture SQL\nCREATE TABLE fixture(id integer);\n",
                 dump_exit=0):
        self.containers = {c["Id"]: c for c in containers}
        self.events = []
        self.execs = {}
        self.dump_stdout = dump_stdout
        self.dump_exit = dump_exit

    def __call__(self, method, path, body, stream):
        self.events.append((method, path))
        parts = path.split("?")[0].strip("/").split("/")
        if method == "GET" and path.startswith("/containers/json"):
            return [{"Id": key} for key in self.containers]
        if parts[0] == "containers" and len(parts) == 3:
            current = self.containers[parts[1]]
            if method == "GET" and parts[2] == "json":
                return current
            if method == "POST" and parts[2] == "exec":
                execution = f"{len(self.execs) + 1:064x}"
                self.execs[execution] = body
                return {"Id": execution}
        if parts[0] == "exec" and len(parts) == 3:
            if method == "GET" and parts[2] == "json":
                return {"Running": False, "ExitCode": self.dump_exit}
            if method == "POST" and parts[2] == "start":
                payload = bytearray()
                if self.dump_stdout:
                    payload.extend(struct.pack(">BxxxI", 1, len(self.dump_stdout)) + self.dump_stdout)
                buf = io.BytesIO(bytes(payload))
                return {"read": lambda size: buf.read(min(size, 7)), "close": buf.close}
        raise AssertionError((method, path, body))


class CollectorTests(unittest.TestCase):
    def test_pending_operator_restore_blocks_collection_before_engine_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "restore.json").write_text('{"version":3,"volume":"recovery-state"}')
            engine = MockPodmanTransport([])
            with self.assertRaisesRegex(RuntimeError, "restoration"):
                backup.prepare({"transport": engine}, {}, root, "fixture")
            self.assertEqual(engine.events, [])

    def test_hot_collection_never_stops_containers_excludes_caches_and_never_captures_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vol_root = root / "volumes"
            gitea_vol = vol_root / "gitea_gitea_data/_data"
            (gitea_vol / "git/lfs/objects/ab").mkdir(parents=True)
            (gitea_vol / "git/lfs/objects/ab/oid123").write_text("lfs-binary-payload")
            (gitea_vol / "git/lfs/tmp").mkdir(parents=True)
            (gitea_vol / "git/lfs/tmp/partial").write_text("transient-upload")
            (gitea_vol / "gitea/sessions").mkdir(parents=True)
            (gitea_vol / "gitea/sessions/sess1").write_text("ephemeral-session")
            (gitea_vol / "gitea/conf").mkdir(parents=True)
            (gitea_vol / "gitea/conf/app.ini").write_text(
                "APP_NAME = Sophon Git\n[database]\nPASSWD = DO_NOT_BACK_UP_DB_PASSWORD\n"
                "[security]\nSECRET_KEY = generated-secret-key\n")

            gitea = participant(
                1, "gitea",
                labels={
                    "com.docker.compose.project": "gitea",
                    "sophon.backup.enable": "true",
                    "sophon.backup.volumes": "/data",
                    "sophon.backup.exclude": "git/lfs/tmp/,gitea/sessions/",
                    "sophon.backup.ini": "/data/gitea/conf/app.ini:security.SECRET_KEY",
                },
                mounts=[{"Type": "volume", "Name": "gitea_gitea_data", "Destination": "/data", "RW": True}],
            )
            gitea_db = participant(
                2, "gitea-db",
                labels={
                    "com.docker.compose.project": "gitea",
                    "sophon.backup.enable": "true",
                    "sophon.backup.dump": 'exec pg_dumpall -U "$POSTGRES_USER" --clean --if-exists',
                    "sophon.backup.dump.name": "postgres-cluster.sql",
                    "sophon.backup.dump.restore": "postgres-cluster",
                },
                mounts=[{"Type": "volume", "Name": "gitea_db_data", "Destination": "/var/lib/postgresql", "RW": True}],
            )
            stateless = participant(
                3, "coredns",
                labels={"com.docker.compose.project": "coredns"},
            )
            disabled = participant(
                4, "scratch",
                labels={"sophon.backup.enable": "false"},
                mounts=[{"Type": "volume", "Name": "scratch_vol", "Destination": "/scratch", "RW": True}],
            )

            api = MockPodmanTransport([gitea, gitea_db, stateless, disabled])
            result = backup.prepare(
                {"transport": api}, {}, root / "stage", "fixture-version", volumes_root=vol_root)
            staged = Path(result["path"])
            manifest = json.loads((staged / "manifest.json").read_text())

            self.assertEqual(manifest["version"], 3)
            self.assertEqual(manifest["sophon_version"], "fixture-version")
            self.assertEqual(manifest["failures"], [])
            self.assertEqual([g["name"] for g in manifest["groups"]], ["gitea"])
            self.assertIn("gitea_db_data", manifest["undeclared_volumes"])
            self.assertNotIn("NEVER_CAPTURE_ENV", json.dumps(manifest))
            self.assertFalse(any(
                ev[1].startswith("/containers/") and ("/stop" in ev[1] or "/start" in ev[1] or "/archive" in ev[1])
                for ev in api.events
            ))
            self.assertTrue(gitea["State"]["Running"])

            vol_stage = staged / "gitea/gitea/volume-data"
            self.assertEqual((vol_stage / "git/lfs/objects/ab/oid123").read_text(), "lfs-binary-payload")
            self.assertFalse((vol_stage / "git/lfs/tmp").exists())
            self.assertFalse((vol_stage / "gitea/sessions").exists())
            self.assertFalse((vol_stage / "gitea/conf/app.ini").exists())
            kopiaignore = (vol_stage / ".kopiaignore").read_text()
            self.assertIn("git/lfs/tmp/", kopiaignore)
            self.assertIn("/gitea/conf/app.ini", kopiaignore)

            ini_text = (staged / "gitea/gitea/ini-app.ini").read_text()
            self.assertIn("generated-secret-key", ini_text)
            self.assertNotIn("DO_NOT_BACK_UP_DB_PASSWORD", ini_text)
            self.assertIn("CREATE TABLE", (staged / "gitea/gitea-db/postgres-cluster.sql").read_text())

    def test_self_service_developer_workload_discovers_volume_and_custom_dump_without_ansible_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vol_root = root / "volumes"
            uploads = vol_root / "team_app_uploads/_data"
            uploads.mkdir(parents=True)
            (uploads / "report.pdf").write_bytes(b"%PDF-1.7-team-artifact")

            app = participant(
                1, "team-web",
                labels={
                    "com.docker.compose.project": "team-app",
                    "sophon.backup.enable": "true",
                    "sophon.backup.volumes": "/var/lib/app/uploads",
                },
                mounts=[{"Type": "volume", "Name": "team_app_uploads", "Destination": "/var/lib/app/uploads", "RW": True}],
            )
            db = participant(
                2, "team-db",
                labels={
                    "com.docker.compose.project": "team-app",
                    "sophon.backup.enable": "true",
                    "sophon.backup.dump": 'exec pg_dump -Fc -Z0 -U "$POSTGRES_USER" "$POSTGRES_DB"',
                    "sophon.backup.dump.name": "app.dump",
                    "sophon.backup.dump.restore": "postgres-custom",
                },
                mounts=[{"Type": "volume", "Name": "team_app_pg", "Destination": "/var/lib/postgresql", "RW": True}],
            )
            api = MockPodmanTransport([app, db], dump_stdout=b"PGDMP-uncompressed-custom-stream")
            result = backup.prepare({"transport": api}, {}, root / "stage", "fixture", volumes_root=vol_root)
            staged = Path(result["path"])
            self.assertEqual(result["manifest"]["failures"], [])
            self.assertEqual(
                (staged / "team-app/team-web/volume-var-lib-app-uploads/report.pdf").read_bytes(),
                b"%PDF-1.7-team-artifact",
            )
            self.assertEqual(
                (staged / "team-app/team-db/app.dump").read_bytes(),
                b"PGDMP-uncompressed-custom-stream",
            )

    def test_missing_volume_fails_only_that_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            bad = participant(
                1, "external",
                labels={"sophon.backup.enable": "true", "sophon.backup.group": "external",
                        "sophon.backup.paths": "/data/state"},
                mounts=[{"Type": "volume", "Name": "missing_vol", "Destination": "/data", "RW": True}],
            )
            good = participant(
                2, "database",
                labels={"sophon.backup.enable": "true", "sophon.backup.group": "good",
                        "sophon.backup.dump": "pg_dumpall", "sophon.backup.dump.name": "dump.sql"},
            )
            api = MockPodmanTransport([bad, good])
            result = backup.prepare(
                {"transport": api}, {}, Path(temporary) / "stage", "fixture", volumes_root=Path(temporary) / "vols")
            manifest = result["manifest"]
            self.assertEqual(manifest["failures"][0]["group"], "external")
            self.assertIn("missing_vol", manifest["failures"][0]["error"])
            self.assertEqual([g["name"] for g in manifest["groups"]], ["good"])

    def test_legacy_labels_without_enable_or_with_stop_are_rejected(self):
        for labels in (
            {"sophon.backup.version": "1", "sophon.backup.spec": "{}"},
            {"sophon.backup.enable": "true", "sophon.backup.stop": "true", "sophon.backup.paths": "/data/a"},
        ):
            container = participant(1, "old", labels=labels,
                                    mounts=[{"Type": "volume", "Name": "v", "Destination": "/data", "RW": True}])
            with self.assertRaises(RuntimeError):
                backup.parse_spec(container)

    def test_failed_empty_or_truncated_native_dump_is_rejected(self):
        for kwargs, expected in (
            ({"dump_exit": 7}, "exit 7"),
            ({"dump_stdout": b""}, "empty stdout"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                db = participant(
                    1, "database",
                    labels={"sophon.backup.enable": "true", "sophon.backup.group": "app",
                            "sophon.backup.dump": "pg_dumpall", "sophon.backup.dump.name": "database.sql"},
                )
                api = MockPodmanTransport([db], **kwargs)
                result = backup.prepare({"transport": api}, {}, Path(temporary), "fixture")
                self.assertEqual(result["manifest"]["groups"], [])
                self.assertIn(expected, result["manifest"]["failures"][0]["error"])
        with tempfile.TemporaryDirectory() as temporary:
            db = participant(
                1, "database",
                labels={"sophon.backup.enable": "true", "sophon.backup.group": "app",
                        "sophon.backup.dump": "pg_dumpall", "sophon.backup.dump.name": "database.sql"},
            )
            truncated_payload = struct.pack(">BxxxI", 1, 9) + b"SELECT 1;"
            buf = io.BytesIO(truncated_payload)
            with patch.object(backup, "proxy_dump_stream",
                              return_value={"read": buf.read, "close": buf.close}):
                with self.assertRaisesRegex(RuntimeError, "truncated before completion status"):
                    backup.dump({}, {"id": db["Id"], "name": "database", "running": True},
                                {"name": "database.sql"}, Path(temporary) / "out.sql")

    def test_dump_proxy_blocks_arbitrary_engine_requests_and_strips_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "podman.sock"
            db = participant(
                1, "gitea-db",
                labels={
                    "com.docker.compose.project": "gitea",
                    "traefik.enable": "false",
                    "sophon.backup.enable": "true",
                    "sophon.backup.dump": 'exec pg_dumpall -U "$POSTGRES_USER"',
                    "sophon.backup.dump.name": "postgres-cluster.sql",
                },
            )
            seen_exec = {}
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.listen(16)

            def serve_podman():
                while True:
                    try:
                        conn, _ = srv.accept()
                    except OSError:
                        break
                    with conn:
                        raw = b""
                        while b"\r\n\r\n" not in raw:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            raw += chunk
                        if not raw:
                            continue
                        head, rest = raw.split(b"\r\n\r\n", 1)
                        lines = head.decode().split("\r\n")
                        method, path, _ = lines[0].split(" ", 2)
                        headers = {k.lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
                        clen = int(headers.get("content-length", "0"))
                        while len(rest) < clen:
                            rest += conn.recv(clen - len(rest))
                        if method == "GET" and path.startswith("/v1.41/containers/json"):
                            body = json.dumps([{"Id": db["Id"]}]).encode()
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                         + str(len(body)).encode() + b"\r\n\r\n" + body)
                        elif method == "GET" and path == f"/v1.41/containers/{db['Id']}/json":
                            body = json.dumps(db).encode()
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                         + str(len(body)).encode() + b"\r\n\r\n" + body)
                        elif method == "POST" and path == f"/v1.41/containers/{db['Id']}/exec":
                            seen_exec["spec"] = json.loads(rest.decode())
                            body = json.dumps({"Id": "e" * 64}).encode()
                            conn.sendall(b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: "
                                         + str(len(body)).encode() + b"\r\n\r\n" + body)
                        elif method == "POST" and path == f"/v1.41/exec/{'e' * 64}/start":
                            sql = b"SELECT 1;\n"
                            frame = struct.pack(">BxxxI", 1, len(sql)) + sql
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
                                         b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame)
                        elif method == "GET" and path == f"/v1.41/exec/{'e' * 64}/json":
                            body = json.dumps({"Running": False, "ExitCode": 0}).encode()
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                         + str(len(body)).encode() + b"\r\n\r\n" + body)
                        else:
                            conn.sendall(b"HTTP/1.1 500 Unexpected\r\nContent-Length: 0\r\n\r\n")

            t_sock = threading.Thread(target=serve_podman, daemon=True)
            t_sock.start()

            service = dump_proxy.ProxyService({"socket": str(sock_path), "timeout": 10})
            httpd = HTTPServer(("127.0.0.1", 0), dump_proxy.make_handler(service))
            port = httpd.server_address[1]
            t_http = threading.Thread(target=httpd.serve_forever, daemon=True)
            t_http.start()
            try:
                api = backup.engine_api(f"http://127.0.0.1:{port}", 10)
                inventory = backup.proxy_inventory(api)
                self.assertNotIn("NEVER_CAPTURE_ENV", json.dumps(inventory))
                self.assertNotIn("traefik.enable", json.dumps(inventory))

                for method, forbidden_path in (
                    ("POST", f"/containers/{db['Id']}/exec"),
                    ("POST", f"/containers/{db['Id']}/stop?t=10"),
                    ("DELETE", f"/containers/{db['Id']}"),
                    ("GET", f"/containers/{db['Id']}/archive?path=/var"),
                    ("POST", f"/v1/dump/{db['Id']}/undeclared.sql"),
                ):
                    with self.assertRaises(backup.EngineError) as ctx:
                        backup._http_request(api, method, forbidden_path, {})
                    self.assertEqual(ctx.exception.status, 403)

                out = Path(temporary) / "out.sql"
                exported = backup.dump(api, {"id": db["Id"], "name": "gitea-db", "running": True},
                                       {"name": "postgres-cluster.sql"}, out)
                self.assertEqual(out.read_bytes(), b"SELECT 1;\n")
                self.assertEqual(exported["bytes"], len(b"SELECT 1;\n"))
                self.assertEqual(seen_exec["spec"]["Cmd"], ["sh", "-ec", 'exec pg_dumpall -U "$POSTGRES_USER"'])
            finally:
                httpd.shutdown()
                httpd.server_close()
                srv.close()

    @unittest.skipUnless(shutil.which("kopia"), "kopia binary required for live native action test")
    def test_live_kopia_native_before_and_after_snapshot_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo_dir = root / "repo"
            config_file = root / "config/repository.config"
            cache_dir = root / "cache"
            logs_dir = root / "logs"
            source_root = root / "source"
            sophon_state = root / "sophon-state"
            vol_root = root / "volumes"
            for d in (repo_dir, config_file.parent, cache_dir, logs_dir, source_root, sophon_state):
                d.mkdir(parents=True, exist_ok=True)
            (vol_root / "app_data/_data/keep").mkdir(parents=True)
            (vol_root / "app_data/_data/keep/file.txt").write_text("important-state")
            (vol_root / "app_data/_data/cache").mkdir(parents=True)
            (vol_root / "app_data/_data/cache/drop.txt").write_text("transient-cache")

            app = participant(
                1, "app",
                labels={
                    "sophon.backup.enable": "true",
                    "sophon.backup.group": "app",
                    "sophon.backup.volumes": "/data",
                    "sophon.backup.exclude": "cache/",
                },
                mounts=[{"Type": "volume", "Name": "app_data", "Destination": "/data", "RW": True}],
            )

            podman_mock = MockPodmanTransport([app])
            service = dump_proxy.ProxyService({"transport": podman_mock})
            httpd = HTTPServer(("127.0.0.1", 0), dump_proxy.make_handler(service))
            t_http = threading.Thread(target=httpd.serve_forever, daemon=True)
            t_http.start()
            try:
                env = dict(
                    os.environ,
                    KOPIA_PASSWORD="test-repo-password",
                    KOPIA_CONFIG_PATH=str(config_file),
                    KOPIA_CACHE_DIRECTORY=str(cache_dir),
                    KOPIA_LOG_DIR=str(logs_dir),
                    KOPIA_CHECK_FOR_UPDATES="false",
                    SOURCE_ROOT=str(source_root),
                    VOLUMES_ROOT=str(vol_root),
                    ENGINE_URL=f"http://127.0.0.1:{httpd.server_address[1]}",
                    SOPHON_VERSION="live-action-test",
                )
                subprocess.run(
                    ["kopia", "repository", "create", "filesystem", f"--path={repo_dir}",
                     "--enable-actions", "--override-username=backup", "--override-hostname=sophon"],
                    env=env, check=True, capture_output=True, text=True,
                )
                backup_script = str(Path(__file__).resolve().parents[1] / "roles/kopia/files/backup.py")
                subprocess.run(
                    ["kopia", "policy", "set", str(sophon_state),
                     f"--before-snapshot-root-action={backup_script}",
                     f"--after-snapshot-root-action={backup_script}",
                     "--no-persist-action-script",
                     "--action-command-mode=essential"],
                    env=env, check=True, capture_output=True, text=True,
                )
                snap_proc = subprocess.run(
                    ["kopia", "snapshot", "create", str(sophon_state), "--json"],
                    env=env, capture_output=True, text=True,
                )
                self.assertEqual(snap_proc.returncode, 0, snap_proc.stdout + snap_proc.stderr)
                snap = json.loads(snap_proc.stdout)
                restored = root / "restored"
                subprocess.run(
                    ["kopia", "restore", snap["id"], str(restored)],
                    env=env, check=True, capture_output=True, text=True,
                )
                self.assertEqual((restored / "app/app/volume-data/keep/file.txt").read_text(), "important-state")
                self.assertFalse((restored / "app/app/volume-data/cache").exists())
                report = json.loads((source_root / "last-run.json").read_text())
                self.assertEqual(report["status"], "complete")
                self.assertEqual(report["upload_status"], "complete")
                self.assertEqual(list((source_root / "runs").iterdir()), [])
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
