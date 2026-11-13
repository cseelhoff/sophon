#!/usr/bin/env python3
"""Disposable M1 dev check: real native exports and a v3 snapshot without Portainer."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

import yaml


def run(args):
    root = Path(args.rendered).resolve()
    prefix = "sophon-m1-" + uuid.uuid4().hex[:10]
    containers, volumes = [], []
    api = None
    environment = dict(os.environ)
    environment.update(POSTGRES_PASSWORD=secrets.token_urlsafe(24),
                       LDAP_INIT_ROOT_USER_PW=secrets.token_urlsafe(24),
                       KOPIA_PASSWORD=secrets.token_urlsafe(32),
                       KOPIA_SERVER_PASSWORD=secrets.token_urlsafe(24),
                       KOPIA_SERVER_USERNAME="admin", KOPIA_SFTP_HOST="host.containers.internal",
                       KOPIA_SFTP_PORT="22", KOPIA_SFTP_USERNAME=os.environ["USER"],
                       KOPIA_CLIENT_USERNAME="backup", KOPIA_CLIENT_HOSTNAME="m1-dev",
                       KOPIA_BACKUPS_ENABLED="false", KOPIA_SNAPSHOT_INTERVAL="6h")
    report = {"portainer_present": False, "checks": {}, "cleanup": []}

    def redact(value):
        for key, secret in environment.items():
            if any(word in key for word in ("PASSWORD", "_PW", "PRIVATE_KEY")) and secret:
                value = value.replace(secret, "[redacted]")
        return value

    def command(argv, *, check=True, input=None):
        result = subprocess.run(argv, env=environment, input=input, capture_output=True, text=True, timeout=180)
        if check and result.returncode:
            raise RuntimeError(redact(f"{argv}: {result.stdout[-1000:]} {result.stderr[-2000:]}"))
        return result

    def podman(*argv, **kwargs):
        return command(["podman", "--cgroup-manager=cgroupfs", *argv], **kwargs)

    def wait(argv):
        for _ in range(90):
            result = podman(*argv, check=False)
            if result.returncode == 0:
                return result.stdout
            time.sleep(1)
        raise RuntimeError("Dev producer/service readiness failed: " + redact(result.stderr))

    def labels(service, replacements):
        result = service["labels"]
        if isinstance(result, list):
            result = dict(item.split("=", 1) for item in result)
        flags = []
        for key, value in result.items():
            if not key.startswith("sophon.backup."):
                continue
            value = str(value).replace("$$", "$")
            for old, new in replacements.items():
                value = value.replace(old, new)
            flags.extend(["--label", key + "=" + value])
        return flags

    def start(name, image, flags, argv=()):
        containers.append(name)
        podman("run", "-d", "--name", name, "--network", front, *flags, image, *argv)

    if "true" != podman("info", "--format", "{{.Host.Security.Rootless}}").stdout.strip():
        raise RuntimeError("Dev check requires rootless Podman")
    if "portainer" in podman("ps", "-a", "--format", "{{.Names}}").stdout.lower():
        raise RuntimeError("This fixture must run on dev without Portainer")
    with tempfile.TemporaryDirectory(prefix=prefix + "-", dir=args.sftp_root) as directory:
        directory = Path(directory)
        configuration = directory / "containers.conf"
        configuration.write_text('[engine]\ncgroup_manager="cgroupfs"\n')
        environment["CONTAINERS_CONF"] = str(configuration)
        front = prefix + "-front"
        data, acme, slow = prefix + "-data", prefix + "-acme", prefix + "-slow"
        project = directory / "project"
        proxy_config = project / "roles/kopia/files/engine-proxy.cfg"
        proxy_config.parent.mkdir(parents=True)
        shutil.copy(root / "engine-proxy.cfg", proxy_config)
        environment["SOPHON_PROJECT_PATH"] = str(project)
        repository = directory / "repository"
        repository.mkdir()
        environment["KOPIA_SFTP_PATH"] = str(repository)
        environment["KOPIA_SFTP_PRIVATE_KEY_B64"] = base64.b64encode(Path(args.key).read_bytes()).decode()
        environment["KOPIA_SFTP_KNOWN_HOSTS_B64"] = base64.b64encode(Path(args.known_hosts).read_bytes()).decode()
        compose_text = (root / "kopia.yml").read_text().replace("SOPHON_M1_DATA", data).replace("SOPHON_M1_ACME", acme)
        compose = yaml.safe_load(compose_text)
        compose["networks"]["proxy"]["name"] = front
        kopia = prefix + "-kopia"
        compose["services"]["kopia"]["container_name"] = kopia
        compose["services"]["engine-proxy"]["container_name"] = prefix + "-engine"
        compose["volumes"]["fixture-slow"] = {"external": True, "name": slow}
        compose["services"]["kopia"]["volumes"].append("fixture-slow:/volumes/" + slow + ":ro")
        mapping = json.loads(compose["services"]["kopia"]["environment"]["BACKUP_VOLUMES"])
        mapping[slow] = "/volumes/" + slow
        compose["services"]["kopia"]["environment"]["BACKUP_VOLUMES"] = json.dumps(mapping)
        compose_file = directory / "compose.yml"
        compose_file.write_text(yaml.safe_dump(compose))
        compose_command = ["podman-compose", "-p", prefix, "-f", str(compose_file)]
        started = False
        try:
            podman("network", "create", front)
            for volume in (data, acme, slow):
                podman("volume", "create", volume)
                volumes.append(volume)
            initialize = (
                "import os; from pathlib import Path; p=Path('/data'); "
                "(p/'acme.json').write_text('{\"dev-generated-certificate-state\":true}'); "
                "os.chmod(p/'acme.json',0o600)"
            )
            podman("run", "--rm", "--network", "none", "-v", acme + ":/data",
                   "docker.io/library/python:3.14.10-slim-trixie", "python3", "-c", initialize)
            traefik = yaml.safe_load((root / "traefik.yml").read_text())["services"]["traefik"]
            start(prefix + "-certificate-writer", "docker.io/library/python:3.14.10-slim-trixie",
                  ["-v", acme + ":/data", *labels(traefik, {"traefik_traefik_data": acme})],
                  ["python3", "-c", "import signal,sys,time;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));time.sleep(3600)"])
            gitea = yaml.safe_load((root / "gitea.yml").read_text())["services"]
            database = prefix + "-database"
            start(database, "docker.io/library/postgres:18.6-alpine3.24",
                  ["-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=gitea", "-e", "POSTGRES_USER=fixture",
                   *labels(gitea["gitea-db"], {})])
            wait(["exec", database, "pg_isready", "-U", "fixture", "-d", "gitea"])
            podman("exec", database, "psql", "-U", "fixture", "-d", "gitea", "-c",
                   "create table m1_fixture(value text); insert into m1_fixture values ('native-export-state');")
            environment["GITEA__database__PASSWD"] = environment["POSTGRES_PASSWORD"]
            application = prefix + "-gitea"
            start(application, "docker.io/gitea/gitea:28.2.0", [
                "-v", data + ":/data", "-e", "GITEA__database__DB_TYPE=postgres",
                "-e", "GITEA__database__HOST=" + database + ":5432", "-e", "GITEA__database__NAME=gitea",
                "-e", "GITEA__database__USER=fixture", "-e", "GITEA__database__PASSWD",
                "-e", "GITEA__security__INSTALL_LOCK=true", "-e", "GITEA__actions__ENABLED=false",
                "-e", "GITEA__server__OFFLINE_MODE=true", "-e", "NEVER_CAPTURE_ENV=M1_ENV_MARKER",
                *labels(gitea["gitea"], {"gitea_gitea_data": data})])
            wait(["exec", application, "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:3000/api/v1/version"])
            podman("exec", "-u", "1000", application, "sh", "-ec",
                   "mkdir /tmp/m1-work; cd /tmp/m1-work; git init; printf 'irreproducible fixture\\n' > state; "
                   "git add state; git -c user.name=M1Fixture -c user.email=fixture@invalid commit -m fixture; "
                   "git clone --bare . /data/git/repositories/m1.git; "
                   "ln -s /etc/passwd /data/git/repositories/m1.git/external-link")
            ldap_service = yaml.safe_load((root / "openldap.yml").read_text())["services"]["ldap"]
            ldap = prefix + "-ldap"
            start(ldap, "docker.io/vegardit/openldap:2.6.10", [
                "-e", "LDAP_INIT_ORG_DN=dc=dev,dc=invalid", "-e", "LDAP_INIT_ORG_NAME=dev",
                "-e", "LDAP_INIT_ROOT_USER_DN=cn=admin,dc=dev,dc=invalid", "-e", "LDAP_INIT_ROOT_USER_PW",
                "-e", "LDAP_TLS_ENABLED=false", "-e", "LDAP_LDAPS_ENABLED=false", *labels(ldap_service, {})])
            wait(["exec", ldap, "ldapsearch", "-Q", "-Y", "EXTERNAL", "-H", "ldapi:///", "-b", "", "-s", "base", "namingContexts"])
            podman("run", "--rm", "--network", "slirp4netns:allow_host_loopback=true", "-e", "KOPIA_PASSWORD",
                   "-e", "KOPIA_CONFIG_PATH=/tmp/repo.config", "-e", "KOPIA_CACHE_DIRECTORY=/tmp/cache",
                   "-e", "KOPIA_LOG_DIR=/tmp/logs", "-v", args.key + ":/keys/key:ro",
                   "-v", args.known_hosts + ":/keys/known_hosts:ro", "docker.io/kopia/kopia:0.23.1",
                   "repository", "create", "sftp", "--host=host.containers.internal", "--username=" + os.environ["USER"],
                   "--path=" + str(repository), "--keyfile=/keys/key", "--known-hosts=/keys/known_hosts",
                   "--no-persist-credentials", "--no-use-keyring")
            socket_path = Path(os.environ["XDG_RUNTIME_DIR"]) / "podman/podman.sock"
            socket_path.parent.mkdir(exist_ok=True)
            if socket_path.exists():
                raise RuntimeError("Dev API socket already exists; refusing to replace an unknown service")
            api = subprocess.Popen(["podman", "--cgroup-manager=cgroupfs", "system", "service",
                                    "--time=0", "unix:" + str(socket_path)], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if socket_path.exists():
                    break
                time.sleep(.1)
            started = True
            command(compose_command + ["up", "-d"])
            wait(["exec", kopia, "python3", "/app/runtime.py", "health"])
            result = podman("exec", kopia, "python3", "/app/backup.py", "run")
            report["checks"]["complete_run"] = json.loads(result.stdout)
            latest = json.loads(podman("exec", kopia, "cat", "/source/last-run.json").stdout)
            report["checks"]["groups"] = [group["name"] for group in latest["groups"]]
            assert latest["status"] == "complete", latest["failures"]
            assert "M1_ENV_MARKER" not in json.dumps(latest)
            snapshot = latest["snapshot"]["id"]
            podman("exec", kopia, "kopia", "snapshot", "restore", snapshot, "/tmp/m1-restored")
            check = (
                "import hashlib,json,os,zlib;from pathlib import Path;p=Path('/tmp/m1-restored');"
                "m=json.loads((p/'manifest.json').read_text());"
                "assert m['version']==3;"
                "assert any('CREATE TABLE public.m1_fixture' in f.read_text() for f in p.rglob('*.sql'));"
                "assert any('dc=dev,dc=invalid' in f.read_text() for f in p.rglob('*.ldif'));"
                "repo=next(p.rglob('m1.git'));objects=[f for f in (repo/'objects').glob('*/*') if f.is_file()];"
                "assert len(objects)>=3;"
                "assert all(hashlib.sha1(zlib.decompress(f.read_bytes())).hexdigest()==f.parent.name+f.name for f in objects);"
                "assert (repo/'external-link').is_symlink() and os.readlink(repo/'external-link')=='/etc/passwd';"
                "i=next(p.rglob('identity.ini')).read_text();assert '[security]' in i and '[database]' not in i;"
                "assert list(p.rglob('ssh-ed25519-private'));"
                "assert not list(p.rglob('*.tar'));print('real-files/native-SQL/native-LDIF/generated-identity: PASS')"
            )
            report["checks"]["restored_contents"] = podman("exec", kopia, "python3", "-c", check).stdout.strip()
            bad = prefix + "-missing"
            start(bad, "docker.io/library/postgres:18.6-alpine3.24", [
                "--label", "sophon.backup.version=3", "--label", "sophon.backup.group=missing",
                "--label", "sophon.backup.stop=true", "--label", "sophon.backup.paths.0.name=state",
                "--label", "sophon.backup.paths.0.volume=not-mounted", "--label", "sophon.backup.paths.0.path=state"],
                  ["sleep", "3600"])
            partial = podman("exec", kopia, "python3", "/app/backup.py", "run", check=False)
            assert partial.returncode == 1, partial.stderr
            report["checks"]["partial_run"] = json.loads(partial.stdout)
            assert report["checks"]["partial_run"]["status"] == "partial"
            assert report["checks"]["partial_run"]["snapshot"]["id"] != snapshot
            assert report["checks"]["partial_run"]["snapshot"]["source"] == latest["snapshot"]["source"]
            assert podman("inspect", application, "--format", "{{.State.Running}}").stdout.strip() == "true"
            report["checks"]["writers_resumed"] = True
            podman("run", "--rm", "--network", "none", "-v", slow + ":/data",
                   "docker.io/library/python:3.14.10-slim-trixie", "python3", "-c",
                   "from pathlib import Path;Path('/data/state').write_text('crash-recovery-fixture')")
            slow_writer, slow_exporter = prefix + "-slow-writer", prefix + "-slow-exporter"
            start(slow_writer, "docker.io/library/python:3.14.10-slim-trixie", [
                "-v", slow + ":/data", "--label", "sophon.backup.version=3",
                "--label", "sophon.backup.group=slow", "--label", "sophon.backup.stop=true",
                "--label", "sophon.backup.paths.0.volume=" + slow, "--label", "sophon.backup.paths.0.path=state",
                "--label", "sophon.backup.paths.0.name=state"], [
                    "python3", "-c", "import signal,sys,time;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));time.sleep(3600)"])
            start(slow_exporter, "docker.io/library/postgres:18.6-alpine3.24", [
                "--label", "sophon.backup.version=3", "--label", "sophon.backup.group=slow",
                "--label", "sophon.backup.stop=false", "--label", "sophon.backup.dumps.0.name=slow.txt",
                "--label", 'sophon.backup.dumps.0.argv=["sh","-ec","sleep 60; printf native-export"]'],
                  ["sleep", "3600"])
            exporter_id = podman("inspect", slow_exporter, "--format", "{{.Id}}").stdout.strip()
            manual = subprocess.Popen(
                ["podman", "--cgroup-manager=cgroupfs", "exec", kopia, "python3", "-c",
                 "import os,sys;print(os.getpid(),flush=True);os.execv(sys.executable,[sys.executable,'/app/backup.py','run'])"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
            collector_pid = int(manual.stdout.readline().strip())
            for _ in range(150):
                result = podman("exec", kopia, "cat", "/source/journal.json", check=False)
                journal = json.loads(result.stdout) if result.returncode == 0 else {}
                if any(item["member"] == exporter_id for item in journal.get("execs", [])):
                    break
                if manual.poll() is not None:
                    raise RuntimeError("Crash fixture exited before its pending native export")
                time.sleep(.1)
            else:
                raise RuntimeError("Crash fixture did not reach a pending native export")
            assert podman("inspect", slow_writer, "--format", "{{.State.Running}}").stdout.strip() == "false"
            podman("exec", kopia, "python3", "-c", f"import os,signal;os.kill({collector_pid},signal.SIGKILL)")
            manual.communicate(timeout=20)
            assert manual.returncode == 137, manual.returncode
            podman("restart", kopia)
            wait(["exec", kopia, "python3", "/app/runtime.py", "health"])
            assert podman("inspect", slow_writer, "--format", "{{.State.Running}}").stdout.strip() == "true"
            assert podman("exec", kopia, "test", "!", "-e", "/source/journal.json").returncode == 0
            podman("exec", kopia, "test", "!", "-e", "/source/runs/" + journal["run"])
            execution = next(item["id"] for item in journal["execs"] if item["member"] == exporter_id)
            cancelled = podman("exec", kopia, "python3", "-c",
                              "import backup,os;print(backup.engine(backup.engine_api(os.environ['ENGINE_URL']),"
                              f"'GET','/exec/{execution}/json')['Running'])").stdout.strip()
            assert cancelled == "False", cancelled
            report["checks"]["interrupted_stop_recovery"] = {
                "collector_exit": manual.returncode, "writer_resumed": True, "journal_removed": True,
                "export_cancelled": True, "interrupted_staging_removed": True}
        finally:
            if started:
                cleanup = command(compose_command + ["down", "-v"], check=False)
                report["cleanup"].append({"stack": cleanup.returncode})
            for container in reversed(containers):
                result = podman("rm", "-f", "-v", container, check=False)
                report["cleanup"].append({container: result.returncode})
            if api:
                api.terminate()
                api.wait(timeout=10)
            for volume in volumes:
                result = podman("volume", "rm", volume, check=False)
                report["cleanup"].append({volume: result.returncode})
            result = podman("network", "rm", front, check=False)
            report["cleanup"].append({front: result.returncode})
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
            if any(code != 0 for record in report["cleanup"] for code in record.values()):
                raise RuntimeError("Dev fixture cleanup failed; inspect recorded resource-specific results")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-only", action="store_true", required=True)
    parser.add_argument("--rendered", required=True)
    parser.add_argument("--sftp-root", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())
