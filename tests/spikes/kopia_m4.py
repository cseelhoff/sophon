#!/usr/bin/env python3
"""Disposable dev proof of cold reflinks, list-only recovery and four-snapshot retention."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main(args):
    prefix = "sophon-m4-" + uuid.uuid4().hex[:8]
    environment = dict(os.environ, KOPIA_PASSWORD=secrets.token_urlsafe(32),
                       KOPIA_SFTP_HOST="host.containers.internal", KOPIA_SFTP_PORT="22",
                       KOPIA_SFTP_USERNAME=os.environ["USER"], KOPIA_CLIENT_USERNAME="backup",
                       KOPIA_CLIENT_HOSTNAME="m4-dev", SOPHON_VERSION="m4-dev:" + hashlib.sha256(
                           (Path(args.context) / "coarse.py").read_bytes()).hexdigest())
    report = {"checks": {}, "cleanup": []}
    containers, volumes = [], []
    api = None
    front, worker, proxy = prefix + "-front", prefix + "-worker", prefix + "-proxy"
    image = "localhost/sophon-kopia:m4-dev"

    def command(argv, *, check=True):
        result = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=300)
        if check and result.returncode:
            detail = result.stderr[-3000:]
            for name in ("KOPIA_PASSWORD", "KOPIA_SFTP_PRIVATE_KEY_B64"):
                detail = detail.replace(environment.get(name, "NEVER_MATCH"), "[redacted]")
            raise RuntimeError(f"{argv[:4]} failed: {detail}")
        return result

    def podman(*argv, **kwargs):
        return command(["podman", "--cgroup-manager=cgroupfs", *argv], **kwargs)

    def wait(predicate, label):
        for _ in range(600):
            if predicate():
                return
            time.sleep(.1)
        raise RuntimeError("Dev check timed out: " + label)

    def running(name):
        result = podman("inspect", name, "--format", "{{.State.Running}}", check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def coarse_worker(name, source_path, work_path, action, extra=()):
        flags = []
        for key in (*[key for key in environment if key.startswith("KOPIA_")], "SOPHON_VERSION", "ENGINE_URL"):
            flags += ["-e", key]
        containers.append(name)
        podman("run", "-d", "--pull=never", "--name", name, "--network", front,
               "--read-only", "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE", "--cap-add=CHOWN", "--cap-add=FOWNER",
               "--security-opt=label=disable", "--security-opt=no-new-privileges",
               "--tmpfs", "/tmp:rw,mode=1777", "--label", "sophon.coarse.control=worker",
               "-e", "KOPIA_CONFIG_PATH=/coarse/config/repository.config",
               "-e", "KOPIA_CACHE_DIRECTORY=/coarse/cache", "-e", "KOPIA_LOG_DIR=/coarse/logs",
               "-v", str(source_path) + ":/volumes:ro", "-v", str(work_path) + ":/coarse",
               "-v", coord + ":/source", *extra, *flags, "--entrypoint", "python3", image, "/app/coarse.py", action)

    assert podman("info", "--format", "{{.Host.Security.Rootless}}").stdout.strip() == "true"
    assert not podman("ps", "-a", "--format", "{{.Names}}").stdout.strip(), "Dedicated idle dev engine required"
    volume_root = Path(podman("info", "--format", "{{.Store.VolumePath}}").stdout.strip())
    with tempfile.TemporaryDirectory(prefix=prefix, dir=args.work_parent) as temporary:
        temporary = Path(temporary)
        with tempfile.TemporaryDirectory(prefix=prefix, dir=args.sftp_root) as remote:
            repository = Path(remote) / "repository"
            repository.mkdir()
            environment.update(KOPIA_SFTP_PATH=str(repository),
                               KOPIA_SFTP_PRIVATE_KEY_B64=base64.b64encode(Path(args.key).read_bytes()).decode(),
                               KOPIA_SFTP_KNOWN_HOSTS_B64=base64.b64encode(Path(args.known_hosts).read_bytes()).decode(),
                               ENGINE_URL="http://" + proxy + ":2375")
            work = temporary / "work"
            work.mkdir()
            coord, data = prefix + "-coord", prefix + "-data"
            socket = temporary / "engine.sock"
            try:
                podman("network", "create", front)
                for name in (coord, data):
                    podman("volume", "create", name)
                    volumes.append(name)
                podman("run", "--rm", "--pull=never", "--network", "none", "-v", coord + ":/source",
                       "--entrypoint", "python3", image, "-c",
                       "import backup;from pathlib import Path;"
                       "p=backup.private_dir('/source');(p/'.collector.lock').touch()")
                podman("run", "--rm", "--pull=never", "--network", "none", "-v", data + ":/data",
                       "--entrypoint", "python3", image, "-c",
                       "import os;from pathlib import Path;p=Path('/data/state');p.write_bytes(b'cold-state'*8192);"
                       "os.chown(p,1001,1002);os.chmod(p,0o400)")
                apps = []
                script = "import signal,time,sys;signal.signal(signal.SIGTERM,lambda *_:(time.sleep(2),sys.exit(0)));time.sleep(86400)"
                for index in range(3):
                    name = prefix + "-app-" + str(index)
                    containers.append(name)
                    apps.append(name)
                    podman("run", "-d", "--pull=never", "--name", name, "--network", front,
                           "-v", data + ":/data", "docker.io/library/python:3.14.10-slim-trixie",
                           "python3", "-c", script)
                podman("stop", apps[-1])
                ids = {name: podman("inspect", name, "--format", "{{.Id}}").stdout.strip() for name in apps}
                podman("run", "--rm", "--pull=never", "--network", "slirp4netns:allow_host_loopback=true",
                       "-e", "KOPIA_PASSWORD", "-e", "KOPIA_CONFIG_PATH=/tmp/repo.config",
                       "-e", "KOPIA_LOG_DIR=/tmp/logs", "-e", "KOPIA_CACHE_DIRECTORY=/tmp/cache",
                       "-v", args.key + ":/keys/key:ro", "-v", args.known_hosts + ":/keys/known_hosts:ro",
                       "docker.io/kopia/kopia:0.23.1", "repository", "create", "sftp",
                       "--host=host.containers.internal", "--username=" + os.environ["USER"],
                       "--path=" + str(repository), "--keyfile=/keys/key", "--known-hosts=/keys/known_hosts",
                       "--no-use-keyring", "--no-persist-credentials")
                api = subprocess.Popen(["podman", "--cgroup-manager=cgroupfs", "system", "service",
                                        "--time=0", "unix:" + str(socket)], env=environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait(socket.exists, "engine API")
                containers.append(proxy)
                podman("run", "-d", "--pull=never", "--name", proxy, "--network", front,
                       "--read-only", "--cap-drop=ALL", "--user", "0:0", "--label", "sophon.coarse.control=proxy",
                       "-p", "127.0.0.1::2375", "-v", str(socket) + ":/socket/podman.sock:ro",
                       "-v", str(Path(args.context) / "coarse-proxy.cfg") + ":/usr/local/etc/haproxy/haproxy.cfg:ro",
                       "docker.io/library/haproxy:3.2.6-alpine")
                address = "http://" + podman("port", proxy, "2375/tcp").stdout.strip()

                def status(path, method="GET"):
                    try:
                        with urllib.request.urlopen(urllib.request.Request(address + path, method=method), timeout=10) as response:
                            return response.status
                    except urllib.error.HTTPError as error:
                        return error.code

                wait(lambda: status("/v1.41/libpod/containers/json") == 200, "restricted proxy")
                for path, method in ((f"/v1.41/containers/{ids[apps[0]]}/json", "GET"),
                                     (f"/v1.41/containers/{ids[apps[0]]}/exec", "POST"),
                                     (f"/v1.41/containers/{ids[apps[0]]}/archive", "GET"),
                                     (f"/v1.41/containers/{ids[apps[0]]}", "DELETE"),
                                     ("/v1.41/images/json", "GET")):
                    assert status(path, method) == 403
                report["checks"]["proxy_list_allowed_inspect_exec_archive_delete_images_denied"] = True
                wrong = temporary / "wrong-source"
                wrong.mkdir()
                for index, src in enumerate((volume_root, wrong, volume_root)):
                    scratch = volume_root if index == 2 else temporary / ("misconfigured-" + str(index))
                    if index != 2:
                        scratch.mkdir()
                    if index == 0:
                        (scratch / "keep").write_text("unowned data")
                    bad = prefix + "-misconfigured-" + str(index)
                    coarse_worker(bad, src, scratch, "run")
                    assert podman("wait", bad).stdout.strip() == "1"
                    assert all(running(name) for name in apps[:-1]) and not running(apps[-1])
                    if index == 0:
                        assert (scratch / "keep").read_text() == "unowned data"
                    podman("rm", bad)
                    containers.remove(bad)
                report["checks"]["unowned_work_wrong_volume_root_and_overlap_rejected_before_stops"] = True
                began = time.time()
                coarse_worker(worker, volume_root, work, "serve")
                success = work / "last-run.json"
                wait(success.exists, "initial weekly backup")
                wait(lambda: not (work / "staging").exists(), "initial staging cleanup")
                assert json.loads(success.read_text())["status"] == "complete"
                due = json.loads((work / "schedule.json").read_text())["due"]
                assert began + 7 * 86400 - 1 <= due <= time.time() + 7 * 86400
                assert all(running(name) for name in apps[:-1]) and not running(apps[-1])
                for _ in range(4):
                    result = podman("exec", worker, "python3", "/app/coarse.py", "run")
                    assert json.loads(result.stdout)["status"] == "complete"
                source = "backup@m4-dev:/sophon-coarse"
                snapshots = json.loads(podman("exec", worker, "kopia", "snapshot", "list", source, "--json").stdout)
                assert len(snapshots) == 4, len(snapshots)
                latest = json.loads(success.read_text())["snapshot"]
                podman("exec", worker, "kopia", "snapshot", "restore", latest, "/coarse/verify",
                       "--no-skip-owners", "--no-skip-permissions", "--no-ignore-permission-errors")
                digest = podman("exec", worker, "python3", "-c",
                                "import hashlib;from pathlib import Path;"
                                f"print(hashlib.sha256(Path('/coarse/verify/volumes/{data}/_data/state').read_bytes()).hexdigest())").stdout.strip()
                assert digest == hashlib.sha256(b"cold-state" * 8192).hexdigest()
                attributes = json.loads(podman("exec", worker, "python3", "-c",
                    f"import os,json;s=os.stat('/coarse/verify/volumes/{data}/_data/state');"
                    "print(json.dumps([s.st_uid,s.st_gid,s.st_mode&0o777]))").stdout)
                assert attributes == [1001, 1002, 0o400]
                report["checks"]["weekly_due_seconds"] = 7 * 86400
                report["checks"]["separate_source_retains_exactly_four_after_five_runs"] = True
                report["checks"]["restored_real_files_and_numeric_ownership"] = attributes
                report["checks"]["previously_stopped_container_stayed_stopped"] = True
                offline = repository.with_name("repository-offline")
                repository.rename(offline)
                failed = podman("exec", worker, "python3", "/app/coarse.py", "run", check=False)
                assert failed.returncode == 1
                assert all(running(name) for name in apps[:-1]) and not running(apps[-1])
                assert (work / "pending.json").exists() and not (work / "journal.json").exists()
                started = [podman("inspect", name, "--format", "{{.State.StartedAt}}").stdout for name in apps[:-1]]
                if repository.exists():
                    assert sorted(path.name for path in repository.iterdir()) == [".shards"]
                    assert json.loads((repository / ".shards").read_text()) == {
                        "default": [3, 3], "maxNonShardedLength": 20}
                    (repository / ".shards").unlink()
                    repository.rmdir()
                offline.rename(repository)
                podman("exec", worker, "python3", "/app/coarse.py", "run")
                assert started == [podman("inspect", name, "--format", "{{.State.StartedAt}}").stdout for name in apps[:-1]]
                report["checks"]["upload_failure_keeps_ready_copy_and_retry_does_not_pause_again"] = True
                # Stop the coordinator mid-stop: its next startup must recover before repository access.
                active = subprocess.Popen(["podman", "--cgroup-manager=cgroupfs", "exec", worker,
                                           "python3", "/app/coarse.py", "run"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                journal = work / "journal.json"
                wait(lambda: journal.exists() and any(not running(name) for name in apps[:-1]), "one stopped writer")
                podman("kill", "--signal", "KILL", worker)
                active.wait(timeout=20)
                blocked = podman("run", "--rm", "--pull=never", "--network", "none", "-v", coord + ":/source",
                                 "--entrypoint", "python3", image, "-c",
                                 "import backup;backup.prepare({}, {}, '/source', 'coarse-hold-probe')", check=False)
                assert blocked.returncode == 1 and "Coarse backup recovery is pending" in blocked.stderr
                restore_state = prefix + "-restore-state"
                podman("volume", "create", restore_state)
                volumes.append(restore_state)
                blocked = podman("run", "--rm", "--pull=never", "--network", "none", "-v", coord + ":/source",
                                 "-v", restore_state + ":/restore", "--entrypoint", "python3", image,
                                 "/app/restore.py", "plan", "--group", "fixture", check=False)
                assert blocked.returncode == 1 and "Finish coarse backup restart recovery" in blocked.stderr
                podman("volume", "rm", restore_state)
                volumes.remove(restore_state)
                report["checks"]["pending_coarse_recovery_blocks_primary_and_restore"] = True
                repository.rename(offline)
                podman("start", worker)
                wait(lambda: not journal.exists() and all(running(name) for name in apps[:-1]), "startup recovery")
                assert not running(apps[-1])
                offline.rename(repository)
                report["checks"]["sigkill_mid_stop_recovers_running_ids_without_repository"] = True
                podman("stop", worker)
                with tempfile.TemporaryDirectory(prefix=prefix, dir="/dev/shm") as memory:
                    memory = Path(memory)
                    no_reflink, scratch = memory / "volumes", memory / "work"
                    no_reflink.mkdir()
                    scratch.mkdir()
                    (no_reflink / "state").write_bytes(b"not-reflinkable" * 1024)
                    bad = prefix + "-unsupported"
                    coarse_worker(bad, no_reflink, scratch, "run",
                                  ["-v", coord + ":/volumes/" + coord + "/_data:ro"])
                    assert podman("wait", bad).stdout.strip() == "1"
                    logs = podman("logs", bad).stdout + podman("logs", bad).stderr
                    assert "no plain-copy fallback" in logs, logs
                    assert not (scratch / "pending.json").exists()
                    assert all(running(name) for name in apps[:-1]) and not running(apps[-1])
                    podman("rm", bad)
                    containers.remove(bad)
                report["checks"]["tmpfs_reflink_failure_skips_and_resumes_without_upload"] = True
            finally:
                for name in reversed(containers):
                    if podman("container", "exists", name, check=False).returncode == 0:
                        result = podman("rm", "-f", name, check=False)
                        report["cleanup"].append({name: result.returncode})
                if api:
                    api.terminate()
                    api.wait(timeout=10)
                for name in volumes:
                    result = podman("volume", "rm", name, check=False)
                    report["cleanup"].append({name: result.returncode})
                if podman("network", "exists", front, check=False).returncode == 0:
                    report["cleanup"].append({front: podman("network", "rm", front, check=False).returncode})
                Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
                if any(code != 0 for row in report["cleanup"] for code in row.values()):
                    raise RuntimeError("Dev coarse cleanup failed")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-only", action="store_true", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--work-parent", required=True)
    parser.add_argument("--sftp-root", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--output", required=True)
    main(parser.parse_args())
