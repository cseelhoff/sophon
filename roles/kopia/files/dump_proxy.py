#!/usr/bin/env python3
"""Read-only, label-bound Podman socket proxy for Kopia hot backups.

Exposes only:
  - GET  /healthz                       -> engine reachability check
  - GET  /v1/inventory                  -> sanitized container metadata (never Config.Env)
  - POST /v1/dump/<container>/<dump>    -> streams ONLY a dump command declared on <container>'s own labels

Arbitrary container exec, stop/start/kill, archive access, and environment inspection are never exposed.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import struct
import sys
import time
from urllib.parse import unquote

import backup


def sanitized_labels(labels):
    result = {}
    for key, value in (labels or {}).items():
        if isinstance(key, str) and isinstance(value, str) and (
                key.startswith(backup.PREFIX) or key == "com.docker.compose.project"):
            result[key] = value
    return result


def sanitize_container(raw):
    config = raw.get("Config") or {}
    state = raw.get("State") or {}
    mounts = [
        {
            "Type": mount.get("Type"),
            "Name": mount.get("Name"),
            "Destination": mount.get("Destination"),
            "RW": mount.get("RW", True),
        }
        for mount in (raw.get("Mounts") or [])
    ]
    return {
        "Id": raw["Id"],
        "Name": raw["Name"],
        "Image": raw["Image"],
        "State": {"Running": bool(state.get("Running"))},
        "Mounts": mounts,
        "Config": {
            "Image": config.get("Image", ""),
            "Labels": sanitized_labels(config.get("Labels")),
        },
    }


class ProxyError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class ProxyService:
    def __init__(self, podman_api):
        self.podman_api = podman_api

    def health(self):
        backup.engine(self.podman_api, "GET", "/containers/json?limit=1")
        return {"ok": True}

    def inventory(self):
        result = []
        for entry in backup.engine(self.podman_api, "GET", "/containers/json?all=true"):
            container_id = entry.get("Id", "")
            if not backup.IDENTITY.fullmatch(container_id):
                raise ProxyError(502, "Invalid container ID from container engine")
            try:
                inspected = backup.engine(self.podman_api, "GET", f"/containers/{container_id}/json")
            except backup.EngineError as error:
                if error.status == 404:
                    continue
                raise
            if inspected.get("Id") != container_id or type((inspected.get("State") or {}).get("Running")) is not bool:
                raise ProxyError(502, "Invalid container inspection response")
            result.append(sanitize_container(inspected))
        return result

    def stream_dump(self, container_id, dump_name, write_frame):
        if not backup.IDENTITY.fullmatch(container_id) or not backup.NAME.fullmatch(dump_name):
            raise ProxyError(400, "Invalid container ID or dump name")
        try:
            current = backup.engine(self.podman_api, "GET", f"/containers/{container_id}/json")
        except backup.EngineError as error:
            if error.status == 404:
                raise ProxyError(404, "Container not found") from error
            raise
        if not (current.get("State") or {}).get("Running"):
            raise ProxyError(409, "Target container is not running")
        try:
            spec = backup.parse_spec(sanitize_container(current))
        except (RuntimeError, ValueError) as error:
            raise ProxyError(403, f"Target container has invalid backup labels: {error}") from error
        if spec is None:
            raise ProxyError(403, "Target container has not opted into Sophon backups")
        matching = [item for item in spec["dumps"] if item["name"] == dump_name]
        if len(matching) != 1:
            raise ProxyError(403, f"Dump {dump_name} is not declared on target container")
        record = matching[0]
        body = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "Cmd": record["argv"],
        }
        if record.get("user"):
            body["User"] = record["user"]
        execution = backup.engine(self.podman_api, "POST", f"/containers/{container_id}/exec", body)["Id"]
        if not backup.IDENTITY.fullmatch(execution):
            raise ProxyError(502, "Invalid exec ID from container engine")
        stream = backup.engine(
            self.podman_api, "POST", f"/exec/{execution}/start",
            {"Detach": False, "Tty": False}, stream=True,
        )
        try:
            while header := stream["read"](8):
                header += backup.exact(stream, 8 - len(header))
                channel, remaining = struct.unpack(">BxxxI", header)
                if channel not in (1, 2):
                    raise ProxyError(502, "Unexpected exec stream channel")
                while remaining:
                    chunk = backup.exact(stream, min(backup.CHUNK, remaining))
                    remaining -= len(chunk)
                    write_frame(struct.pack(">BxxxI", channel, len(chunk)) + chunk)
        finally:
            stream["close"]()
        state = backup.engine(self.podman_api, "GET", f"/exec/{execution}/json")
        trailer = json.dumps({
            "running": bool(state.get("Running")),
            "exit_code": state.get("ExitCode"),
        }, separators=(",", ":")).encode("utf-8")
        write_frame(struct.pack(">BxxxI", 0, len(trailer)) + trailer)


def make_handler(service):
    dump_route = re.compile(r"/v1/dump/([a-f0-9]{64})/([^/]+)\Z")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, status, payload):
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()

        def do_GET(self):
            try:
                if self.path == "/healthz":
                    self._send_json(200, service.health())
                elif self.path == "/v1/inventory":
                    self._send_json(200, service.inventory())
                else:
                    self._send_json(403, {"error": "Endpoint not permitted by dump-proxy"})
            except ProxyError as error:
                self._send_json(error.status, {"error": str(error)})
            except Exception as error:
                self._send_json(502, {"error": str(error)})

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 0:
                self.rfile.read(min(content_length, 4096))
            match = dump_route.fullmatch(self.path)
            if not match:
                self._send_json(403, {"error": "Endpoint not permitted by dump-proxy"})
                return
            container_id, raw_name = match.group(1), unquote(match.group(2))
            headers_sent = False

            def write_frame(data):
                nonlocal headers_sent
                if not headers_sent:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True
                self.wfile.write(data)
                self.wfile.flush()

            try:
                service.stream_dump(container_id, raw_name, write_frame)
                if not headers_sent:
                    write_frame(b"")
            except ProxyError as error:
                if not headers_sent:
                    self._send_json(error.status, {"error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as error:
                if not headers_sent:
                    self._send_json(502, {"error": str(error)})

        def do_PUT(self):
            self._send_json(403, {"error": "Method not permitted by dump-proxy"})

        def do_DELETE(self):
            self._send_json(403, {"error": "Method not permitted by dump-proxy"})

    return Handler


def serve(socket_path="/socket/podman.sock", host="0.0.0.0", port=2375, timeout=900):
    service = ProxyService({"socket": socket_path, "timeout": timeout})
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True

    def shutdown_handler(signum, frame):
        raise KeyboardInterrupt

    import signal
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    os.umask(0o077)
    socket_path = os.environ.get("PODMAN_SOCKET", "/socket/podman.sock")
    listen_host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("LISTEN_PORT", "2375"))
    cmd_timeout = float(os.environ.get("COMMAND_TIMEOUT", "900"))
    try:
        serve(socket_path=socket_path, host=listen_host, port=listen_port, timeout=cmd_timeout)
    except Exception as exc:
        print(f"ERROR dump-proxy failed: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
