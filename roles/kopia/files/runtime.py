import base64
import http.cookiejar
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request

import backup


def required(environment, name):
    value = environment.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def kopia(environment, *arguments, timeout=180):
    result = subprocess.run(["kopia", "--file-log-level=info", "--disable-content-log", *arguments],
                            env=environment, capture_output=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.decode(errors="replace")
        for name in ("KOPIA_PASSWORD", "KOPIA_SERVER_PASSWORD", "KOPIA_SFTP_PRIVATE_KEY_B64"):
            if environment.get(name):
                detail = detail.replace(environment[name], "[redacted]")
        raise RuntimeError(f"Kopia {' '.join(arguments[:2])} failed: {detail.strip()}")
    return result.stdout


def health(environment):
    authorization = base64.b64encode((required(environment, "KOPIA_SERVER_USERNAME") + ":" +
                                     required(environment, "KOPIA_SERVER_PASSWORD")).encode()).decode()
    headers = {"Authorization": "Basic " + authorization}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(urllib.request.Request("http://127.0.0.1:51515/", headers=headers), timeout=10) as response:
        token = re.search(r'<meta name="kopia-csrf-token" content="([^"]+)"', response.read().decode())
    if token is None:
        raise RuntimeError("Kopia UI did not issue a CSRF token")
    headers["X-Kopia-Csrf-Token"] = token[1]
    with opener.open(urllib.request.Request("http://127.0.0.1:51515/api/v1/repo/status", headers=headers),
                     timeout=10) as response:
        if json.load(response).get("connected") is not True:
            raise RuntimeError("Kopia repository is disconnected")
    print("Repository connected")


def connect(environment):
    required(environment, "KOPIA_PASSWORD")
    host = required(environment, "KOPIA_SFTP_HOST")
    user = required(environment, "KOPIA_SFTP_USERNAME")
    path = required(environment, "KOPIA_SFTP_PATH")
    port = int(required(environment, "KOPIA_SFTP_PORT"))
    if not 1 <= port <= 65535 or not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Invalid SFTP port/path")
    credentials = Path("/tmp/credentials")
    credentials.mkdir(mode=0o700, exist_ok=True)
    for filename, variable in (("id_ed25519", "KOPIA_SFTP_PRIVATE_KEY_B64"),
                               ("known_hosts", "KOPIA_SFTP_KNOWN_HOSTS_B64")):
        material = base64.b64decode(required(environment, variable), validate=True).decode()
        destination = credentials / filename
        destination.write_text(material.rstrip() + "\n")
        destination.chmod(0o600)
    kopia(environment, "repository", "connect", "sftp", "--host=" + host, "--port=" + str(port),
          "--username=" + user, "--path=" + path, "--keyfile=/tmp/credentials/id_ed25519",
          "--known-hosts=/tmp/credentials/known_hosts", "--no-embed-credentials",
          "--no-persist-credentials", "--no-use-keyring", "--no-check-for-updates")


def start(environment):
    required(environment, "KOPIA_SERVER_PASSWORD")
    connect(environment)
    client_user = required(environment, "KOPIA_CLIENT_USERNAME")
    client_host = required(environment, "KOPIA_CLIENT_HOSTNAME")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in (client_user, client_host)):
        raise ValueError("Invalid backup source identity")
    kopia(environment, "repository", "set-client", "--username=" + client_user, "--hostname=" + client_host)
    kopia(environment, "maintenance", "set", "--owner=" + client_user + "@" + client_host,
          "--enable-quick=true", "--enable-full=true", "--quick-interval=24h", "--full-interval=168h")
    kopia(environment, "policy", "set", "--global", "--keep-latest=10", "--keep-hourly=24",
          "--keep-daily=14", "--keep-weekly=8", "--keep-monthly=12", "--keep-annual=3",
          "--snapshot-interval=0s", "--ignore-identical-snapshots=false")
    enabled = required(environment, "KOPIA_BACKUPS_ENABLED")
    if enabled not in ("true", "false"):
        raise ValueError("KOPIA_BACKUPS_ENABLED must be true or false")
    interval = required(environment, "KOPIA_SNAPSHOT_INTERVAL")
    match = re.fullmatch(r"([1-9][0-9]*)(s|m|h|d)", interval)
    if not match:
        raise ValueError("Snapshot interval must be a positive number followed by s, m, h or d")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    with backup.locked(environment.get("SOURCE_ROOT", "/source")) as root:
        backup.recover(backup.engine_api(required(environment, "ENGINE_URL")), root)
    server, worker = None, None
    shutting_down = False

    def terminate(signum, frame):
        nonlocal shutting_down
        shutting_down = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    due = time.monotonic()
    try:
        server = subprocess.Popen(["kopia", "server", "start", "--address=http://0.0.0.0:51515",
                                   "--insecure", "--ui", "--control-api", "--shutdown-grace-period=180s"],
                                  env=environment)
        while not shutting_down:
            if server.poll() is not None:
                raise RuntimeError(f"Kopia UI server exited: {server.returncode}")
            if worker is not None and worker.poll() is not None:
                if worker.returncode:
                    print(f"ERROR backup run exited {worker.returncode}; inspect /source/last-run.json",
                          file=sys.stderr, flush=True)
                    if (Path(environment.get("SOURCE_ROOT", "/source")) / "journal.json").exists():
                        raise RuntimeError("Backup recovery is pending; restart will retry before serving the UI")
                worker = None
                due = time.monotonic() + seconds
            if enabled == "true" and worker is None and time.monotonic() >= due:
                worker = subprocess.Popen(["python3", "/app/backup.py", "run"], env=environment)
            time.sleep(.5)
    finally:
        for process in (worker, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    print("ERROR forced child termination; stop recovery remains journaled", file=sys.stderr)


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
        print(str(error), file=sys.stderr)
        sys.exit(1)
