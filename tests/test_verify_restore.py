import base64
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import certificates
import verify_restore


def make_domain_cert(directory: Path, domain: str, common_name: str | None = None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cn = common_name or f"*.{domain}"
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(domain),
                x509.DNSName(f"*.{domain}"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{cn.replace('*', 'wildcard')}.crt"
    key_path = directory / f"{cn.replace('*', 'wildcard')}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def base_drill_env():
    return {
        "SOPHON_PROXMOX_HOST": "pve.example.internal",
        "SOPHON_PROXMOX_NODE": "pve1",
        "SOPHON_PROXMOX_PASSWORD": "pve-password",
        "SOPHON_DOMAIN": "sophon.example.com",
        "SOPHON_CF_TOKEN": "LIVE_PRODUCTION_CF_TOKEN_MUST_BE_STRIPPED",
        "SOPHON_CF_TUNNEL_ENABLED": "true",
        "SOPHON_CF_TUNNEL_NAME": "sophon",
        "SOPHON_KEYCLOAK_REALM": "sophon",
        "SOPHON_KEYCLOAK_ADMIN_PASSWORD": "kc-admin-password",
        "SOPHON_KEYCLOAK_DB_PASSWORD": "kc-db-password",
        "SOPHON_KEYCLOAK_ARCANE_CLIENT_SECRET": "kc-arcane-secret",
        "SOPHON_GITEA_ADMIN_PASSWORD": "gitea-admin-password",
        "SOPHON_GITEA_DB_PASSWORD": "gitea-db-password",
        "SOPHON_OPENLDAP_ADMIN_PASSWORD": "ldap-admin-password",
        "SOPHON_OPENLDAP_EXAMPLE_USER_PASSWORD": "ldap-user-password",
        "SOPHON_OPENLDAP_EXAMPLE_ADMIN_PASSWORD": "ldap-ex-admin-password",
        "SOPHON_ARCANE_ADMIN_PASSWORD": "arcane-admin-password",
        "SOPHON_KOPIA_PASSWORD": "kopia-repo-password",
        "SOPHON_KOPIA_SFTP_HOST": "10.0.60.10",
        "SOPHON_KOPIA_SFTP_PORT": "22",
        "SOPHON_KOPIA_SFTP_USERNAME": "sophon-restore",
        "SOPHON_KOPIA_SFTP_PATH": "/repository",
        "SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64": base64.b64encode(b"FAKE-RESTORE-KEY").decode("ascii"),
        "SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64": base64.b64encode(b"10.0.60.10 ssh-ed25519 AAAA").decode("ascii"),
        "SOPHON_KOPIA_BACKUPS_ENABLED": "true",
        "SOPHON_KOPIA_COARSE_ENABLED": "true",
    }


class FakeSerialDriver:
    """Simulates CoreOS install -> reboot -> ttyS0 first-boot password -> login -> deploy -> restore."""

    def __init__(self, https_port: int):
        self.https_port = https_port
        self.stage = "install"
        self.sent_lines = []
        self.staged_env_b64 = None
        self.secrets = set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def register_secret(self, value: str):
        if value:
            self.secrets.add(value)

    def recent_output(self, limit: int = 1500) -> str:
        return f"stage={self.stage}"

    def expect(self, patterns, *, timeout=60.0, reconnect=False, nudge_interval=0.0):
        import re
        compiled = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        text_by_stage = {
            "install": "Installation complete. Rebooting to the installed disk.\n",
            "new_pw": "New password for core: ",
            "confirm_pw": "Confirm password: ",
            "after_pw": "Password set. Log in as core, then run: sophon-deploy\nsophon-infravm login: ",
            "login_pw": "Password: ",
            "shell_init": "[core@sophon-infravm ~]$ ",
            "prompt": "__SOPHON_PROMPT__$ ",
        }
        sample = text_by_stage[self.stage]
        for idx, regex in enumerate(compiled):
            m = regex.search(sample)
            if m:
                if self.stage == "install":
                    self.stage = "new_pw"
                return idx, m, sample
        raise AssertionError(f"Unexpected patterns {patterns!r} in stage {self.stage!r}")

    def send_line(self, line="", *, secret=False):
        self.sent_lines.append(line)
        if self.stage == "new_pw":
            self.stage = "confirm_pw"
        elif self.stage == "confirm_pw":
            self.stage = "after_pw"
        elif self.stage == "after_pw" and line == "core":
            self.stage = "login_pw"
        elif self.stage == "login_pw":
            self.stage = "shell_init"
        elif self.stage == "shell_init" and "PS1=" in line:
            self.stage = "prompt"

    def run_checked(self, command: str, *, timeout: float = 600.0, prompt_pattern: str = r"__SOPHON_PROMPT__\$ "):
        self.sent_lines.append(command)
        if "sophon-verify.env" in command and "base64 -d" in command:
            # Extract the base64 payload written to tmpfs so the test can inspect every variable!
            payload = command.split("<<'__SOPHON_ENV_EOF__'\n", 1)[1].split("\n__SOPHON_ENV_EOF__", 1)[0]
            self.staged_env_b64 = payload.replace("\n", "")
            return "deploy-ok"
        if "jq -r .SOPHON_INFRAVM_IP" in command:
            return "127.0.0.1"
        if command.startswith("sophon-restore "):
            return "Snapshot : snap-20260419 (2026-04-19T02:00:00Z)\nServices : traefik, ldap, keycloak, gitea\nRestore complete (traefik, ldap, keycloak, gitea)."
        if "sophon_smoke_check" in command:
            assert '"sudo"' not in command, "smoke check must inspect core's rootless podman store, not sudo podman"
            assert "sophon:latest python3 -" in command, "Fedora CoreOS has no host python3; smoke check must run via sophon:latest"
            return json.dumps({
                "containers_ok": True,
                "cloudflared_absent": True,
                "kopia_readonly": True,
                "oidc_issuer": "https://auth.sophon.example.com/realms/sophon",
                "gitea_version": "1.25.5",
                "arcane_status": 200,
            })
        return ""


class VerifyRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_guardrails_refuse_production_vm_name_and_readwrite_sftp_account(self):
        env = base_drill_env()
        with self.assertRaisesRegex(ValueError, "sophon-infravm"):
            verify_restore.build_config(["--vm-name", "sophon-infravm"], environ=env)

        bad_sftp = dict(env, SOPHON_KOPIA_SFTP_USERNAME="sophon-backup")
        with self.assertRaisesRegex(ValueError, "sophon-restore"):
            verify_restore.build_config([], environ=bad_sftp)

    def test_guest_environment_strips_cloudflare_tokens_and_enforces_readonly_standby(self):
        env = base_drill_env()
        cfg = verify_restore.build_config([], environ=env)
        guest_env = cfg["guest_env"]
        self.assertEqual(guest_env["SOPHON_CF_TOKEN"], "")
        self.assertEqual(guest_env["SOPHON_CF_TUNNEL_ENABLED"], "false")
        self.assertEqual(guest_env["SOPHON_CF_TUNNEL_NAME"], "")
        self.assertEqual(guest_env["SOPHON_KOPIA_SFTP_USERNAME"], "sophon-restore")
        self.assertEqual(guest_env["SOPHON_KOPIA_BACKUPS_ENABLED"], "false")
        self.assertEqual(guest_env["SOPHON_KOPIA_COARSE_ENABLED"], "false")
        self.assertEqual(guest_env["SOPHON_KOPIA_CREATE_REPOSITORY"], "false")

    def test_sni_probe_accepts_restored_cert_and_rejects_traefik_default_cert(self):
        good_cert, good_key = make_domain_cert(self.directory, "sophon.example.com")
        default_cert, default_key = make_domain_cert(
            self.directory, "traefik.default", common_name="TRAEFIK DEFAULT CERT"
        )

        def start_https(cert_file, key_file):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    return

                def do_GET(self):
                    host = self.headers.get("Host", "")
                    if host == "auth.sophon.example.com" and self.path.endswith("/.well-known/openid-configuration"):
                        body = json.dumps({"issuer": "https://auth.sophon.example.com/realms/sophon"}).encode()
                    elif host == "git.sophon.example.com" and self.path == "/api/v1/version":
                        body = json.dumps({"version": "1.25.5"}).encode()
                    else:
                        body = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server.server_address[1]

        good_port = start_https(good_cert, good_key)
        probes = verify_restore.run_host_sni_probes(
            "127.0.0.1", "sophon.example.com", "sophon", port=good_port, ca_file=str(good_cert)
        )
        self.assertEqual(probes["keycloak_issuer"], "https://auth.sophon.example.com/realms/sophon")
        self.assertEqual(probes["gitea_version"], "1.25.5")
        self.assertEqual(probes["arcane_status"], 200)

        bad_port = start_https(default_cert, default_key)
        with self.assertRaisesRegex(RuntimeError, "TRAEFIK DEFAULT CERT"):
            verify_restore.run_host_sni_probes(
                "127.0.0.1", "sophon.example.com", "sophon", port=bad_port, validate_certs=False, retry_timeout=0.0
            )

        # Also verify bootstrap_store generates a valid Traefik store whose temporary cert is rejected post-restore!
        boot_raw = json.loads(certificates.bootstrap_store("sophon.example.com").decode("utf-8"))
        boot_entry = boot_raw["letsencrypt"]["Certificates"][0]
        boot_cert = self.directory / "boot.crt"
        boot_key = self.directory / "boot.key"
        boot_cert.write_bytes(base64.b64decode(boot_entry["certificate"]))
        boot_key.write_bytes(base64.b64decode(boot_entry["key"]))
        boot_port = start_https(boot_cert, boot_key)
        with self.assertRaisesRegex(RuntimeError, "SOPHON PRE-RESTORE BOOTSTRAP"):
            verify_restore.run_host_sni_probes(
                "127.0.0.1", "sophon.example.com", "sophon", port=boot_port, validate_certs=False, retry_timeout=0.0
            )

    def test_end_to_end_drill_recreates_vm_drives_serial_verifies_and_stops_vm(self):
        good_cert, good_key = make_domain_cert(self.directory, "sophon.example.com")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                host = self.headers.get("Host", "")
                if host == "auth.sophon.example.com":
                    body = json.dumps({"issuer": "https://auth.sophon.example.com/realms/sophon"}).encode()
                elif host == "git.sophon.example.com":
                    body = json.dumps({"version": "1.25.5"}).encode()
                else:
                    body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        https_srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(good_cert), keyfile=str(good_key))
        https_srv.socket = ctx.wrap_socket(https_srv.socket, server_side=True)
        threading.Thread(target=https_srv.serve_forever, daemon=True).start()
        self.addCleanup(https_srv.server_close)
        self.addCleanup(https_srv.shutdown)
        https_port = https_srv.server_address[1]

        serial_driver = FakeSerialDriver(https_port)
        actions = []

        class FakePve:
            def __init__(self, host, *, port=8006, user="root@pam", password="", validate_certs=False, ca_file=None):
                assert host == "pve.example.internal"
                assert user == "root@pam"
                self.created = False

            def authenticate(self):
                actions.append("authenticate")

            def find_vm(self, node, name):
                if not self.created:
                    return {"vmid": 301, "name": name, "type": "qemu", "node": node, "template": 0}
                return {"vmid": 302, "name": name, "type": "qemu", "node": node, "template": 0}

            def destroy_vm(self, node, vmid, *, expected_marker, timeout=300.0):
                actions.append(("destroy", vmid, expected_marker))
                self.created = True

            def set_onboot(self, node, vmid, enabled: bool, *, expected_marker=None):
                actions.append(("set_onboot", vmid, enabled, expected_marker))

            def stop_vm(self, node, vmid, *, expected_marker=None, timeout=180.0):
                actions.append(("stop", vmid, expected_marker))

            def open_serial(self, node, vmid):
                actions.append(("open_serial", vmid))
                return serial_driver

        def fake_provision(env):
            actions.append(("provision", env["SOPHON_INFRAVM_NAME"], env["SOPHON_HYPERVISOR"], env["SOPHON_PROXMOX_ONBOOT"]))

        report_file = self.directory / "report.json"
        cfg = verify_restore.build_config(
            ["--report-file", str(report_file), "--https-port", str(https_port), "--ca-file", str(good_cert)],
            environ=base_drill_env(),
        )
        with patch.object(verify_restore, "ProxmoxClient", FakePve), patch.object(
            verify_restore, "run_provision_playbook", side_effect=fake_provision
        ):
            report = verify_restore.run_drill(cfg)

        self.assertEqual(actions[0], "authenticate")
        self.assertEqual(actions[1], ("destroy", 301, "sophon-infravm-two-iso-v1"))
        self.assertEqual(actions[2], ("provision", "sophon-restore-drill", "proxmox", "false"))
        self.assertEqual(actions[3], ("open_serial", 302))
        self.assertEqual(actions[4], ("set_onboot", 302, False, "sophon-infravm-two-iso-v1"))
        self.assertEqual(actions[5], ("stop", 302, "sophon-infravm-two-iso-v1"))

        staged_env_text = base64.b64decode(serial_driver.staged_env_b64).decode("utf-8")
        self.assertNotIn("LIVE_PRODUCTION_CF_TOKEN_MUST_BE_STRIPPED", staged_env_text)
        self.assertIn("SOPHON_CF_TOKEN=''\n", staged_env_text)
        self.assertIn("SOPHON_CF_TUNNEL_ENABLED=false\n", staged_env_text)
        self.assertIn("SOPHON_KOPIA_SFTP_USERNAME=sophon-restore\n", staged_env_text)

        self.assertTrue(report_file.is_file())
        saved = json.loads(report_file.read_text())
        self.assertEqual(saved["status"], "verified")
        self.assertEqual(saved["vmid"], 302)
        self.assertEqual(saved["snapshot"], "snap-20260419")
        self.assertEqual(saved["teardown"], "stop")


if __name__ == "__main__":
    unittest.main()
