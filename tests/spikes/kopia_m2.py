#!/usr/bin/env python3
"""Rebuild disposable dev applications and restore latest v3 state using Podman alone."""
import argparse
import base64
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import yaml


def run(args):
    root = Path(args.rendered).resolve()
    prefix = "sophon-m2-" + uuid.uuid4().hex[:10]
    environment = dict(os.environ)
    environment.update(KOPIA_PASSWORD=secrets.token_urlsafe(32), KOPIA_SERVER_PASSWORD=secrets.token_urlsafe(24),
                       KOPIA_SERVER_USERNAME="admin", KOPIA_SFTP_HOST="host.containers.internal",
                       KOPIA_SFTP_PORT="22", KOPIA_SFTP_USERNAME=os.environ["USER"],
                       KOPIA_CLIENT_USERNAME="backup", KOPIA_CLIENT_HOSTNAME="m2-dev",
                       KOPIA_BACKUPS_ENABLED="false", KOPIA_SNAPSHOT_INTERVAL="6h")
    report = {"checks": {}, "cleanup": [], "profile": "dev.invalid; no Portainer, tunnels, runners or ACME"}
    containers, volumes, states = [], [], []
    api, proxy, thread, stack = None, None, None, None
    front = prefix + "-front"
    image = "localhost/sophon-kopia:m2-dev"
    fault = {"target": None, "injected": False, "import_armed": False, "import_injected": False}
    native_execs = {}
    definitions = {role: yaml.safe_load((root / (role + ".yml")).read_text())
                   for role in ("traefik", "openldap", "keycloak", "gitea", "kopia")}
    sensitive = [value.split("=", 1)[1] for role in ("gitea", "keycloak")
                 for service in definitions[role]["services"].values()
                 for value in service.get("environment", []) if isinstance(value, str) and
                 "=" in value and any(part in value.split("=", 1)[0] for part in ("PASSWORD", "PASSWD", "SECRET"))]

    def command(argv, *, check=True, input=None, timeout=900):
        result = subprocess.run(argv, env=environment, input=input, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            detail = result.stdout[-2000:] + "\n" + result.stderr[-3000:]
            for value in sensitive + [environment["KOPIA_PASSWORD"], environment["KOPIA_SERVER_PASSWORD"]]:
                if value:
                    detail = detail.replace(value, "[redacted]")
            raise RuntimeError(f"Dev command failed ({result.returncode}): {argv[:4]}\n{detail}")
        return result

    def podman(*argv, **kwargs):
        return command(["podman", "--cgroup-manager=cgroupfs", *argv], **kwargs)

    def wait(argv):
        for _ in range(120):
            result = podman(*argv, check=False)
            if not result.returncode:
                return result.stdout
            time.sleep(1)
        raise RuntimeError("Dev service readiness failed: " + argv[1])

    def volume(role, alias):
        if role == "openldap" and alias == "acme_data":
            role, alias = "traefik", "traefik_data"
        return prefix + "-" + role + "-" + alias

    def spec(role, name):
        service = copy.deepcopy(definitions[role]["services"][name])
        values = service.get("environment", {})
        if isinstance(values, list):
            values = dict(entry.split("=", 1) for entry in values)
        service["environment"] = {key: str(value).replace("$$", "$") for key, value in values.items()}
        values = service.get("labels", {})
        if isinstance(values, list):
            values = dict(entry.split("=", 1) for entry in values)
        service["labels"] = {}
        for key, value in values.items():
            if key.startswith("sophon.backup."):
                for alias in definitions[role].get("volumes", {}):
                    value = str(value).replace(role + "_" + alias, volume(role, alias))
                service["labels"][key] = str(value).replace("$$", "$")
        return service

    def start(role, name, extra=(), overrides=None, argv=None):
        service = spec(role, name)
        values = service["environment"]
        values.update(overrides or {})
        flags = ["--pull=never", "--security-opt=label=disable"]
        if name != "gitea-init":
            flags += ["--network-alias", name]
        for value in service.get("volumes", []):
            parts = value.split(":")
            if parts[0].startswith("/"):
                continue
            flags += ["-v", ":".join([volume(role, parts[0]), *parts[1:]])]
        for key, value in values.items():
            environment[key] = value
            flags += ["-e", key]
        for key, value in service["labels"].items():
            flags += ["--label", key + "=" + value]
        if service.get("user"):
            flags += ["--user", str(service["user"])]
        if service.get("entrypoint"):
            flags += ["--entrypoint", json.dumps(service["entrypoint"])]
        identifier = prefix + "-" + name
        if identifier not in containers:
            containers.append(identifier)
        podman("run", "-d", "--name", identifier, "--network", front, *flags, *extra, service["image"],
               *(argv if argv is not None else [value.replace("$$", "$") for value in service.get("command", [])]))
        return identifier

    def request(url, data=None, password=None):
        headers = {}
        if password:
            headers["Authorization"] = "Basic " + base64.b64encode(("drill-admin:" + password).encode()).decode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        with urllib.request.urlopen(urllib.request.Request(
                url, data=None if data is None else json.dumps(data).encode(), headers=headers), timeout=30) as response:
            return json.load(response)

    def endpoint(container, port):
        address = podman("port", container, str(port) + "/tcp").stdout.strip()
        if not address.startswith("127.0.0.1:"):
            raise RuntimeError("Drill HTTP endpoints must bind loopback only")
        return "http://" + address

    def files(volume_name, script):
        return podman("run", "--rm", "--pull=never", "--network", "none", "-v", volume_name + ":/data",
                      "--entrypoint", "python3", image, "-c", script).stdout.strip()

    def certificates(directory):
        command(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                 "-nodes", "-days", "2", "-subj", "/CN=dev.invalid", "-addext",
                 "subjectAltName=DNS:dev.invalid,DNS:*.dev.invalid", "-keyout", str(directory / "dev.invalid.key"),
                 "-out", str(directory / "dev.invalid.crt")])
        state = {"dev-fixture": {"Certificates": [{"domain": {"main": "dev.invalid", "sans": ["*.dev.invalid"]},
                  "certificate": base64.b64encode((directory / "dev.invalid.crt").read_bytes()).decode(),
                  "key": base64.b64encode((directory / "dev.invalid.key").read_bytes()).decode()}]}}
        (directory / "acme.json").write_text(json.dumps(state))
        for vol, script in (
            (volume("traefik", "traefik_data"), "shutil.copy('/fixtures/acme.json','/data/acme.json');os.chmod('/data/acme.json',0o600)"),
            (volume("openldap", "ldap_certs"), "p=Path('/data');(p/'certs').mkdir();(p/'private').mkdir();"
             "shutil.copy('/fixtures/dev.invalid.crt',p/'certs/dev.invalid.crt');"
             "shutil.copy('/fixtures/dev.invalid.key',p/'private/dev.invalid.key');os.chmod(p/'private/dev.invalid.key',0o644)"),
        ):
            podman("run", "--rm", "--pull=never", "--network", "none", "-v", vol + ":/data",
                   "-v", str(directory) + ":/fixtures:ro", "--entrypoint", "python3", image,
                   "-c", "import os,shutil;from pathlib import Path;" + script)

    def keycloak_login(keycloak):
        values = spec("keycloak", "keycloak")["environment"]
        wait(["exec", keycloak, "/opt/keycloak/bin/kcadm.sh", "config", "credentials",
              "--server", "http://localhost:8080", "--realm", "master",
              "--user", values["KC_BOOTSTRAP_ADMIN_USERNAME"], "--password", values["KC_BOOTSTRAP_ADMIN_PASSWORD"]])

    def rebuild(directory):
        for role in ("traefik", "openldap", "keycloak", "gitea"):
            for alias in definitions[role].get("volumes", {}):
                name = volume(role, alias)
                if name not in volumes:
                    podman("volume", "create", name)
                    volumes.append(name)
        certificates(directory)
        # Explicit dev-only static configuration: no Docker provider, resolver or issuance.
        start("traefik", "traefik", argv=["--entrypoints.web.address=:8088", "--log.level=ERROR"])
        ldap = start("openldap", "ldap", ["--network-alias", "ldap.dev.invalid"])
        wait(["exec", ldap, "ldapsearch", "-Q", "-Y", "EXTERNAL", "-H", "ldapi:///",
              "-b", "", "-s", "base", "namingContexts"])
        for role in ("keycloak", "gitea"):
            database = start(role, role + "-db")
            values = spec(role, role + "-db")["environment"]
            wait(["exec", database, "pg_isready", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"]])
        keycloak = start("keycloak", "keycloak", overrides={"JAVA_OPTS_KC_HEAP": "-Xms128m -Xmx512m"},
                         argv=["start", "--hostname=http://auth.dev.invalid:8080", "--http-enabled=true"])
        keycloak_login(keycloak)
        gitea = start("gitea", "gitea", ["-p", "127.0.0.1::3000"], overrides={
            "GITEA__actions__ENABLED": "false", "GITEA__server__OFFLINE_MODE": "true"})
        wait(["exec", gitea, "wget", "-qO-", "http://127.0.0.1:3000/api/v1/version"])
        initializer = start("gitea", "gitea-init")
        if podman("wait", initializer).stdout.strip() != "0":
            raise RuntimeError("Repository-configured Gitea initializer failed")
        return ldap, keycloak, gitea

    def realm_export(keycloak):
        podman("stop", "-t", "120", keycloak)
        export = prefix + "-export"
        destination = prefix + "-export-data"
        podman("volume", "create", destination)
        volumes.append(destination)
        flags = []
        for key, value in spec("keycloak", "keycloak")["environment"].items():
            if key.startswith("KC_DB"):
                environment[key] = value
                flags += ["-e", key]
        podman("run", "--rm", "--pull=never", "--name", export, "--network", front, *flags,
               "-e", "JAVA_OPTS_KC_HEAP=-Xms128m -Xmx512m", "-v", destination + ":/export",
               spec("keycloak", "keycloak")["image"], "export", "--dir=/export",
               "--realm=m2-drill", "--users=realm_file")
        value = files(destination, "import json;from pathlib import Path;"
                      "print(json.dumps(json.loads(Path('/data/m2-drill-realm.json').read_text()),sort_keys=True))")
        podman("volume", "rm", destination)
        volumes.remove(destination)
        podman("start", keycloak)
        return hashlib.sha256(value.encode()).hexdigest()

    def directory_state(ldap):
        raw = podman("exec", ldap, "slapcat", "-n", "1", "-s", "dc=dev,dc=invalid").stdout
        paragraphs = []
        for paragraph in raw.strip().split("\n\n"):
            lines = paragraph.replace("\n ", "").splitlines()
            paragraphs.append("\n".join(sorted(line for line in lines if not line.startswith("contextCSN:"))))
        return hashlib.sha256("\n\n".join(sorted(paragraphs)).encode()).hexdigest()

    def gitea_identity():
        return files(volume("gitea", "gitea_data"),
                     "import configparser,hashlib,json;"
                     "c=configparser.ConfigParser(interpolation=None,allow_unnamed_section=True);"
                     "c.read('/data/gitea/conf/app.ini');"
                     "v={s+'.'+k:c.get(s,k,fallback=None) for s,k in "
                     "[('security','SECRET_KEY'),('security','INTERNAL_TOKEN'),"
                     "('server','LFS_JWT_SECRET'),('oauth2','JWT_SECRET')]};"
                     "assert any(v.values());print(hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest())")

    if podman("info", "--format", "{{.Host.Security.Rootless}}").stdout.strip() != "true":
        raise RuntimeError("Drill requires rootless Podman")
    if "portainer" in podman("ps", "-a", "--format", "{{.Names}}").stdout.lower():
        raise RuntimeError("Drill requires a dedicated dev engine without Portainer")
    socket_path = Path(os.environ["XDG_RUNTIME_DIR"]) / "podman/podman.sock"
    if socket_path.exists():
        raise RuntimeError("Refusing to replace an existing API socket")
    with tempfile.TemporaryDirectory(prefix=prefix + "-", dir=args.sftp_root) as temporary:
        directory = Path(temporary)
        configuration = directory / "containers.conf"
        configuration.write_text('[engine]\ncgroup_manager="cgroupfs"\n')
        environment["CONTAINERS_CONF"] = str(configuration)
        direct_socket = directory / "engine.sock"
        socket_path.parent.mkdir(exist_ok=True)

        class Forwarder(BaseHTTPRequestHandler):
            def log_message(self, *values):
                pass

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                native_command = None
                if self.command == "POST" and self.path.endswith("/exec"):
                    execution_body = json.loads(body)
                    cmd = execution_body.get("Cmd", [])
                    marker = 'psql -X -q -U "$temp" -d "$database" -v ON_ERROR_STOP=1 -f "$dump"'
                    if (fault["import_armed"] and not fault["import_injected"] and len(cmd) > 2
                            and cmd[0] == "sh" and marker in cmd[2]):
                        cmd[2] = cmd[2].replace(marker, (
                            """awk '/^CREATE DATABASE / {exit} {print}' "$dump" > /tmp/partial.sql\n"""
                            'psql -X -q -U "$temp" -d "$database" -v ON_ERROR_STOP=1 -f /tmp/partial.sql\n'
                            'exit 97\n' + marker))
                        body = json.dumps(execution_body).encode()
                        fault["import_injected"] = True
                    if cmd and cmd[0] in ("slapcat", "slapadd", "slaptest", "stat"):
                        native_command = cmd[0]
                if (self.command == "POST" and self.path == f"/v1.41/containers/{fault['target']}/start"
                        and not fault["injected"]):
                    fault["injected"] = True
                    self.send_error(500, "Deliberate dev-only post-import restart failure")
                    return
                connection = http.client.HTTPConnection("localhost", timeout=3600)
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.connect(str(direct_socket))
                try:
                    connection.request(self.command, self.path, body, {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.end_headers()
                    if native_command:
                        data = response.read()
                        native_execs[json.loads(data)["Id"]] = native_command
                        self.wfile.write(data)
                        return
                    execution = self.path.split("/")[-2]
                    if self.command == "POST" and execution in native_execs:
                        data = response.read()
                        offset, stderr = 0, bytearray()
                        while offset < len(data):
                            channel, length = struct.unpack(">BxxxI", data[offset:offset + 8])
                            offset += 8
                            if channel == 2:
                                stderr.extend(data[offset:offset + length])
                            offset += length
                        if stderr:
                            report.setdefault("native_diagnostics", []).append({
                                "tool": native_execs[execution], "stderr": stderr.decode(errors="replace")[-2000:]})
                        self.wfile.write(data)
                        return
                    while chunk := response.read(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    connection.close()

            do_GET = do_POST = do_DELETE = forward

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        try:
            api = subprocess.Popen(["podman", "--cgroup-manager=cgroupfs", "system", "service",
                                    "--time=0", "unix:" + str(direct_socket)], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if api.poll() is not None:
                    raise RuntimeError("Dev engine API exited")
                if direct_socket.exists():
                    break
                time.sleep(.1)
            proxy = Server(str(socket_path), Forwarder)
            socket_path.chmod(0o600)
            thread = threading.Thread(target=proxy.serve_forever)
            thread.start()
            podman("network", "create", front)
            ldap, keycloak, gitea = rebuild(directory)
            values = spec("gitea", "gitea-init")["command"][0]
            # The initializer's password is a generated dev fixture, never a site input.
            import shlex
            tokens = shlex.split(values)
            admin_password = tokens[tokens.index("--password") + 1]
            sensitive.append(admin_password)
            url = endpoint(gitea, 3000)
            request(url + "/api/v1/user/repos", {"name": "state", "private": True}, admin_password)
            content = base64.b64encode(b"irreproducible M2 repository content\n").decode()
            commit = request(url + "/api/v1/repos/drill-admin/state/contents/state.txt",
                             {"content": content, "message": "irreproducible state"}, admin_password)["commit"]["sha"]
            ssh = podman("exec", gitea, "sha256sum", "/data/ssh/ssh_host_ed25519_key").stdout.split()[0]
            podman("exec", "-i", ldap, "sh", "-ec",
                   'export LDAPTLS_CACERT="$LDAP_TLS_CA_FILE"; '
                   'exec ldapadd -x -H ldaps://ldap.dev.invalid -D "$LDAP_INIT_ROOT_USER_DN" -w "$LDAP_INIT_ROOT_USER_PW"', input=(
                "dn: uid=drill-user,ou=people,dc=dev,dc=invalid\nobjectClass: inetOrgPerson\n"
                "uid: drill-user\ncn: Restorable User\nsn: User\nuserPassword: dev-drill-user-password\n\n"))
            podman("exec", "-i", keycloak, "/opt/keycloak/bin/kcadm.sh", "create", "realms", "-f", "-", input=json.dumps({
                "realm": "m2-drill", "enabled": True,
                "users": [{"username": "irreproducible-user", "enabled": True,
                           "credentials": [{"type": "password", "value": "dev-drill-user-password", "temporary": False}]}],
                "clients": [{"clientId": "irreproducible-client", "secret": secrets.token_hex(24), "enabled": True}]}))
            before = {"keycloak": realm_export(keycloak), "ldap": directory_state(ldap),
                      "certificate": files(volume("traefik", "traefik_data"),
                                           "import hashlib;from pathlib import Path;print(hashlib.sha256(Path('/data/acme.json').read_bytes()).hexdigest())"),
                      "gitea_commit": commit, "gitea_ssh_identity": ssh, "gitea_ini_identity": gitea_identity()}
            environment["KOPIA_SFTP_PRIVATE_KEY_B64"] = base64.b64encode(Path(args.key).read_bytes()).decode()
            pins = "\n".join("host.containers.internal,127.0.0.1 " + line.split(" ", 1)[1]
                             for line in Path(args.known_hosts).read_text().splitlines() if line and not line.startswith("#"))
            environment["KOPIA_SFTP_KNOWN_HOSTS_B64"] = base64.b64encode(pins.encode()).decode()
            repository = directory / "repository"
            repository.mkdir()
            environment["KOPIA_SFTP_PATH"] = str(repository)
            podman("run", "--rm", "--pull=never", "--network", "slirp4netns:allow_host_loopback=true",
                   "-e", "KOPIA_PASSWORD", "-e", "KOPIA_CONFIG_PATH=/tmp/repo.config",
                   "-e", "KOPIA_CACHE_DIRECTORY=/tmp/cache", "-e", "KOPIA_LOG_DIR=/tmp/logs",
                   "-v", args.key + ":/keys/key:ro", "-v", args.known_hosts + ":/keys/known_hosts:ro",
                   "docker.io/kopia/kopia:0.23.1", "repository", "create", "sftp",
                   "--host=host.containers.internal", "--username=" + os.environ["USER"], "--path=" + str(repository),
                   "--keyfile=/keys/key", "--known-hosts=/keys/known_hosts", "--no-persist-credentials", "--no-use-keyring")
            project = directory / "project"
            target = project / "roles/kopia/files/engine-proxy.cfg"
            target.parent.mkdir(parents=True)
            shutil.copy(root / "engine-proxy.cfg", target)
            environment["SOPHON_PROJECT_PATH"] = str(project)
            composed = yaml.safe_load((root / "kopia.yml").read_text()
                                      .replace("SOPHON_M2_DATA", volume("gitea", "gitea_data"))
                                      .replace("SOPHON_M2_ACME", volume("traefik", "traefik_data")))
            composed["networks"]["proxy"]["name"] = front
            for name in ("kopia", "engine-proxy"):
                composed["services"][name]["container_name"] = prefix + "-" + name
            compose_path = directory / "compose.yml"
            compose_path.write_text(yaml.safe_dump(composed))
            stack = ["podman-compose", "-p", prefix, "-f", str(compose_path)]
            command(stack + ["up", "-d"])
            kopia = prefix + "-kopia"
            wait(["exec", kopia, "python3", "/app/runtime.py", "health"])
            result = json.loads(podman("exec", kopia, "python3", "/app/backup.py", "run").stdout)
            assert result["status"] == "complete", result
            snapshot = result["snapshot"]["id"]
            report["checks"]["snapshot"] = snapshot
            podman("stop", "-t", "240", kopia, prefix + "-engine-proxy")
            podman("rm", kopia)
            podman("rm", prefix + "-engine-proxy")
            for name in reversed(containers):
                podman("rm", "-f", name)
            for name in list(volumes):
                podman("volume", "rm", name)
                volumes.remove(name)
            ldap, keycloak, gitea = rebuild(directory)
            assert files(volume("traefik", "traefik_data"),
                         "import hashlib;from pathlib import Path;print(hashlib.sha256(Path('/data/acme.json').read_bytes()).hexdigest())") != before["certificate"]
            try:
                request(endpoint(gitea, 3000) + "/api/v1/repos/drill-admin/state", password=admin_password)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise RuntimeError("Fresh rebuild unexpectedly retained the source repository")
            assert directory_state(ldap) != before["ldap"]
            assert gitea_identity() != before["gitea_ini_identity"]
            report["checks"]["fresh_application_volumes"] = True
            for key, value in list(environment.items()):
                if key.startswith("KOPIA_"):
                    environment["SOPHON_" + key] = value
            environment.update(SOPHON_KOPIA_SFTP_HOST="127.0.0.1", SOPHON_KOPIA_RESTORE_IMAGE=image,
                               SOPHON_RESTORE_SOURCE_VOLUME=prefix + "_source-v3")
            for group in ("traefik", "ldap", "keycloak", "gitea"):
                state = prefix + "-restore-" + group
                states.append(state)
                base = ["bash", args.restore, "--state-volume", state]
                planned = json.loads(command(base + ["plan", "--group", group]).stdout)
                assert planned["snapshot"] == snapshot and planned["phase"] == "planned"
                if group == "gitea":
                    fault["target"] = podman("inspect", gitea, "--format", "{{.Id}}").stdout.strip()
                    fault["import_armed"] = True
                    failed = command(base + ["apply", "--group", group, "--snapshot", snapshot, "--approve"], check=False)
                    assert failed.returncode == 1 and fault["import_injected"] and not fault["injected"], failed.stderr
                    for member in planned["plan"]["members"]:
                        assert podman("inspect", member["id"], "--format", "{{.State.Running}}").stdout.strip() == "false"
                    assert json.loads(command(base + ["status"]).stdout)["phase"] == "failed"
                    report["checks"]["partial_native_import_stopped_entire_group"] = True
                    held = podman("run", "--rm", "--pull=never", "--network", "none",
                                  "-v", environment["SOPHON_RESTORE_SOURCE_VOLUME"] + ":/source",
                                  "--entrypoint", "python3", image, "-c",
                                  "import backup;backup.prepare({}, {}, '/source', 'dev-hold-check')", check=False)
                    assert held.returncode != 0 and "Operator restoration is pending" in held.stderr
                    report["checks"]["backup_blocked_during_failed_restore"] = True
                    failed = command(base + ["apply", "--group", group, "--snapshot", snapshot, "--approve"], check=False)
                    assert failed.returncode == 1 and fault["injected"], failed.stderr
                    for member in planned["plan"]["members"]:
                        assert podman("inspect", member["id"], "--format", "{{.State.Running}}").stdout.strip() == "false"
                    assert json.loads(command(base + ["status"]).stdout)["phase"] == "failed"
                    report["checks"]["post_import_failure_stopped_entire_group"] = True
                applied = json.loads(command(base + ["apply", "--group", group, "--snapshot", snapshot, "--approve"]).stdout)
                assert applied["phase"] == "complete"
                command(base + ["cleanup", "--approve"])
                podman("volume", "rm", state)
                states.remove(state)
            wait(["exec", gitea, "wget", "-qO-", "http://127.0.0.1:3000/api/v1/version"])
            restored_commit = request(endpoint(gitea, 3000) + "/api/v1/repos/drill-admin/state/commits",
                                      password=admin_password)[0]["sha"]
            after = {"keycloak": realm_export(keycloak), "ldap": directory_state(ldap),
                     "certificate": files(volume("traefik", "traefik_data"),
                                          "import hashlib;from pathlib import Path;print(hashlib.sha256(Path('/data/acme.json').read_bytes()).hexdigest())"),
                     "gitea_commit": restored_commit, "gitea_ini_identity": gitea_identity(),
                     "gitea_ssh_identity": podman("exec", gitea, "sha256sum", "/data/ssh/ssh_host_ed25519_key").stdout.split()[0]}
            report["checks"]["source"] = before
            report["checks"]["restored"] = after
            assert before == after, "Source/restored state comparison failed"
            keycloak_login(keycloak)
            wait(["exec", ldap, "sh", "-ec",
                  'export LDAPTLS_CACERT="$LDAP_TLS_CA_FILE"; exec ldapsearch -x -H ldaps://ldap.dev.invalid '
                  '-D uid=drill-user,ou=people,dc=dev,dc=invalid -w dev-drill-user-password '
                  '-b uid=drill-user,ou=people,dc=dev,dc=invalid -s base dn'])
            report["checks"]["keycloak_login_and_ldap_password_bind"] = True
            report["checks"]["all_comparisons_equal"] = True
            report["checks"]["backup_stack_stopped_during_restore"] = True
            command(stack + ["up", "-d"])
            wait(["exec", kopia, "python3", "/app/runtime.py", "health"])
            resumed = json.loads(podman("exec", kopia, "python3", "/app/backup.py", "run").stdout)
            assert resumed["status"] == "complete"
            report["checks"]["normal_backup_resumed"] = True
        finally:
            for state in states:
                result = command(["bash", args.restore, "--state-volume", state, "cleanup", "--approve"], check=False)
                report["cleanup"].append({"restore_helpers": result.returncode})
                result = podman("volume", "rm", state, check=False)
                report["cleanup"].append({state: result.returncode})
            if stack:
                result = command(stack + ["down", "-v"], check=False)
                report["cleanup"].append({"backup_stack": result.returncode})
            for name in reversed(containers):
                if podman("container", "exists", name, check=False).returncode == 0:
                    result = podman("rm", "-f", name, check=False)
                    report["cleanup"].append({name: result.returncode})
            for name in volumes:
                result = podman("volume", "rm", name, check=False)
                report["cleanup"].append({name: result.returncode})
            if proxy:
                proxy.shutdown()
                proxy.server_close()
                thread.join()
                socket_path.unlink()
            if api:
                api.terminate()
                api.wait(timeout=10)
            if podman("network", "exists", front, check=False).returncode == 0:
                result = podman("network", "rm", front, check=False)
                report["cleanup"].append({front: result.returncode})
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
            for entry in report.get("native_diagnostics", []):
                print(json.dumps(entry))
            if any(code != 0 for row in report["cleanup"] for code in row.values()):
                raise RuntimeError("Drill cleanup failed; inspect resource-specific report")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-only", action="store_true", required=True)
    parser.add_argument("--rendered", required=True)
    parser.add_argument("--sftp-root", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--restore", required=True)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())
