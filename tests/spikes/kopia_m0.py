#!/usr/bin/env python3
"""M0 only: disposable rootless-Podman probes; never runs Configure or real workloads."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import tempfile
import time
import uuid

PYTHON = "docker.io/library/python:3.14.10-slim-trixie"
PROXY = "docker.io/library/haproxy:3.2.6-alpine"
KOPIA = "docker.io/kopia/kopia:0.23.1"
SIZE = 64 * 1024 * 1024


def command(argv, *, check=True, **kwargs):
    started = time.monotonic()
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180, **kwargs)
    record = {"command": argv, "exit": result.returncode, "seconds": round(time.monotonic() - started, 6),
              "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    if check and result.returncode:
        raise RuntimeError(json.dumps(record))
    return record


def podman(*args, **kwargs):
    return command(["podman", "--cgroup-manager=cgroupfs", *args], **kwargs)


def call(port, method, path, body=None, *, stream=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    connection.request(method, "/v1.41" + path, body=None if body is None else json.dumps(body),
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    if stream:
        return connection, response
    data = response.read()
    connection.close()
    return response.status, json.loads(data) if data and response.status != 403 else None


def exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise RuntimeError("Truncated Docker exec frame")
        data.extend(chunk)
    return bytes(data)


def run(args):
    info = json.loads(podman("info", "--format", "json")["stdout"])
    if not info["host"]["security"]["rootless"]:
        raise RuntimeError("This spike requires rootless Podman on a disposable dev host")
    report = {"podman": info["version"], "kernel": info["host"]["kernel"],
              "filesystem": info["store"]["graphStatus"], "images": {}, "copies": [], "exec": {}, "kopia": {},
              "cleanup": []}
    for image in (PYTHON, PROXY, KOPIA):
        podman("image", "exists", image)
        report["images"][image] = podman("image", "inspect", image, "--format", "{{json .RepoDigests}}")["stdout"]
    prefix = "sophon-m0-" + uuid.uuid4().hex[:12]
    containers, volumes = [], []
    api = None
    with tempfile.TemporaryDirectory(prefix=prefix + "-", dir=args.sftp_root) as temporary:
        temporary = Path(temporary)
        try:
            source = prefix + "-source"
            podman("volume", "create", source)
            volumes.append(source)
            initialize = (
                "import os,hashlib,json; from pathlib import Path; "
                "p=Path('/data/private'); p.mkdir(); f=p/'state'; "
                f"data=os.urandom({SIZE}); f.write_bytes(data); "
                "os.link(f,p/'hardlink'); os.symlink('state',p/'symlink'); "
                "os.setxattr(f,'user.sophon-m0',b'preserved'); "
                "os.chown(f,1001,1002); os.chmod(f,0o400); os.chown(p,999,998); os.chmod(p,0o700); "
                "print(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}))"
            )
            fixture = json.loads(podman("run", "--rm", "--network", "none", "-v", source + ":/data",
                                       PYTHON, "python3", "-c", initialize)["stdout"])
            report["fixture"] = fixture
            report["uid_map"] = podman("unshare", "cat", "/proc/self/uid_map")["stdout"]
            writer = prefix + "-writer"
            podman("run", "-d", "--name", writer, "--network", "none", "-v", source + ":/data",
                   PYTHON, "python3", "-c",
                   "import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); time.sleep(3600)")
            containers.append(writer)
            capabilities = (
                (), ("DAC_READ_SEARCH",), ("DAC_READ_SEARCH", "CHOWN"),
                ("DAC_READ_SEARCH", "CHOWN", "FOWNER"),
                ("DAC_OVERRIDE", "CHOWN"), ("DAC_OVERRIDE", "FOWNER"),
                ("DAC_OVERRIDE", "CHOWN", "FOWNER"),
            )
            copy_script = r'''
import errno,hashlib,json,os,subprocess,time
from pathlib import Path
result={}
try:
    Path('/input/forbidden-write').write_text('must fail')
except OSError as error:
    result['source_write_errno']=error.errno
else:
    raise SystemExit('Source mount was writable')
result['filesystem']=subprocess.check_output(['stat','-f','-c','%T','/input','/staging'],text=True).splitlines()
start=time.monotonic()
copy=subprocess.run(['cp','--archive','--reflink=always','/input/.','/staging/'],capture_output=True,text=True)
result.update(seconds=time.monotonic()-start,copy_exit=copy.returncode,copy_error=copy.stderr.strip())
result['cap_eff']=next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
if copy.returncode == 0:
    file=Path('/staging/private/state'); attributes=file.stat()
    result.update(sha256=hashlib.sha256(file.read_bytes()).hexdigest(),uid=attributes.st_uid,gid=attributes.st_gid,
                  mode=oct(attributes.st_mode & 0o777),
                  hardlink=file.stat().st_ino==Path('/staging/private/hardlink').stat().st_ino,
                  symlink=os.readlink('/staging/private/symlink'))
    try:
        result['xattr']=os.getxattr(file,'user.sophon-m0').decode()
    except OSError as error:
        result['xattr_error']=str(error)
print(json.dumps(result))
'''
            for index, caps in enumerate(capabilities):
                stage = prefix + "-stage-" + str(index)
                podman("volume", "create", stage)
                volumes.append(stage)
                flags = [arg for cap in caps for arg in ("--cap-add", cap)]
                result = podman("run", "--rm", "--read-only", "--network", "none", "--user", "0", "--cap-drop", "ALL",
                                *flags, "--security-opt", "label=disable", "--security-opt", "no-new-privileges",
                                "-v", source + ":/input:ro", "-v", stage + ":/staging",
                                PYTHON, "python3", "-c", copy_script, check=False)
                result["capabilities"] = caps
                if result["exit"] == 0:
                    result["measurement"] = json.loads(result.pop("stdout"))
                    measured = result["measurement"]
                    result["passes"] = (
                        measured["copy_exit"] == 0 and measured["source_write_errno"] == 30
                        and measured.get("sha256") == fixture["sha256"]
                        and measured.get("uid") == 1001 and measured.get("gid") == 1002
                        and measured.get("mode") == "0o400" and measured.get("xattr") == "preserved"
                        and measured.get("hardlink") is True and measured.get("symlink") == "state"
                    )
                else:
                    result["passes"] = False
                report["copies"].append(result)
            if not any(result["passes"] for result in report["copies"]):
                raise RuntimeError("No tested capability set preserved the reflink clone and its metadata")
            socket_path = temporary / "podman.sock"
            api = subprocess.Popen(["podman", "--cgroup-manager=cgroupfs", "system", "service", "--time=0", "unix:" + str(socket_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 15
            while not socket_path.exists():
                if api.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Dev Podman compatibility service did not start")
                time.sleep(.1)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            proxy = prefix + "-proxy"
            containers.append(proxy)
            podman("run", "-d", "--name", proxy, "--read-only", "--cap-drop", "ALL", "--user", "0",
                   "--security-opt", "label=disable", "--security-opt", "no-new-privileges", "--network", "slirp4netns",
                   "-p", f"127.0.0.1:{port}:2375", "-v", str(socket_path) + ":/socket/podman.sock:ro",
                   "-v", str(Path(__file__).with_name("kopia-m0-proxy.cfg").resolve()) + ":/config/proxy.cfg:ro",
                   PROXY, "haproxy", "-db", "-f", "/config/proxy.cfg")
            deadline = time.monotonic() + 15
            while True:
                try:
                    status, listing = call(port, "GET", "/containers/json")
                    if status == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Dev socket proxy did not become ready")
                time.sleep(.1)
            identity = next(item["Id"] for item in listing if writer in " ".join(item["Names"]))
            report["proxy_denied"] = {}
            for method, path in (("GET", "/images/json"), ("GET", "/volumes"),
                                 ("GET", f"/containers/{identity}/archive?path=/data"),
                                 ("POST", "/containers/create"), ("POST", f"/containers/{identity}/kill"),
                                 ("DELETE", f"/containers/{identity}")):
                status, _ = call(port, method, path, {})
                report["proxy_denied"][method + " " + path] = status
                if status != 403:
                    raise RuntimeError("Proxy allowed a forbidden operation: " + method + " " + path)
            status, created = call(port, "POST", f"/containers/{identity}/exec", {
                "AttachStdout": True, "AttachStderr": True, "Tty": False,
                "Cmd": ["python3", "-c",
                        "import sys,time; "
                        "[(sys.stdout.buffer.write(b'x'*1048576),sys.stdout.buffer.flush(),time.sleep(.1)) for _ in range(16)]; "
                        "sys.stderr.write('m0-stderr-control\\n')"],
            })
            if status != 201:
                raise RuntimeError("Exec creation failed: " + str(status))
            execution = created["Id"]
            started = time.monotonic()
            connection, response = call(port, "POST", f"/exec/{execution}/start",
                                        {"Detach": False, "Tty": False}, stream=True)
            report["exec"]["http_status"] = response.status
            digest = hashlib.sha256()
            count, first, stderr = 0, None, bytearray()
            while header := response.read(8):
                header += exact(response, 8 - len(header))
                channel, size = struct.unpack(">BxxxI", header)
                data = exact(response, size)
                if channel == 1:
                    first = first if first is not None else time.monotonic() - started
                    digest.update(data)
                    count += len(data)
                elif channel == 2:
                    stderr.extend(data)
                else:
                    raise RuntimeError("Unexpected exec channel")
            connection.close()
            total = time.monotonic() - started
            status, finished = call(port, "GET", f"/exec/{execution}/json")
            report["exec"].update(bytes=count, first_byte_seconds=first, total_seconds=total,
                                  sha256=digest.hexdigest(), stderr=stderr.decode(), exit_code=finished["ExitCode"])
            if (count != 16 * 1024 * 1024 or digest.hexdigest() != hashlib.sha256(b"x" * count).hexdigest()
                    or stderr != b"m0-stderr-control\n" or finished["ExitCode"] != 0
                    or first is None or total - first < 1):
                raise RuntimeError("Exec stream integrity, framing or incremental-delivery check failed")
            stop_started = time.monotonic()
            status, _ = call(port, "POST", f"/containers/{identity}/stop?t=10")
            report["stop"] = {"status": status, "seconds": time.monotonic() - stop_started}
            status, stopped = call(port, "GET", f"/containers/{identity}/json")
            report["stop"]["exit_code"] = stopped["State"]["ExitCode"]
            if stopped["State"]["Running"] or stopped["State"]["ExitCode"] != 0:
                raise RuntimeError("Proxy stop did not gracefully stop the fixture writer")
            status, _ = call(port, "POST", f"/containers/{identity}/start")
            report["start_status"] = status
            status, restarted = call(port, "GET", f"/containers/{identity}/json")
            if not restarted["State"]["Running"]:
                raise RuntimeError("Proxy start did not restart the fixture writer")

            repository_path = temporary / "repository"
            repository_path.mkdir()
            password = secrets.token_urlsafe(32)
            public = Path(args.key + ".pub").read_text().split()
            wrong_hosts = temporary / "wrong-known-hosts"
            host_entry = args.sftp_host if args.sftp_port == 22 else f"[{args.sftp_host}]:{args.sftp_port}"
            wrong_hosts.write_text(f"{host_entry} {public[0]} {public[1]}\n")
            for case, operation, secret, hosts in (
                    ("missing", "connect", password, args.known_hosts),
                    ("create", "create", password, args.known_hosts),
                    ("existing", "connect", password, args.known_hosts),
                    ("wrong_password", "connect", "incorrect-dev-only-password", args.known_hosts),
                    ("wrong_host_key", "connect", password, str(wrong_hosts))):
                invocation = [
                    "run", "--rm", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "label=disable", "--network", "slirp4netns:allow_host_loopback=true",
                    "--tmpfs", "/tmp:rw,mode=1777", "-e", "KOPIA_PASSWORD",
                    "-e", "KOPIA_CACHE_DIRECTORY=/tmp/cache",
                    "-e", "KOPIA_CONFIG_PATH=/tmp/repository.config", "-e", "KOPIA_LOG_DIR=/tmp/logs",
                    "-v", args.key + ":/keys/id_ed25519:ro", "-v", str(hosts) + ":/keys/known_hosts:ro",
                    KOPIA, "--config-file=/tmp/repository.config", "--log-dir=/tmp/logs",
                    "repository", operation, "sftp", "--cache-directory=/tmp/cache", "--no-check-for-updates",
                    "--host=" + args.sftp_host,
                    "--port=" + str(args.sftp_port), "--username=" + os.environ["USER"],
                    "--path=" + str(repository_path), "--keyfile=/keys/id_ed25519",
                    "--known-hosts=/keys/known_hosts", "--no-persist-credentials", "--no-use-keyring",
                    "--no-embed-credentials",
                ]
                result = podman(*invocation, check=False, env=dict(os.environ, KOPIA_PASSWORD=secret))
                result["stderr"] = result["stderr"].replace(secret, "[redacted]")
                result["stdout"] = result["stdout"].replace(secret, "[redacted]")
                if case == "missing":
                    result["remote_entries"] = sorted(path.name for path in repository_path.iterdir())
                report["kopia"][case] = result
            if (report["kopia"]["missing"]["exit"] == 0 or report["kopia"]["create"]["exit"] != 0
                    or report["kopia"]["existing"]["exit"] != 0
                    or report["kopia"]["wrong_password"]["exit"] == 0
                    or report["kopia"]["wrong_host_key"]["exit"] == 0):
                raise RuntimeError("Kopia failure-classification control failed")
            if (report["kopia"]["missing"]["stderr"] !=
                    "error connecting to repository: repository not initialized in the provided storage"
                    or "invalid repository password" not in report["kopia"]["wrong_password"]["stderr"]
                    or "knownhosts: key mismatch" not in report["kopia"]["wrong_host_key"]["stderr"]):
                raise RuntimeError("Kopia errors did not match the measured, distinct failure classes")
        finally:
            for name in reversed(containers):
                report["cleanup"].append(podman("rm", "-f", name, check=False))
            if api is not None:
                api.terminate()
                api.wait(timeout=10)
            for name in reversed(volumes):
                report["cleanup"].append(podman("volume", "rm", name, check=False))
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
            if any(record["exit"] != 0 for record in report["cleanup"]):
                raise RuntimeError("Dev fixture cleanup failed; inspect the saved cleanup records")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-only", action="store_true", required=True)
    parser.add_argument("--sftp-root", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--sftp-host", default="host.containers.internal")
    parser.add_argument("--sftp-port", type=int, default=22)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())
