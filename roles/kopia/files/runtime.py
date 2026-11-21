import base64
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import backup


def required(environment, name):
    value = environment.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def control_env(environment):
    # ponytail: kopia server start reads KOPIA_SERVER_CONTROL_*, while kopia server status/snapshot read KOPIA_SERVER_*
    env = dict(environment)
    user = env.get("KOPIA_SERVER_CONTROL_USER") or env.get("KOPIA_SERVER_USERNAME") or "admin"
    password = env.get("KOPIA_SERVER_CONTROL_PASSWORD") or env.get("KOPIA_SERVER_PASSWORD") or ""
    env["KOPIA_SERVER_USERNAME"] = env["KOPIA_SERVER_CONTROL_USER"] = user
    if password:
        env["KOPIA_SERVER_PASSWORD"] = env["KOPIA_SERVER_CONTROL_PASSWORD"] = password
    return env


def kopia(environment, *arguments, timeout=180):
    result = subprocess.run(["kopia", "--file-log-level=info", "--disable-content-log", *arguments],
                            env=environment, capture_output=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.decode(errors="replace")
        for name in ("KOPIA_PASSWORD", "KOPIA_SERVER_PASSWORD", "KOPIA_SERVER_CONTROL_PASSWORD", "KOPIA_SFTP_PRIVATE_KEY_B64"):
            if environment.get(name):
                detail = detail.replace(environment[name], "[redacted]")
        raise RuntimeError(f"Kopia {' '.join(arguments[:2])} failed: {detail.strip()}")
    return result.stdout


def startup_diagnostic(error, environment):
    detail = str(error)
    for name, value in environment.items():
        if value and any(word in name for word in ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY")):
            detail = detail.replace(value, "[redacted]")
    return "KOPIA_STARTUP_ERROR_JSON=" + json.dumps({"message": detail})


def health(environment):
    kopia(control_env(environment), "server", "status", timeout=10)
    print("Repository connected")


def _server_idle(ctrl_env, target):
    lines = kopia(ctrl_env, "server", "status", timeout=10).decode(errors="replace").splitlines()
    matching = [line for line in lines if line.endswith(":" + target)]
    return not matching or all(line.startswith("IDLE:") for line in matching)


def wait_server_idle(environment, target, timeout=300, check_alive=None):
    ctrl = control_env(environment)
    deadline = time.monotonic() + timeout
    while True:
        if check_alive is not None and not check_alive():
            return False
        try:
            if _server_idle(ctrl, target):
                return True
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for Kopia server idle on {target}")
        time.sleep(0.25)


def trigger_snapshot(environment, state_root, target, timeout=86400):
    state_root = Path(state_root)
    last_run_path = state_root / "last-run.json"
    ctrl = control_env(environment)
    wait_server_idle(ctrl, target, timeout=timeout)
    with backup.locked(state_root):
        last_run_path.unlink(missing_ok=True)
    kopia(ctrl, "server", "snapshot", target, timeout=30)
    deadline = time.monotonic() + timeout
    while True:
        if last_run_path.exists() and _server_idle(ctrl, target):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for snapshot completion on {target}")
        time.sleep(0.25)
    record = json.loads(last_run_path.read_text())
    if record.get("status") != "complete" or record.get("upload_status") != "complete" or record.get("failures"):
        raise RuntimeError(f"Kopia snapshot {target} failed: {json.dumps(record.get('failures') or record)}")
    return record


def connect(environment, readonly=False, enable_actions=False):
    required(environment, "KOPIA_PASSWORD")
    host = required(environment, "KOPIA_SFTP_HOST")
    user = required(environment, "KOPIA_SFTP_USERNAME")
    path = required(environment, "KOPIA_SFTP_PATH")
    port = int(required(environment, "KOPIA_SFTP_PORT"))
    if not 1 <= port <= 65535 or not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Invalid SFTP port/path")
    is_readonly = readonly or environment.get("KOPIA_READONLY", "").strip().lower() == "true"
    credentials = Path("/tmp/credentials")
    credentials.mkdir(mode=0o700, exist_ok=True)
    for filename, variable in (("id_ed25519", "KOPIA_SFTP_PRIVATE_KEY_B64"),
                               ("known_hosts", "KOPIA_SFTP_KNOWN_HOSTS_B64")):
        material = base64.b64decode(required(environment, variable), validate=True).decode()
        destination = credentials / filename
        destination.write_text(material.rstrip() + "\n")
        destination.chmod(0o600)
    arguments = [
        "repository", "connect", "sftp", "--host=" + host, "--port=" + str(port),
        "--username=" + user, "--path=" + path, "--keyfile=/tmp/credentials/id_ed25519",
        "--known-hosts=/tmp/credentials/known_hosts", "--no-embed-credentials",
        "--no-persist-credentials", "--no-use-keyring", "--no-check-for-updates",
    ]
    if is_readonly:
        arguments.append("--readonly")
    elif enable_actions:
        arguments.append("--enable-actions")
    kopia(environment, *arguments)


def _trigger_initial_snapshot(server_env, state_root, target, server, is_shutting_down):
    def alive():
        if is_shutting_down():
            return False
        if server.poll() is not None:
            raise RuntimeError(f"Kopia server exited: {server.returncode}")
        return True

    if not wait_server_idle(server_env, target, timeout=30, check_alive=alive):
        return
    if not (state_root / "last-run.json").exists():
        kopia(server_env, "server", "snapshot", target, timeout=30)


def run_server(server_cmd, server_env, initial_target=None, state_root=None):
    server = None
    shutting_down = False

    def terminate(signum, frame):
        nonlocal shutting_down
        shutting_down = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        server = subprocess.Popen(server_cmd, env=server_env)
        if initial_target and state_root is not None:
            _trigger_initial_snapshot(server_env, state_root, initial_target, server, lambda: shutting_down)
        while not shutting_down:
            if server.poll() is not None:
                raise RuntimeError(f"Kopia server exited: {server.returncode}")
            time.sleep(0.5)
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=180)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def start(environment):
    required(environment, "KOPIA_SERVER_PASSWORD")
    readonly = environment.get("KOPIA_READONLY", "false").strip().lower()
    if readonly not in ("true", "false"):
        raise ValueError("KOPIA_READONLY must be true or false")
    enabled = required(environment, "KOPIA_BACKUPS_ENABLED")
    if enabled not in ("true", "false"):
        raise ValueError("KOPIA_BACKUPS_ENABLED must be true or false")
    if readonly == "true" and enabled == "true":
        raise ValueError("KOPIA_BACKUPS_ENABLED must be false when KOPIA_READONLY=true")
    client_user = required(environment, "KOPIA_CLIENT_USERNAME")
    client_host = required(environment, "KOPIA_CLIENT_HOSTNAME")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in (client_user, client_host)):
        raise ValueError("Invalid backup source identity")
    interval = required(environment, "KOPIA_SNAPSHOT_INTERVAL")
    if not re.fullmatch(r"([1-9][0-9]*)(s|m|h|d)", interval):
        raise ValueError("Snapshot interval must be a positive number followed by s, m, h or d")
    action_timeout = int(environment.get("COMMAND_TIMEOUT", "900"))
    if action_timeout <= 0:
        raise ValueError("COMMAND_TIMEOUT must be positive")
    backup.engine_api(required(environment, "ENGINE_URL"), action_timeout)

    server_env = control_env(environment)
    source_root = Path(server_env.get("SOURCE_ROOT", "/source"))
    with backup.locked(source_root) as root:
        backup.cleanup_stale_runs(root)
        if readonly != "true" and enabled == "true":
            (root / "last-run.json").unlink(missing_ok=True)

    connect(server_env, readonly=(readonly == "true"), enable_actions=(readonly != "true"))
    if readonly != "true":
        kopia(server_env, "repository", "set-client", "--username=" + client_user, "--hostname=" + client_host)
        kopia(server_env, "maintenance", "set", "--owner=" + client_user + "@" + client_host,
              "--enable-quick=true", "--enable-full=true", "--quick-interval=24h", "--full-interval=168h")
        kopia(server_env, "policy", "set", "--global", "--keep-latest=10", "--keep-hourly=24",
              "--keep-daily=14", "--keep-weekly=8", "--keep-monthly=12", "--keep-annual=3",
              "--snapshot-interval=0s", "--ignore-identical-snapshots=false")
        kopia(
            server_env, "policy", "set", "/sophon-state",
            "--before-snapshot-root-action=/app/backup.py",
            "--after-snapshot-root-action=/app/backup.py",
            "--no-persist-action-script",
            "--action-command-mode=essential",
            "--action-command-timeout=" + str(action_timeout) + "s",
            "--snapshot-interval=" + (interval if enabled == "true" else "0s"),
        )

    server_cmd = [
        "kopia", "server", "start",
        "--address=http://0.0.0.0:51515",
        "--insecure", "--ui", "--control-api",
        "--shutdown-grace-period=180s",
    ]
    if readonly != "true":
        server_cmd.append("--enable-actions")
    run_server(
        server_cmd, server_env,
        initial_target="/sophon-state" if (readonly != "true" and enabled == "true") else None,
        state_root=source_root,
    )


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if sys.argv[1:] == ["health"]:
            health(dict(os.environ))
        elif not sys.argv[1:]:
            start(dict(os.environ))
        else:
            raise ValueError("Expected no arguments or health")
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(startup_diagnostic(error, dict(os.environ)), file=sys.stderr)
        sys.exit(1)
