"""Proxmox HTTPS API and serial0 (`termproxy` + `vncwebsocket`) driver.

Uses only the Python standard library so `sophon-tools` needs no extra WebSocket
dependencies while preserving InfraVM's console-only (`serial0`) security boundary.
"""

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import select
import socket
import ssl
import struct
import time
from urllib.parse import quote, urlencode

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|[@-Z\\^_])")
MAX_TRANSCRIPT_CHARS = 65536
KEEPALIVE_INTERVAL_SEC = 15.0
INCOMPLETE_ANSI_TAIL_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*|\][^\x07\x1B]*(?:\x1B)?)?\Z")
SERIAL_TX_CHUNK_CHARS = 48
SERIAL_TX_CHUNK_DELAY_SEC = 0.015


def build_ssl_context(*, validate_certs=False, ca_file=None):
    if validate_certs or ca_file:
        ctx = ssl.create_default_context(cafile=ca_file if ca_file else None)
        if not validate_certs:
            ctx.check_hostname = False
        return ctx
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class WebSocketClosedError(EOFError):
    """Raised when the remote WebSocket closes or drops."""


class WebSocketClient:
    """Minimal RFC 6455 client over TLS supporting binary/text frames and Ping/Pong."""

    def __init__(self, sock):
        self._sock = sock
        self._rx_buf = bytearray()
        self._closed = False

    @classmethod
    def connect(cls, host, port, path, *, headers=None, ssl_context=None, server_hostname=None, timeout=30.0):
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        ctx = ssl_context or build_ssl_context(validate_certs=True)
        tls_sock = ctx.wrap_socket(raw, server_hostname=server_hostname or host)
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

        req_headers = {
            "Host": f"{host}:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": ws_key,
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Protocol": "binary",
            "Origin": f"https://{host}:{port}",
        }
        if headers:
            req_headers.update(headers)
        lines = [f"GET {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in req_headers.items()] + ["", ""]
        tls_sock.sendall("\r\n".join(lines).encode("utf-8"))

        response_buf = bytearray()
        while b"\r\n\r\n" not in response_buf:
            chunk = tls_sock.recv(4096)
            if not chunk:
                tls_sock.close()
                raise WebSocketClosedError("EOF during WebSocket HTTP upgrade")
            response_buf.extend(chunk)
            if len(response_buf) > 65536:
                tls_sock.close()
                raise RuntimeError("Oversized WebSocket HTTP upgrade response")

        header_bytes, leftover = bytes(response_buf).split(b"\r\n\r\n", 1)
        header_lines = header_bytes.decode("iso-8859-1").split("\r\n")
        status_line = header_lines[0]
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            tls_sock.close()
            raise RuntimeError(f"WebSocket upgrade rejected: {status_line}")

        resp_headers = {}
        for line in header_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()
        if resp_headers.get("sec-websocket-accept") != expected_accept:
            tls_sock.close()
            raise RuntimeError("Invalid Sec-WebSocket-Accept header from server")

        client = cls(tls_sock)
        client._rx_buf.extend(leftover)
        return client

    def send_frame(self, payload: bytes, opcode: int = 0x2):
        if self._closed:
            raise WebSocketClosedError("WebSocket is closed")
        mask = os.urandom(4)
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytearray(payload)
        for idx in range(length):
            masked[idx] ^= mask[idx % 4]
        self._sock.sendall(bytes(header) + mask + bytes(masked))

    def recv_frame(self, timeout: float):
        """Return `(opcode, payload_bytes)`, or `None` on timeout before a full frame arrives."""
        deadline = time.monotonic() + max(0.0, timeout)
        fragments = bytearray()
        fragment_opcode = None
        while True:
            frame = self._try_parse_buffered_frame()
            if frame is not None:
                fin, opcode, payload = frame
                if opcode == 0x8:
                    self._closed = True
                    raise WebSocketClosedError("WebSocket closed by server")
                if opcode == 0x9:
                    self.send_frame(payload, opcode=0xA)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    if fin:
                        return opcode, payload
                    fragment_opcode = opcode
                    fragments.extend(payload)
                    continue
                if opcode == 0x0 and fragment_opcode is not None:
                    fragments.extend(payload)
                    if fin:
                        return fragment_opcode, bytes(fragments)
                    continue
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if hasattr(self._sock, "pending") and self._sock.pending() > 0:
                readable = True
            else:
                readable = bool(select.select([self._sock], [], [], min(remaining, 0.5))[0])
            if not readable:
                continue
            self._sock.settimeout(max(0.5, min(remaining, 5.0)))
            try:
                chunk = self._sock.recv(16384)
            except TimeoutError:
                continue
            except ssl.SSLWantReadError:
                continue
            if not chunk:
                self._closed = True
                raise WebSocketClosedError("WebSocket connection closed")
            self._rx_buf.extend(chunk)

    def _try_parse_buffered_frame(self):
        buf = self._rx_buf
        if len(buf) < 2:
            return None
        b1, b2 = buf[0], buf[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < offset + 2:
                return None
            length = struct.unpack("!H", buf[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return None
            length = struct.unpack("!Q", buf[offset:offset + 8])[0]
            offset += 8
        mask = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            mask = bytes(buf[offset:offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = bytearray(buf[offset:offset + length])
        del buf[:offset + length]
        if masked:
            for idx in range(length):
                payload[idx] ^= mask[idx % 4]
        return fin, opcode, bytes(payload)

    def close(self):
        if not self._closed:
            try:
                self.send_frame(b"", opcode=0x8)
            except (OSError, WebSocketClosedError):
                pass
            self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class ProxmoxClient:
    """Thin HTTPS client for Proxmox VE guest lifecycle and `serial0` terminal sessions."""

    def __init__(self, host, *, port=8006, user="root@pam", password="", validate_certs=False, ca_file=None):
        if not host or not password:
            raise ValueError("Proxmox host and password are required")
        self.host = host
        self.port = int(port)
        self.user = user if "@" in user else f"{user}@pam"
        self.password = password
        self.validate_certs = bool(validate_certs)
        self.ca_file = ca_file or None
        self.ssl_context = build_ssl_context(validate_certs=self.validate_certs, ca_file=self.ca_file)
        self.ticket = None
        self.csrf = None
        self._ticket_time = 0.0

    def authenticate(self):
        data = self._raw_request(
            "POST",
            "/api2/json/access/ticket",
            form={"username": self.user, "password": self.password},
            authenticated=False,
        )
        if not isinstance(data, dict) or not data.get("ticket") or not data.get("CSRFPreventionToken"):
            raise RuntimeError("Proxmox authentication failed")
        self.ticket = data["ticket"]
        self.csrf = data["CSRFPreventionToken"]
        self._ticket_time = time.monotonic()

    def ensure_authenticated(self):
        # Proxmox tickets last 2 hours; refresh proactively after 90 minutes.
        if not self.ticket or (time.monotonic() - self._ticket_time) > 5400:
            self.authenticate()

    def request(self, method, path, *, form=None, query=None):
        self.ensure_authenticated()
        return self._raw_request(method, path, form=form, query=query, authenticated=True)

    def _raw_request(self, method, path, *, form=None, query=None, authenticated=True):
        full_path = path
        if query:
            full_path = f"{path}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        body = None
        if authenticated:
            headers["Cookie"] = f"PVEAuthCookie={self.ticket}"
            if method != "GET":
                headers["CSRFPreventionToken"] = self.csrf
        if form is not None:
            body = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=30, context=self.ssl_context)
        try:
            conn.request(method, full_path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status >= 400:
                raise RuntimeError(f"Proxmox API {method} {path} failed with HTTP {resp.status}")
            payload = json.loads(raw) if raw else {}
            return payload.get("data")
        finally:
            conn.close()

    def find_vm(self, node, name):
        resources = self.request("GET", "/api2/json/cluster/resources", query={"type": "vm"}) or []
        matches = [vm for vm in resources if vm.get("name") == name]
        if not matches:
            return None
        if len(matches) > 1:
            raise RuntimeError(f"Multiple Proxmox guests share the name {name!r}; refusing to proceed.")
        vm = matches[0]
        if vm.get("type") != "qemu" or int(vm.get("template", 0)) != 0 or vm.get("node") != node:
            raise RuntimeError(f"Incompatible guest already uses {name!r}; refusing to modify it.")
        return vm

    def vm_config(self, node, vmid):
        return self.request("GET", f"/api2/json/nodes/{quote(str(node))}/qemu/{int(vmid)}/config") or {}

    def vm_status(self, node, vmid):
        return self.request("GET", f"/api2/json/nodes/{quote(str(node))}/qemu/{int(vmid)}/status/current") or {}

    def wait_task(self, node, upid, timeout=300.0):
        if not upid:
            return
        deadline = time.monotonic() + timeout
        encoded_upid = quote(str(upid), safe="")
        while time.monotonic() < deadline:
            status = self.request("GET", f"/api2/json/nodes/{quote(str(node))}/tasks/{encoded_upid}/status") or {}
            if status.get("status") == "stopped":
                exit_status = status.get("exitstatus", "OK")
                if exit_status != "OK":
                    raise RuntimeError(f"Proxmox task {upid} failed: {exit_status}")
                return
            time.sleep(1.0)
        raise TimeoutError(f"Timed out waiting for Proxmox task {upid}")

    def set_onboot(self, node, vmid, enabled: bool, *, expected_marker=None):
        if expected_marker is not None:
            self.assert_owned_vm(node, vmid, expected_marker)
        self.request(
            "POST",
            f"/api2/json/nodes/{quote(str(node))}/qemu/{int(vmid)}/config",
            form={"onboot": "1" if enabled else "0"},
        )

    def stop_vm(self, node, vmid, *, expected_marker=None, timeout=180.0):
        if expected_marker is not None:
            self.assert_owned_vm(node, vmid, expected_marker)
        upid = self.request("POST", f"/api2/json/nodes/{quote(str(node))}/qemu/{int(vmid)}/status/stop")
        self.wait_task(node, upid, timeout=timeout)

    def destroy_vm(self, node, vmid, *, expected_marker, timeout=300.0):
        self.assert_owned_vm(node, vmid, expected_marker)
        status = self.vm_status(node, vmid)
        if status.get("status") == "running":
            self.stop_vm(node, vmid, timeout=min(timeout, 180.0))
        upid = self.request(
            "DELETE",
            f"/api2/json/nodes/{quote(str(node))}/qemu/{int(vmid)}",
            query={"purge": "1", "destroy-unreferenced-disks": "1"},
        )
        self.wait_task(node, upid, timeout=timeout)

    def assert_owned_vm(self, node, vmid, expected_marker):
        cfg = self.vm_config(node, vmid)
        if cfg.get("description", "") != expected_marker:
            raise RuntimeError(
                f"VM {vmid} was not created by this workflow (expected marker {expected_marker!r})."
            )

    def open_serial(self, node, vmid, *, serial="serial0", cols=120, rows=40):
        session = ProxmoxSerialSession(self, node, int(vmid), serial=serial, cols=cols, rows=rows)
        session.connect()
        return session


class ProxmoxSerialSession:
    """Interactive `expect`/`send` session over Proxmox `termproxy` + `vncwebsocket`."""

    def __init__(self, client: ProxmoxClient, node: str, vmid: int, *, serial="serial0", cols=120, rows=40):
        self.client = client
        self.node = str(node)
        self.vmid = int(vmid)
        self.serial = serial
        self.cols = int(cols)
        self.rows = int(rows)
        self._ws = None
        self._raw_tail = ""
        self._unread = ""
        self._transcript = ""
        self._secrets = set()
        self._last_keepalive = 0.0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def register_secret(self, value: str):
        if value and len(value) >= 4:
            self._secrets.add(value)

    def _redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text

    def recent_output(self, limit: int = 4000) -> str:
        return self._redact(self._transcript[-limit:])

    def connect(self):
        self.close()
        self.client.ensure_authenticated()
        term = self.client.request(
            "POST",
            f"/api2/json/nodes/{quote(self.node)}/qemu/{self.vmid}/termproxy",
            form={"serial": self.serial},
        )
        if not isinstance(term, dict) or "port" not in term or "ticket" not in term:
            raise RuntimeError("Proxmox termproxy did not return port and ticket")
        ws_path = (
            f"/api2/json/nodes/{quote(self.node)}/qemu/{self.vmid}/vncwebsocket"
            f"?port={quote(str(term['port']), safe='')}&vncticket={quote(str(term['ticket']), safe='')}"
        )
        ws = WebSocketClient.connect(
            self.client.host,
            self.client.port,
            ws_path,
            headers={"Cookie": f"PVEAuthCookie={self.client.ticket}"},
            ssl_context=self.client.ssl_context,
        )
        auth_user = term.get("user") or self.client.user
        ws.send_frame(f"{auth_user}:{term['ticket']}\n".encode("utf-8"), opcode=0x2)

        # Proxmox termproxy replies with "OK" once the ticket handshake succeeds.
        handshake = bytearray()
        deadline = time.monotonic() + 15.0
        while b"OK" not in handshake:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ws.close()
                raise TimeoutError("Timed out waiting for Proxmox termproxy OK handshake")
            frame = ws.recv_frame(timeout=min(remaining, 2.0))
            if frame is None:
                continue
            _, data = frame
            handshake.extend(data)

        ok_pos = bytes(handshake).index(b"OK") + 2
        initial_output = bytes(handshake)[ok_pos:]
        self._ws = ws
        self._last_keepalive = time.monotonic()
        self.resize(self.cols, self.rows)
        if initial_output:
            self._append_output(initial_output.decode("utf-8", "replace"))

    def resize(self, cols: int, rows: int):
        if self._ws is not None:
            self._ws.send_frame(f"1:{int(cols)}:{int(rows)}:".encode("ascii"), opcode=0x2)

    def send(self, text: str, *, secret: bool = False):
        if secret:
            self.register_secret(text.rstrip("\r\n"))
        # Pace serial writes into small frames so QEMU's virtual 16550A UART RX FIFO never drops bytes on heredocs.
        for offset in range(0, len(text), SERIAL_TX_CHUNK_CHARS):
            chunk = text[offset:offset + SERIAL_TX_CHUNK_CHARS].encode("utf-8")
            frame = f"0:{len(chunk)}:".encode("ascii") + chunk
            self._ws.send_frame(frame, opcode=0x2)
            if offset + SERIAL_TX_CHUNK_CHARS < len(text):
                time.sleep(SERIAL_TX_CHUNK_DELAY_SEC)
        self._last_keepalive = time.monotonic()

    def send_line(self, line: str = "", *, secret: bool = False):
        self.send(line + "\r", secret=secret)

    def _append_output(self, raw_text: str):
        combined = self._raw_tail + raw_text
        self._raw_tail = ""
        stripped = ANSI_ESCAPE_RE.sub("", combined)
        tail_match = INCOMPLETE_ANSI_TAIL_RE.search(stripped)
        if tail_match is not None and len(tail_match.group(0)) < 512:
            self._raw_tail = tail_match.group(0)
            stripped = stripped[:tail_match.start()]
        cleaned = stripped.replace("\r\n", "\n").replace("\r", "\n")
        self._unread += cleaned
        if len(self._unread) > MAX_TRANSCRIPT_CHARS:
            self._unread = self._unread[-MAX_TRANSCRIPT_CHARS:]
        self._transcript = self._redact(self._transcript + cleaned)
        if len(self._transcript) > MAX_TRANSCRIPT_CHARS:
            self._transcript = self._transcript[-MAX_TRANSCRIPT_CHARS:]

    def expect(self, patterns, *, timeout: float = 60.0, reconnect: bool = False, nudge_interval: float = 0.0):
        """Wait for any regex pattern in `patterns` to match unread serial output."""
        compiled = [re.compile(p, re.MULTILINE) if isinstance(p, str) else p for p in patterns]
        deadline = time.monotonic() + timeout
        last_nudge = time.monotonic()

        while True:
            best = None
            for idx, regex in enumerate(compiled):
                match = regex.search(self._unread)
                if match is not None and (best is None or match.start() < best[1].start()):
                    best = (idx, match)
            if best is not None:
                idx, match = best
                consumed = self._unread[:match.end()]
                self._unread = self._unread[match.end():]
                return idx, match, consumed

            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout:.0f}s waiting for {[r.pattern for r in compiled]!r}.\n"
                    f"Recent serial output:\n{self.recent_output(1500)}"
                )

            if self._ws is None:
                if not reconnect:
                    raise WebSocketClosedError("Serial WebSocket is disconnected")
                time.sleep(min(2.0, remaining))
                try:
                    self.connect()
                except (OSError, RuntimeError, TimeoutError):
                    continue

            if now - self._last_keepalive >= KEEPALIVE_INTERVAL_SEC:
                try:
                    self._ws.send_frame(b"2", opcode=0x2)
                    self._last_keepalive = now
                except OSError:
                    if not reconnect:
                        raise
                    self.close()
                    continue

            if nudge_interval > 0 and (now - last_nudge) >= nudge_interval:
                try:
                    self.send("\r")
                    last_nudge = now
                except OSError:
                    if not reconnect:
                        raise
                    self.close()
                    continue

            try:
                frame = self._ws.recv_frame(timeout=min(remaining, 1.0))
            except (WebSocketClosedError, OSError):
                self.close()
                if reconnect:
                    continue
                raise

            if frame is not None:
                _, payload = frame
                if payload:
                    self._append_output(payload.decode("utf-8", "replace"))

    def run_checked(self, command: str, *, timeout: float = 600.0, prompt_pattern: str = r"__SOPHON_PROMPT__\$ "):
        nonce = secrets.token_hex(6)
        begin_marker = f"__SOPHON_BEGIN_{nonce}__"
        rc_regex = rf"__SOPHON_RC_{nonce}__:(\d+)"
        wrapped = (
            "{\n"
            f"printf '__SOPHON_BEGIN_%s__\\n' '{nonce}'\n"
            f"(\n{command}\n)\n"
            f"printf '\\n__SOPHON_RC_%s__:%d\\n' '{nonce}' $?; }}"
        )
        self.send_line(wrapped)
        _, match, before = self.expect([rc_regex], timeout=timeout, reconnect=True)
        exit_code = int(match.group(1))
        if prompt_pattern:
            self.expect([prompt_pattern], timeout=30.0, reconnect=True)
        body = before
        if begin_marker in body:
            body = body.split(begin_marker, 1)[1]
        rc_pos = body.rfind(f"__SOPHON_RC_{nonce}__:")
        if rc_pos != -1:
            body = body[:rc_pos]
        if exit_code != 0:
            raise RuntimeError(
                f"Guest command failed with exit code {exit_code}: {self._redact(command)}\n"
                f"Output:\n{self._redact(body[-2000:])}"
            )
        return body.strip()

    def close(self):
        if self._ws is not None:
            self._ws.close()
            self._ws = None
