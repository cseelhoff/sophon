import base64
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import serial_console


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def make_self_signed_cert(directory: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def ws_read_frame(conn):
    header = _recv_exact(conn, 2)
    if not header:
        return None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(conn, 8))[0]
    mask = _recv_exact(conn, 4) if masked else b""
    payload = bytearray(_recv_exact(conn, length))
    if masked:
        for idx in range(length):
            payload[idx] ^= mask[idx % 4]
    return opcode, masked, bytes(payload)


def ws_send_frame(conn, opcode, payload: bytes):
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    conn.sendall(bytes(header) + payload)


def _recv_exact(conn, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = conn.recv(count - len(buf))
        if not chunk:
            raise EOFError("unexpected EOF")
        buf.extend(chunk)
    return bytes(buf)


class FakeProxmoxServer:
    def __init__(self, directory: Path):
        self.cert_path, self.key_path = make_self_signed_cert(directory)
        self.vms = [
            {"vmid": 205, "name": "sophon-restore-drill", "type": "qemu", "node": "pve1", "template": 0, "status": "running"},
            {"vmid": 206, "name": "unmanaged-vm", "type": "qemu", "node": "pve1", "template": 0, "status": "stopped"},
        ]
        self.configs = {
            205: {"description": "sophon-infravm-two-iso-v1", "serial0": "socket"},
            206: {"description": "someone-elses-vm"},
        }
        self.deleted_vmids = []
        self.received_terminal_inputs = []
        self.pong_received = threading.Event()
        self.ws_connections = 0
        self.disconnect_first_ws = False
        self._build_server()

    def _build_server(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _send_json(self, code, data):
                body = json.dumps({"data": data}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
                if parsed.path == "/api2/json/access/ticket":
                    if form.get("username") == "root@pam" and form.get("password") == "pve-secret":
                        self._send_json(200, {"ticket": "PVE:root@pam:TICKET1", "CSRFPreventionToken": "CSRF1"})
                    else:
                        self._send_json(401, None)
                    return
                if self.headers.get("Cookie") != "PVEAuthCookie=PVE:root@pam:TICKET1":
                    self._send_json(401, None)
                    return
                if self.headers.get("CSRFPreventionToken") != "CSRF1":
                    self._send_json(403, None)
                    return
                if parsed.path == "/api2/json/nodes/pve1/qemu/205/termproxy":
                    assert form.get("serial") == "serial0"
                    self._send_json(200, {"port": 5901, "ticket": "PVEVNC:TERM+TICKET/1", "user": "root@pam"})
                    return
                if parsed.path == "/api2/json/nodes/pve1/qemu/205/status/stop":
                    self._send_json(200, "UPID:pve1:0001:stop")
                    return
                self._send_json(404, None)

            def do_DELETE(self):
                parsed = urlparse(self.path)
                if self.headers.get("Cookie") != "PVEAuthCookie=PVE:root@pam:TICKET1" or self.headers.get("CSRFPreventionToken") != "CSRF1":
                    self._send_json(403, None)
                    return
                if parsed.path == "/api2/json/nodes/pve1/qemu/205":
                    parent.deleted_vmids.append(205)
                    self._send_json(200, "UPID:pve1:0002:destroy")
                    return
                self._send_json(404, None)

            def do_GET(self):
                parsed = urlparse(self.path)
                if self.headers.get("Cookie") != "PVEAuthCookie=PVE:root@pam:TICKET1":
                    self._send_json(401, None)
                    return
                if parsed.path == "/api2/json/cluster/resources":
                    self._send_json(200, parent.vms)
                    return
                if parsed.path.startswith("/api2/json/nodes/pve1/qemu/") and parsed.path.endswith("/config"):
                    vmid = int(parsed.path.split("/")[6])
                    self._send_json(200, parent.configs[vmid])
                    return
                if parsed.path.startswith("/api2/json/nodes/pve1/qemu/") and parsed.path.endswith("/status/current"):
                    vmid = int(parsed.path.split("/")[6])
                    status = next((v["status"] for v in parent.vms if v["vmid"] == vmid), "stopped")
                    self._send_json(200, {"status": status})
                    return
                if parsed.path.startswith("/api2/json/nodes/pve1/tasks/") and parsed.path.endswith("/status"):
                    self._send_json(200, {"status": "stopped", "exitstatus": "OK"})
                    return
                if parsed.path == "/api2/json/nodes/pve1/qemu/205/vncwebsocket":
                    qs = parse_qs(parsed.query)
                    assert qs["port"] == ["5901"]
                    assert qs["vncticket"] == ["PVEVNC:TERM+TICKET/1"]
                    ws_key = self.headers["Sec-WebSocket-Key"]
                    accept = base64.b64encode(hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()).decode("ascii")
                    self.send_response(101, "Switching Protocols")
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.send_header("Sec-WebSocket-Protocol", "binary")
                    self.end_headers()
                    self.close_connection = True
                    parent._handle_termproxy_ws(self.connection)
                    return
                self._send_json(404, None)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(self.cert_path), keyfile=str(self.key_path))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handle_termproxy_ws(self, conn):
        self.ws_connections += 1
        conn_num = self.ws_connections
        opcode, masked, auth_payload = ws_read_frame(conn)
        assert masked, "RFC 6455 requires client frames to be masked"
        assert auth_payload == b"root@pam:PVEVNC:TERM+TICKET/1\n"
        ws_send_frame(conn, 0x2, b"OK")

        # Read initial resize frame 1:cols:rows:
        opcode, masked, resize_payload = ws_read_frame(conn)
        assert masked and resize_payload.startswith(b"1:")

        # Send a WebSocket Ping to verify automatic Pong response
        ws_send_frame(conn, 0x9, b"ping-check")

        if self.disconnect_first_ws and conn_num == 1:
            ws_send_frame(conn, 0x2, b"\x1b[32mInstallation complete. Rebooting to the installed disk.\x1b[0m\r\n")
            ws_send_frame(conn, 0x8, b"")
            return

        ws_send_frame(conn, 0x2, b"\x1b[1m__SOPHON_PROMPT__$ \x1b[0m")
        buf = ""
        cmd_lines = []
        while True:
            try:
                frame = ws_read_frame(conn)
            except (EOFError, OSError):
                break
            if frame is None:
                break
            opcode, masked, payload = frame
            assert masked
            if opcode == 0x8:
                break
            if opcode == 0xA and payload == b"ping-check":
                self.pong_received.set()
                continue
            if payload == b"2":
                continue
            assert payload.startswith(b"0:"), f"unexpected termproxy payload: {payload!r}"
            _, length_str, rest = payload.split(b":", 2)
            assert int(length_str) == len(rest)
            text = rest.decode("utf-8")
            self.received_terminal_inputs.append(text)
            buf += text
            while "\r" in buf or "\n" in buf:
                sep_idx = min(i for i in (buf.find("\r"), buf.find("\n")) if i != -1)
                line = buf[:sep_idx]
                buf = buf[sep_idx + 1:]
                cmd_lines.append(line)
                if "__SOPHON_RC_%s__" in line:
                    full_script = "\n".join(cmd_lines)
                    cmd_lines = []
                    # Verify that the exact shell snippet sent over serial0 is valid bash syntax (including heredocs)!
                    syntax = subprocess.run(["bash", "-n"], input=full_script, text=True, capture_output=True)
                    assert syntax.returncode == 0, f"Invalid bash syntax sent over serial0: {syntax.stderr}\n{full_script}"
                    marker = line.split("'")[3]
                    rc = 7 if "exit 7" in full_script else 0
                    out = (
                        f"__SOPHON_BEGIN_{marker}__\r\n"
                        f"executed-ok\r\n"
                        f"__SOPHON_RC_{marker}__:{rc}\r\n"
                        f"__SOPHON_PROMPT__$ "
                    )
                    ws_send_frame(conn, 0x2, out.encode("utf-8"))


class SerialConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.server = FakeProxmoxServer(self.directory)
        self.addCleanup(self.server.close)

    def test_proxmox_client_enforces_vm_ownership_before_destroy(self):
        client = serial_console.ProxmoxClient(
            host="127.0.0.1",
            port=self.server.port,
            user="root@pam",
            password="pve-secret",
            validate_certs=False,
        )
        client.authenticate()
        vm = client.find_vm("pve1", "sophon-restore-drill")
        self.assertEqual(vm["vmid"], 205)

        unmanaged = client.find_vm("pve1", "unmanaged-vm")
        with self.assertRaisesRegex(RuntimeError, "not created by this workflow"):
            client.destroy_vm("pve1", unmanaged["vmid"], expected_marker="sophon-infravm-two-iso-v1")
        self.assertEqual(self.server.deleted_vmids, [])

        client.destroy_vm("pve1", 205, expected_marker="sophon-infravm-two-iso-v1")
        self.assertEqual(self.server.deleted_vmids, [205])

    def test_serial_session_frames_utf8_bytes_strips_ansi_reconnects_and_checks_exit_codes(self):
        self.server.disconnect_first_ws = True
        client = serial_console.ProxmoxClient(
            host="127.0.0.1",
            port=self.server.port,
            user="root@pam",
            password="pve-secret",
            validate_certs=True,
            ca_file=str(self.server.cert_path),
        )
        client.authenticate()
        with client.open_serial("pve1", 205) as session:
            idx, _, matched = session.expect(["Rebooting to the installed disk."], timeout=5)
            self.assertEqual(idx, 0)
            self.assertIn("Installation complete.", matched)

            # Across VM reboot disconnect, expect(reconnect=True) transparently re-establishes termproxy
            session.expect(["__SOPHON_PROMPT__\\$ "], timeout=5, reconnect=True)
            self.assertEqual(self.server.ws_connections, 2)
            self.assertTrue(self.server.pong_received.wait(timeout=2))

            # Multi-byte UTF-8 input uses byte length, not character count
            session.send("echo café\r")
            output = session.run_checked("python3 - <<'__EOF__'\nprint('heredoc-ok')\n__EOF__", timeout=5)
            self.assertEqual(output, "executed-ok")

            with self.assertRaisesRegex(RuntimeError, "exit code 7"):
                session.run_checked("sh -c 'exit 7'", timeout=5)

    def test_secret_redaction_never_exposes_registered_secrets_in_transcript(self):
        client = serial_console.ProxmoxClient(
            host="127.0.0.1",
            port=self.server.port,
            user="root@pam",
            password="pve-secret",
            validate_certs=False,
        )
        client.authenticate()
        with client.open_serial("pve1", 205) as session:
            session.register_secret("SUPER_SECRET_KOPIA_PW")
            session.expect(["__SOPHON_PROMPT__\\$ "], timeout=5)
            session._append_output("echo SUPER_SECRET_KOPIA_PW\n")
            self.assertNotIn("SUPER_SECRET_KOPIA_PW", session.recent_output())
            self.assertIn("[REDACTED]", session.recent_output())


if __name__ == "__main__":
    unittest.main()
