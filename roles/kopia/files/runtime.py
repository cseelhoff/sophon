import base64
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def required(environment, name):
    value = environment.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def boolean(environment, name, default="false"):
    value = environment.get(name, default)
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def kopia(environment, *arguments):
    result = subprocess.run(
        ["kopia", "--file-log-level=info", "--disable-content-log", *arguments],
        env=environment, capture_output=True, timeout=180,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace")
        for name in ("KOPIA_PASSWORD", "KOPIA_SERVER_PASSWORD", "KOPIA_SFTP_PRIVATE_KEY_B64"):
            if environment.get(name):
                detail = detail.replace(environment[name], "[redacted]")
        raise RuntimeError(f"Kopia {' '.join(arguments[:2])} failed: {detail.strip()}")
    return result.stdout


def prepare(environment):
    result = subprocess.run(
        ["python3", "/app/backup.py", "prepare"],
        env=environment, capture_output=True,
    )
    sys.stderr.buffer.write(result.stderr)
    if result.returncode:
        raise RuntimeError(f"Backup preparation failed with exit {result.returncode}")
    response = json.loads(result.stdout)
    root = Path(required(environment, "SOURCE_ROOT")).resolve()
    staged = Path(response["overrideRoot"])
    if staged.is_symlink() or staged.resolve().parent != root / "runs":
        raise ValueError("Collector returned an invalid snapshot staging root")
    if not (staged / "manifest.json").is_file():
        raise ValueError("Collector did not create the required manifest")
    # Kopia actions consume a plain assignment, not a JSON response.
    print(f"KOPIA_SNAPSHOT_PATH={staged.resolve()}", flush=True)


def cleanup(environment):
    root = Path(required(environment, "SOURCE_ROOT")).resolve()
    original = Path(required(environment, "KOPIA_SOURCE_PATH"))
    staged = Path(required(environment, "KOPIA_SNAPSHOT_PATH"))
    if original.resolve() != root:
        raise ValueError("Cleanup action does not belong to this snapshot source")
    if staged.resolve() == root:
        print("No overridden staging root: preparation did not complete; nothing removed.", file=sys.stderr)
        return
    if staged.is_symlink() or staged.resolve().parent != root / "runs":
        raise ValueError("Refusing to remove a path outside the snapshot run directory")
    target = staged.resolve()
    manifest = target / "manifest.json"
    if not manifest.is_file():
        raise ValueError("Refusing to remove staging without a valid backup manifest")
    data = json.loads(manifest.read_text())
    if data["version"] != 1 or data["run"] != target.name:
        raise ValueError("Refusing to remove staging without a valid backup manifest")
    shutil.rmtree(target)
    print("Removed the completed snapshot's private staging directory.", file=sys.stderr)


def health(environment):
    authorization = base64.b64encode(
        (required(environment, "KOPIA_SERVER_USERNAME") + ":" +
         required(environment, "KOPIA_SERVER_PASSWORD")).encode(),
    ).decode()
    headers = {"Authorization": "Basic " + authorization}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    address = "http://127.0.0.1:51515"
    with opener.open(urllib.request.Request(address + "/", headers=headers), timeout=10) as response:
        token = re.search(r'<meta name="kopia-csrf-token" content="([^"]+)"', response.read().decode())
    if token is None:
        raise RuntimeError("Kopia UI did not issue a CSRF token.")
    headers["X-Kopia-Csrf-Token"] = token[1]
    with opener.open(urllib.request.Request(address + "/api/v1/repo/status", headers=headers),
                     timeout=10) as response:
        if json.load(response).get("connected") is not True:
            raise RuntimeError("Kopia repository is disconnected.")
    print("Repository connected.")


def start(environment):
    password = required(environment, "KOPIA_PASSWORD")
    required(environment, "KOPIA_SERVER_PASSWORD")
    required(environment, "PORTAINER_API_KEY")
    host = required(environment, "KOPIA_SFTP_HOST")
    username = required(environment, "KOPIA_SFTP_USERNAME")
    path = required(environment, "KOPIA_SFTP_PATH")
    port = int(required(environment, "KOPIA_SFTP_PORT"))
    if not 1 <= port <= 65535:
        raise ValueError("KOPIA_SFTP_PORT is invalid")
    if not path.startswith("/") or ".." in Path(path).parts:
        raise ValueError("KOPIA_SFTP_PATH must be an absolute, non-traversing path")
    client_user = required(environment, "KOPIA_CLIENT_USERNAME")
    client_host = required(environment, "KOPIA_CLIENT_HOSTNAME")
    for value in (client_user, client_host):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError("Kopia client identity is invalid")
    initialize = boolean(environment, "KOPIA_INIT_REPOSITORY")
    enabled = boolean(environment, "KOPIA_BACKUPS_ENABLED")
    credentials = Path("/tmp/credentials")
    credentials.mkdir(mode=0o700, exist_ok=True)
    for name, variable in (
        ("id_ed25519", "KOPIA_SFTP_PRIVATE_KEY_B64"),
        ("known_hosts", "KOPIA_SFTP_KNOWN_HOSTS_B64"),
    ):
        file = credentials / name
        material = base64.b64decode(required(environment, variable), validate=True).decode()
        file.write_text(material.rstrip() + "\n")
        file.chmod(0o600)
    arguments = [
        "--host=" + host, "--port=" + str(port), "--username=" + username, "--path=" + path,
        "--keyfile=/tmp/credentials/id_ed25519", "--known-hosts=/tmp/credentials/known_hosts",
        "--no-embed-credentials", "--no-persist-credentials", "--no-use-keyring",
        "--no-check-for-updates", "--enable-actions",
    ]
    operation = "create" if initialize else "connect"
    kopia(environment, "repository", operation, "sftp", *arguments)
    kopia(environment, "repository", "set-client", "--username=" + client_user,
          "--hostname=" + client_host)
    kopia(environment, "maintenance", "set", "--owner=" + client_user + "@" + client_host,
          "--enable-quick=true", "--enable-full=true",
          "--quick-interval=24h", "--full-interval=168h")
    kopia(environment, "policy", "set", "--global",
          "--keep-latest=10", "--keep-hourly=24", "--keep-daily=14",
          "--keep-weekly=8", "--keep-monthly=12", "--keep-annual=3",
          "--ignore-identical-snapshots=false")
    interval = required(environment, "KOPIA_SNAPSHOT_INTERVAL") if enabled else "0s"
    kopia(environment, "policy", "set", environment["SOURCE_ROOT"],
          "--snapshot-interval=" + interval, "--before-snapshot-root-action=/app/prepare.sh",
          "--after-snapshot-root-action=/app/cleanup.sh",
          "--action-command-mode=essential", "--action-command-timeout=60m")
    environment["KOPIA_PASSWORD"] = password
    print(f"Repository connected; backups {'enabled' if enabled else 'disabled during configuration'}.",
          file=sys.stderr, flush=True)
    os.execvpe("kopia", [
        "kopia", "--file-log-level=info", "--disable-content-log", "server", "start",
        "--address=http://0.0.0.0:51515", "--insecure", "--ui", "--control-api", "--enable-actions",
        "--shutdown-grace-period=180s",
    ], environment)


def main(arguments, environment):
    os.umask(0o077)
    if arguments == ["prepare"]:
        prepare(environment)
    elif arguments == ["cleanup"]:
        cleanup(environment)
    elif arguments == ["health"]:
        health(environment)
    elif not arguments:
        start(environment)
    else:
        raise ValueError("Expected no arguments, prepare, cleanup or health")


if __name__ == "__main__":
    try:
        main(sys.argv[1:], dict(os.environ))
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
