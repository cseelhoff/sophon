import base64
import datetime as dt
import errno
import http.cookies
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import status_ui


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64url(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return _b64url(value.to_bytes(length, "big"))


def _make_rs256_jwt(private_key: rsa.RSAPrivateKey, kid: str, claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}".encode("ascii")
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode('ascii')}.{_b64url(sig)}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StatusRoleAndScriptsTests(unittest.TestCase):
    def test_sophon_provision_sh_rejects_overloaded_dev_and_destroy_vm_with_redirect_message(self):
        dev_res = subprocess.run(
            ["sh", str(ROOT / "sophon-provision.sh"), "dev"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(dev_res.returncode, 0)
        self.assertIn("sophon-provision-dev.sh", dev_res.stderr)

        destroy_res = subprocess.run(
            ["sh", str(ROOT / "sophon-provision.sh"), "destroy-vm"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(destroy_res.returncode, 0)
        self.assertIn("sophon-destroy-vm.sh", destroy_res.stderr)

    def test_status_compose_and_coredns_cf_exclude_keep_status_lan_only(self):
        env = jinja2.Environment()
        env.filters["regex_replace"] = lambda val, pattern, repl="": re.sub(pattern, repl, str(val))
        env.filters["bool"] = lambda val: str(val).lower() in ("true", "1", "yes") if isinstance(val, str) else bool(val)
        status_tpl = (ROOT / "roles/status/templates/docker-compose.yml.j2").read_text(encoding="utf-8")
        rendered_status = yaml.safe_load(
            env.from_string(status_tpl).render(
                status_image="localhost/sophon:latest",
                keycloak_hostname="auth",
                status_domain_clean="sophon.example.com",
                infravm_ip="10.0.60.107",
                status_script_host_path="/var/lib/sophon/project/src/status_ui.py",
                status_control_host_dir="/var/lib/sophon/control",
                status_port=8080,
            )
        )
        svc = rendered_status["services"]["sophon-status"]
        self.assertEqual(svc["image"], "localhost/sophon:latest")
        self.assertTrue(svc["read_only"])
        self.assertEqual(svc["cap_drop"], ["ALL"])
        self.assertIn("auth.sophon.example.com:10.0.60.107", svc["extra_hosts"])
        labels = "\n".join(svc["labels"])
        self.assertIn("coredns.dockerdiscovery.host=sophon.example.com", labels)
        self.assertIn("Host(`sophon.example.com`) || Host(`status.sophon.example.com`)", labels)

        coredns_tpl = (ROOT / "roles/coredns/templates/docker-compose.yml.j2").read_text(encoding="utf-8")
        rendered_coredns = yaml.safe_load(
            env.from_string(coredns_tpl).render(
                coredns_image_tag="localhost/sophon-coredns:latest",
                coredns_dns_port=53,
                coredns_domain_name="sophon.example.com",
                infravm_ip="10.0.60.107",
                coredns_upstream_dns="1.1.1.1",
                coredns_cf_account_id="acct",
                coredns_cf_zone_id="zone",
                coredns_cf_tunnel_id="tun",
            )
        )
        coredns_env = "\n".join(rendered_coredns["services"]["coredns"]["environment"])
        self.assertIn("CF_EXCLUDE=sophon.example.com,status.sophon.example.com", coredns_env)

    def test_sophon_control_sync_exports_non_secret_appliance_state_and_strips_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "project"
            control_dir = root / "control"
            project_dir.mkdir()
            (project_dir / "site.json").write_text(
                json.dumps({"SOPHON_INFRAVM_IP": "10.0.60.107"}),
                encoding="utf-8",
            )
            (project_dir / "deploy-result.json").write_text(
                json.dumps(
                    {
                        "domain": "sophon.example.com",
                        "infravm_ip": "10.0.60.107",
                        "mode": "standby_restore",
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "standby.json").write_text(
                json.dumps(
                    {
                        "domain": "sophon.example.com",
                        "prod_network": {
                            "mode": "static",
                            "cidr": "10.0.60.101/24",
                            "gateway": "10.0.60.1",
                        },
                        "sftp_username": "sophon-backup",
                        "desired_cf_tunnel_enabled": True,
                        "desired_cf_token": "SUPER-SECRET-CF-TOKEN",
                        "desired_sftp_private_key_b64": "U1VQRVItU0VDUkVULUtFWQ==",
                        "desired_snapshot_interval": "6h",
                    }
                ),
                encoding="utf-8",
            )
            control_dir.mkdir()
            (control_dir / "appliance-state.json").write_text(
                json.dumps(
                    {
                        "target_ip_check": {
                            "occupied": True,
                            "method": "arp",
                            "reason": "Another host on eth0 answered ARP for 10.0.60.101.",
                            "checked_at": "2026-04-12T00:00:00Z",
                        }
                    }
                ),
                encoding="utf-8",
            )
            res = subprocess.run(
                [str(ROOT / "guest/sophon-control-sync")],
                env={
                    **os.environ,
                    "SOPHON_PROJECT_DIR": str(project_dir),
                    "SOPHON_CONTROL_DIR": str(control_dir),
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            raw_state = (control_dir / "appliance-state.json").read_text(encoding="utf-8")
            self.assertNotIn("SUPER-SECRET-CF-TOKEN", raw_state)
            self.assertNotIn("U1VQRVItU0VDUkVULUtFWQ==", raw_state)
            state = json.loads(raw_state)
            self.assertTrue(state["standby"])
            self.assertEqual(state["current_ip"], "10.0.60.107")
            self.assertEqual(state["target_ip"], "10.0.60.101")
            self.assertTrue(state["has_cf_token"])
            self.assertTrue(state["can_promote_sftp"])
            self.assertTrue(state["target_ip_check"]["occupied"])


class CutoverReadinessAndTcpProbeTests(unittest.TestCase):
    def test_probe_tcp_ip_occupied_detects_open_and_refused_ports_and_ignores_unreachable(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            occupied, reason = status_ui.probe_tcp_ip_occupied("127.0.0.1", ports=(port,), timeout=0.5)
            self.assertTrue(occupied)
            self.assertIn(f"TCP port {port} is accepting connections on 127.0.0.1", reason)

        with mock.patch("socket.socket") as sock_cls:
            fake_sock = mock.MagicMock()
            fake_sock.connect_ex.return_value = errno.ECONNREFUSED
            sock_cls.return_value = fake_sock
            occupied_rst, reason_rst = status_ui.probe_tcp_ip_occupied("10.0.60.101", ports=(443,), timeout=0.2)
            self.assertTrue(occupied_rst)
            self.assertIn("actively refused TCP port 443", reason_rst)

            fake_sock.connect_ex.return_value = errno.EHOSTUNREACH
            occupied_free, _ = status_ui.probe_tcp_ip_occupied("10.0.60.101", ports=(443, 22), timeout=0.2)
            self.assertFalse(occupied_free)

    def test_promote_button_disabled_when_target_prod_ip_is_occupied_and_enabled_when_free(self):
        standby_state = {
            "domain": "sophon.example.com",
            "standby": True,
            "mode": "standby_restore",
            "current_ip": "10.0.60.107",
            "target_cidr": "10.0.60.101/24",
            "target_ip": "10.0.60.101",
            "can_promote_sftp": True,
            "desired_cf_tunnel_enabled": True,
            "has_cf_token": True,
            "target_ip_check": {
                "applicable": True,
                "occupied": True,
                "method": "arp",
                "reason": "Another host on eth0 answered ARP for 10.0.60.101.",
            },
        }
        idle_act = {"state": "idle"}

        occupied = status_ui.evaluate_cutover_readiness(
            standby_state,
            idle_act,
            tcp_probe_fn=lambda ip: (True, f"TCP port 443 is accepting connections on {ip}."),
        )
        self.assertTrue(occupied["standby"])
        self.assertTrue(occupied["needs_ip_switch"])
        self.assertTrue(occupied["target_ip_occupied"])
        self.assertFalse(occupied["promote_enabled"])
        self.assertIn("10.0.60.101 is still in use", occupied["promote_disabled_reason"])
        self.assertIn("sophon-destroy-vm.sh", occupied["promote_disabled_reason"])

        arp_only = status_ui.evaluate_cutover_readiness(
            standby_state,
            idle_act,
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertTrue(arp_only["target_ip_occupied"])
        self.assertFalse(arp_only["promote_enabled"])

        free_state = {
            **standby_state,
            "target_ip_check": {"applicable": True, "occupied": False, "method": "none", "reason": ""},
        }
        free = status_ui.evaluate_cutover_readiness(
            free_state,
            idle_act,
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertFalse(free["target_ip_occupied"])
        self.assertTrue(free["promote_enabled"])
        self.assertEqual(free["promote_disabled_reason"], "")

        in_place = status_ui.evaluate_cutover_readiness(
            {**standby_state, "current_ip": "10.0.60.101"},
            idle_act,
            tcp_probe_fn=lambda ip: (True, "self"),
        )
        self.assertFalse(in_place["needs_ip_switch"])
        self.assertFalse(in_place["target_ip_occupied"])
        self.assertTrue(in_place["promote_enabled"])

        ro_restore = status_ui.evaluate_cutover_readiness(
            {**free_state, "can_promote_sftp": False},
            idle_act,
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertFalse(ro_restore["promote_enabled"])
        self.assertIn("sophon-restore", ro_restore["promote_disabled_reason"])

        active_prod = status_ui.evaluate_cutover_readiness(
            {**free_state, "standby": False, "mode": "production_active", "current_ip": "10.0.60.101"},
            {"state": "succeeded"},
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertFalse(active_prod["standby"])
        self.assertFalse(active_prod["promote_enabled"])
        self.assertIn("already running in active production", active_prod["promote_disabled_reason"])


class StatusWebServerEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control_dir = Path(self.tmp.name)
        self.rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_numbers = self.rsa_key.public_key().public_numbers()
        self.jwks = {
            "keys": [
                {
                    "kty": "RSA",
                    "alg": "RS256",
                    "use": "sig",
                    "kid": "test-key-1",
                    "n": _int_b64url(pub_numbers.n),
                    "e": _int_b64url(pub_numbers.e),
                }
            ]
        }
        self.occupied = True
        self.next_groups = ["arcane-admins"]
        self.last_code_verifier = ""
        self.last_nonce = ""
        self._write_appliance_state()

        self.cfg = status_ui.StatusConfig(
            domain="sophon.example.com",
            oidc_realm="sophon",
            oidc_client_id="sophon-status",
            admin_group="arcane-admins",
            session_secret=b"unit-test-session-secret-0123456789abcdef",
            control_dir=self.control_dir,
            tcp_probe_fn=lambda target_ip: (
                (True, f"TCP port 443 is accepting connections on {target_ip}.")
                if self.occupied
                else (False, "")
            ),
        )
        self.cfg.fetch_jwks = lambda force=False: self.jwks
        self.cfg.exchange_code = self._fake_exchange_code
        self.cfg.request_host_probe = lambda force=False: json.loads(
            (self.control_dir / "appliance-state.json").read_text(encoding="utf-8")
        )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), status_ui.make_handler(self.cfg))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _write_appliance_state(self):
        now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.control_dir / "appliance-state.json").write_text(
            json.dumps(
                {
                    "domain": "sophon.example.com",
                    "standby": True,
                    "mode": "standby_restore",
                    "current_ip": "10.0.60.107",
                    "target_cidr": "10.0.60.101/24",
                    "target_ip": "10.0.60.101",
                    "target_gateway": "10.0.60.1",
                    "can_promote_sftp": True,
                    "desired_cf_tunnel_enabled": True,
                    "has_cf_token": True,
                    "desired_cf_tunnel_name": "sophon",
                    "desired_snapshot_interval": "6h",
                    "restored_snapshot": "snap-01",
                    "target_ip_check": {
                        "applicable": True,
                        "occupied": self.occupied,
                        "method": "arp" if self.occupied else "none",
                        "reason": "Another host on eth0 answered ARP for 10.0.60.101." if self.occupied else "",
                        "checked_at": now_iso,
                    },
                    "updated_at": now_iso,
                }
            ),
            encoding="utf-8",
        )

    def _fake_exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> dict:
        self.assertEqual(code, "authcode-xyz")
        self.last_code_verifier = code_verifier
        now = int(time.time())
        claims = {
            "iss": "https://auth.sophon.example.com/realms/sophon",
            "aud": "sophon-status",
            "azp": "sophon-status",
            "sub": "user-123",
            "preferred_username": "caleb",
            "nonce": self.last_nonce,
            "groups": list(self.next_groups),
            "iat": now,
            "exp": now + 3600,
        }
        return {"id_token": _make_rs256_jwt(self.rsa_key, "test-key-1", claims)}

    def _request(self, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers={"Host": "sophon.example.com", **(headers or {})},
        )
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, dict(resp.headers.items()), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8")

    def _login_and_complete_callback(self):
        status, headers, _ = self._request("GET", "/login")
        self.assertEqual(status, 302)
        loc = headers["Location"]
        self.assertIn("https://auth.sophon.example.com/realms/sophon/protocol/openid-connect/auth", loc)
        self.assertIn("code_challenge_method=S256", loc)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        state_param = qs["state"][0]
        self.last_nonce = qs["nonce"][0]

        jar = http.cookies.SimpleCookie()
        jar.load(headers["Set-Cookie"])
        login_cookie = f"{status_ui.LOGIN_COOKIE}={jar[status_ui.LOGIN_COOKIE].value}"

        return self._request(
            "GET",
            f"/oauth2/callback?code=authcode-xyz&state={urllib.parse.quote(state_param)}",
            headers={"Cookie": login_cookie},
        )

    def test_oidc_pkce_rbac_disabled_button_and_promote_cutover_trigger(self):
        status, _, body = self._request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

        # Unauthenticated GET / redirects to /login
        root_status, root_headers, _ = self._request("GET", "/")
        self.assertEqual(root_status, 302)
        self.assertEqual(root_headers["Location"], "/login")

        # Non-admin user is denied (403)
        self.next_groups = ["developers"]
        cb_status, _, cb_body = self._login_and_complete_callback()
        self.assertEqual(cb_status, 403)
        self.assertIn("arcane-admins", cb_body)

        # Admin user logs in and receives session cookie
        self.next_groups = ["arcane-admins"]
        cb_status, cb_headers, _ = self._login_and_complete_callback()
        self.assertEqual(cb_status, 302)
        self.assertTrue(len(self.last_code_verifier) >= 40)

        session_val = status_ui.sign_cookie(
            {"user": "caleb", "csrf": "csrf-test-token-123", "exp": time.time() + 3600},
            self.cfg.session_secret,
        )
        self.assertIn(status_ui.SESSION_COOKIE, str(cb_headers))
        session_cookie = f"{status_ui.SESSION_COOKIE}={session_val}"
        csrf = "csrf-test-token-123"

        # While target prod IP (10.0.60.101) is occupied: button is disabled & /api/promote returns 409
        dash_status, _, dash_html = self._request("GET", "/", headers={"Cookie": session_cookie})
        self.assertEqual(dash_status, 200)
        self.assertIn('id="promote-btn"', dash_html)
        self.assertRegex(dash_html, r'id="promote-btn"[^>]*\sdisabled(?:\s|>)')
        self.assertIn('aria-disabled="true"', dash_html)
        self.assertIn("10.0.60.101 is still in use", dash_html)

        st_status, _, st_body = self._request("GET", "/api/status", headers={"Cookie": session_cookie})
        self.assertEqual(st_status, 200)
        st_json = json.loads(st_body)
        self.assertFalse(st_json["promote_enabled"])
        self.assertTrue(st_json["target_ip_occupied"])

        # Missing CSRF -> 403
        no_csrf_status, _, _ = self._request("POST", "/api/promote", headers={"Cookie": session_cookie}, body=b"{}")
        self.assertEqual(no_csrf_status, 403)

        # Valid CSRF while prod IP is still alive -> 409 Conflict, no activate.request written
        conflict_status, _, conflict_body = self._request(
            "POST",
            "/api/promote",
            headers={"Cookie": session_cookie, "X-Sophon-CSRF": csrf},
            body=b"{}",
        )
        self.assertEqual(conflict_status, 409)
        self.assertFalse((self.control_dir / "activate.request").exists())
        self.assertIn("10.0.60.101", conflict_body)

        # Old prod VM is powered off -> target IP becomes free -> button unlocks -> promote succeeds (202)
        self.occupied = False
        self._write_appliance_state()

        dash_free_status, _, dash_free_html = self._request("GET", "/", headers={"Cookie": session_cookie})
        self.assertEqual(dash_free_status, 200)
        self.assertNotRegex(dash_free_html, r'id="promote-btn"[^>]*\sdisabled(?:\s|>)')
        self.assertIn('aria-disabled="false"', dash_free_html)

        ok_status, _, ok_body = self._request(
            "POST",
            "/api/promote",
            headers={"Cookie": session_cookie, "X-Sophon-CSRF": csrf},
            body=b'{"tunnel_mode": "default"}',
        )
        self.assertEqual(ok_status, 202)
        ok_json = json.loads(ok_body)
        self.assertTrue(ok_json["ok"])
        req_payload = json.loads((self.control_dir / "activate.request").read_text(encoding="utf-8"))
        self.assertEqual(req_payload["requested_by"], "caleb")
        self.assertEqual(req_payload["tunnel_mode"], "default")

    def test_certificate_refresh_readiness_and_api_trigger(self):
        no_disk = status_ui.evaluate_cutover_readiness(
            {"domain": "sophon.example.com", "cert_disk": {"present": False, "valid": False}},
            {"state": "idle"},
            {"state": "idle"},
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertFalse(no_disk["refresh_certs_enabled"])
        self.assertIn("sophon-mount-secrets", no_disk["refresh_certs_disabled_reason"])

        bad_disk = status_ui.evaluate_cutover_readiness(
            {
                "domain": "sophon.example.com",
                "cert_disk": {"present": True, "valid": False, "error": "Certificate domain mismatch."},
            },
            {"state": "idle"},
            {"state": "idle"},
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertFalse(bad_disk["refresh_certs_enabled"])
        self.assertIn("Certificate domain mismatch", bad_disk["refresh_certs_disabled_reason"])

        good_disk = status_ui.evaluate_cutover_readiness(
            {
                "domain": "sophon.example.com",
                "live_cert_expires_at": "2026-06-01T00:00:00+00:00",
                "cert_disk": {"present": True, "valid": True, "expires_at": "2026-07-15T00:00:00+00:00", "error": ""},
            },
            {"state": "idle"},
            {"state": "idle"},
            tcp_probe_fn=lambda ip: (False, ""),
        )
        self.assertTrue(good_disk["refresh_certs_enabled"])
        self.assertEqual(good_disk["refresh_certs_disabled_reason"], "")

        csrf = "csrf-cert-token-456"
        session_val = status_ui.sign_cookie(
            {"user": "caleb", "csrf": csrf, "exp": time.time() + 3600},
            self.cfg.session_secret,
        )
        session_cookie = f"{status_ui.SESSION_COOKIE}={session_val}"

        # Without SOPHON_CERTS disk mounted -> button disabled & /api/refresh-certs returns 409
        dash_status, _, dash_html = self._request("GET", "/", headers={"Cookie": session_cookie})
        self.assertEqual(dash_status, 200)
        self.assertRegex(dash_html, r'id="refresh-certs-btn"[^>]*\sdisabled(?:\s|>)')

        conflict_status, _, conflict_body = self._request(
            "POST",
            "/api/refresh-certs",
            headers={"Cookie": session_cookie, "X-Sophon-CSRF": csrf},
            body=b"{}",
        )
        self.assertEqual(conflict_status, 409)
        self.assertFalse((self.control_dir / "refresh-certs.request").exists())
        self.assertIn("sophon-mount-secrets", conflict_body)

        # Attach valid SOPHON_CERTS disk in appliance-state.json -> /api/recheck-certs & /api/refresh-certs succeed
        state = json.loads((self.control_dir / "appliance-state.json").read_text(encoding="utf-8"))
        state["live_cert_expires_at"] = "2026-06-01T00:00:00+00:00"
        state["cert_disk"] = {
            "present": True,
            "valid": True,
            "expires_at": "2026-07-15T00:00:00+00:00",
            "error": "",
        }
        (self.control_dir / "appliance-state.json").write_text(json.dumps(state), encoding="utf-8")

        recheck_status, _, recheck_body = self._request(
            "POST",
            "/api/recheck-certs",
            headers={"Cookie": session_cookie, "X-Sophon-CSRF": csrf},
            body=b"{}",
        )
        self.assertEqual(recheck_status, 200)
        self.assertTrue(json.loads(recheck_body)["refresh_certs_enabled"])

        ok_status, _, ok_body = self._request(
            "POST",
            "/api/refresh-certs",
            headers={"Cookie": session_cookie, "X-Sophon-CSRF": csrf},
            body=b"{}",
        )
        self.assertEqual(ok_status, 202)
        self.assertTrue(json.loads(ok_body)["ok"])
        req_payload = json.loads((self.control_dir / "refresh-certs.request").read_text(encoding="utf-8"))
        self.assertEqual(req_payload["requested_by"], "caleb")



if __name__ == "__main__":
    unittest.main()
