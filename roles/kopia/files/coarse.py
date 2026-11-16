#!/usr/bin/env python3
"""Optional last-resort cold volume copies. Never a replacement for native exports."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

import backup
import runtime

WEEK = 7 * 86400
CONTROL = "sophon.coarse.control"


def inventory(api):
    rows = backup.engine(api, "GET", "/libpod/containers/json?all=true")
    if not isinstance(rows, list) or any(not backup.IDENTITY.fullmatch(row.get("Id", "")) for row in rows):
        backup.fail("Invalid coarse container inventory")
    return {row["Id"]: row for row in rows}


def workloads(rows):
    return {key: row for key, row in rows.items()
            if not row.get("IsInfra") and not row.get("IsService")
            and (row.get("Labels") or {}).get(CONTROL) not in ("worker", "proxy")}


def check_idle(source):
    if any((source / name).exists() or (source / name).is_symlink() for name in ("journal.json", "restore.json")):
        backup.fail("Finish primary backup/restore recovery before a coarse run")


def hold(source, journal):
    path = source / "coarse.json"
    if path.is_symlink():
        backup.fail("Coarse coordination hold is a symlink")
    expected = {"version": 1, "run": journal["run"]}
    if path.exists() and json.loads(path.read_text()) != expected:
        backup.fail("Coarse coordination hold belongs to another run")
    return path


def recover(api, work, source):
    path = work / "journal.json"
    if not path.exists():
        if (source / "coarse.json").exists() or (source / "coarse.json").is_symlink():
            backup.fail("Coarse hold has no recovery journal; inspect the configured working directory")
        return
    if path.is_symlink():
        backup.fail("Coarse recovery journal is a symlink")
    journal = json.loads(path.read_text())
    if (journal.get("version") != 1 or not re.fullmatch(r"[a-f0-9]{32}", journal.get("run", ""))
            or journal.get("phase") not in ("prepared", "stopping", "cloning", "starting")
            or not isinstance(journal.get("remaining"), list)
            or len(journal["remaining"]) != len(set(journal["remaining"]))
            or any(not backup.IDENTITY.fullmatch(key) for key in journal["remaining"])):
        backup.fail("Invalid coarse recovery journal")
    marker = hold(source, journal)
    if journal["phase"] != "prepared":
        if journal["remaining"] and not marker.exists():
            backup.fail("Pending coarse restarts lost their coordination hold")
        errors = []
        present = inventory(api)
        for key in list(journal["remaining"]):
            try:
                if key not in present:
                    backup.fail("Previously running container was deleted: " + key)
                if journal["phase"] == "stopping":
                    # A lost stop response must settle before its compensating start.
                    backup.engine(api, "POST", f"/containers/{key}/stop?t=120")
                backup.engine(api, "POST", f"/containers/{key}/start")
                if inventory(api).get(key, {}).get("State") != "running":
                    backup.fail("Previously running container did not restart: " + key)
                journal["remaining"].remove(key)
                backup.write_json(path, journal)
            except (RuntimeError, OSError) as error:
                errors.append(str(error))
        if errors:
            backup.fail("Coarse restart recovery remains pending: " + "; ".join(errors))
    marker.unlink(missing_ok=True)
    path.unlink()
    if not (work / "pending.json").exists() and (work / "staging").exists():
        shutil.rmtree(work / "staging")


def ensure_stopped(api):
    active = [key for key, row in workloads(inventory(api)).items() if row["State"] == "running"]
    if active:
        backup.fail("A workload is still running or appeared during coarse capture: " + ", ".join(active))


def capture(api, volumes, work, source, version):
    check_idle(source)
    rows = inventory(api)
    controls = [(row.get("Labels") or {}).get(CONTROL) for row in rows.values() if row["State"] == "running"]
    if controls.count("worker") != 1 or controls.count("proxy") != 1:
        backup.fail("Coarse capture requires exactly one running worker and restricted control proxy")
    targets = workloads(rows)
    if any(row["State"] not in ("running", "exited", "stopped", "created", "configured") for row in targets.values()):
        backup.fail("A workload is paused or transitioning; refusing coarse capture")
    if (work / "staging").exists():
        backup.fail("Unowned coarse staging exists; inspect it before another capture")
    running = {key: row for key, row in targets.items() if row["State"] == "running"}
    journal = {"version": 1, "run": uuid.uuid4().hex, "phase": "prepared", "remaining": list(running)}
    backup.write_json(work / "journal.json", journal)
    backup.write_json(hold(source, journal), {"version": 1, "run": journal["run"]})
    journal["phase"] = "stopping"
    backup.write_json(work / "journal.json", journal)
    try:
        for key in running:
            backup.engine(api, "POST", f"/containers/{key}/stop?t=120")
        stopped = inventory(api)
        for key in running:
            row = stopped.get(key)
            if row is None or row["State"] not in ("exited", "stopped") or row.get("ExitCode") == 137:
                backup.fail("A workload disappeared or did not stop gracefully: " + key)
        ensure_stopped(api)
        journal["phase"] = "cloning"
        backup.write_json(work / "journal.json", journal)
        stage = backup.private_dir(work / "staging")
        result = subprocess.run(["cp", "--archive", "--reflink=always", "--", str(volumes) + "/.",
                                 str(stage / "volumes")], capture_output=True, text=True, timeout=86400)
        if result.returncode:
            backup.fail("Coarse run skipped: strict reflink clone failed (no plain-copy fallback): " + result.stderr.strip())
        ensure_stopped(api)
        manifest = {"format": "sophon-coarse-1", "run": journal["run"], "sophon_version": version,
                    "created_at": time.time(), "last_resort_only": True, "containers": [
                        {"id": key, "names": row["Names"], "image": row["Image"], "image_id": row["ImageID"],
                         "was_running": key in running} for key, row in targets.items()]}
        backup.write_json(stage / "manifest.json", manifest)
        subprocess.run(["sync", "-f", str(stage)], check=True, timeout=300)
        backup.write_json(work / "pending.json", {"version": 1, "run": journal["run"]})
        journal["phase"] = "starting"
        backup.write_json(work / "journal.json", journal)
    finally:
        recover(api, work, source)


def validate_mounts(volumes, work):
    volumes, work = Path(volumes), Path(work)
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        decode = lambda value: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
        mounts[decode(fields[4])] = (PurePosixPath(decode(fields[3])), fields[5].split(","))
    if str(volumes) not in mounts or str(work) not in mounts:
        backup.fail("Coarse source and working storage must be explicit bind mounts")
    (left, source_flags), (right, work_flags) = mounts[str(volumes)], mounts[str(work)]
    if "ro" not in source_flags or "rw" not in work_flags:
        backup.fail("Coarse volume source must be read-only and dedicated working storage writable")
    if volumes.stat().st_dev == work.stat().st_dev and (left.is_relative_to(right) or right.is_relative_to(left)):
        backup.fail("Coarse source and working storage overlap; refusing recursive capture or writable source alias")


def initialize(work):
    marker = work / ".sophon-coarse.json"
    if marker.is_symlink():
        backup.fail("Coarse working-directory marker is a symlink")
    if marker.exists():
        if json.loads(marker.read_text()) != {"version": 1}:
            backup.fail("Unrecognized coarse working directory")
    else:
        if any(work.iterdir()):
            backup.fail("Use an empty dedicated coarse working directory on first initialization")
        backup.write_json(marker, {"version": 1})


def run(environment, recover_only=False):
    work = Path(environment.get("COARSE_ROOT", "/coarse"))
    source = Path(environment.get("SOURCE_ROOT", "/source"))
    volumes = Path(environment.get("COARSE_VOLUMES", "/volumes"))
    validate_mounts(volumes, work)
    initialize(work)
    api = backup.engine_api(runtime.required(environment, "ENGINE_URL"))
    with backup.locked(work):
        for name in ("config", "cache", "logs"):
            backup.private_dir(work / name)
        with backup.locked(source):
            check_idle(source)
            recover(api, work, source)
            if recover_only:
                return
            if not (work / "pending.json").exists():
                if volumes.stat().st_dev != work.stat().st_dev:
                    backup.fail("Coarse run skipped: source and staging are on different filesystems")
                if not any(not (entry / "_data").is_symlink() and (entry / "_data").is_dir()
                           and (entry / "_data").samefile(source)
                           for entry in volumes.iterdir() if entry.is_dir() and not entry.is_symlink()):
                    backup.fail("The selected source is not the Podman volumes root containing backup coordination")
                capture(api, volumes, work, source, runtime.required(environment, "SOPHON_VERSION"))
        pending = json.loads((work / "pending.json").read_text())
        manifest = json.loads((work / "staging/manifest.json").read_text())
        if pending != {"version": 1, "run": manifest["run"]}:
            backup.fail("Coarse upload staging does not match its durable ready record")
        runtime.connect(environment)
        identity = runtime.required(environment, "KOPIA_CLIENT_USERNAME") + "@" + runtime.required(
            environment, "KOPIA_CLIENT_HOSTNAME") + ":/sophon-coarse"
        runtime.kopia(environment, "policy", "set", identity, "--keep-latest=4", "--keep-hourly=0",
                      "--keep-daily=0", "--keep-weekly=0", "--keep-monthly=0", "--keep-annual=0",
                      "--snapshot-interval=0s", "--ignore-identical-snapshots=false")
        result = json.loads(runtime.kopia(environment, "snapshot", "create", str(work / "staging"),
                                         "--override-source=" + identity, "--json", "--fail-fast", timeout=86400))
        if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", result["id"])
                or result.get("rootEntry", {}).get("summ", {}).get("numFailed") != 0):
            backup.fail("Coarse snapshot was incomplete; immutable upload staging retained")
        runtime.kopia(environment, "snapshot", "expire", identity, "--delete")
        backup.write_json(work / "last-run.json", {"status": "complete", "snapshot": result["id"],
                                                  "source": identity, "run": pending["run"]})
        (work / "pending.json").unlink()
        shutil.rmtree(work / "staging")
        print(json.dumps({"status": "complete", "snapshot": result["id"], "source": identity}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "run", "recover"))
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    environment = dict(os.environ)
    try:
        if args.action != "serve":
            run(environment, args.action == "recover")
            return 0
        run(environment, recover_only=True)
        schedule = Path(environment.get("COARSE_ROOT", "/coarse")) / "schedule.json"
        while True:
            due = json.loads(schedule.read_text())["due"] if schedule.exists() else 0
            if time.time() >= due:
                backup.write_json(schedule, {"due": time.time() + WEEK})
                try:
                    run(environment)
                except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    diagnostic = runtime.startup_diagnostic(error, environment)
                    print(diagnostic, file=sys.stderr, flush=True)
                    work = Path(environment.get("COARSE_ROOT", "/coarse"))
                    backup.write_json(work / "last-run.json", {
                        "status": "failed", "error": json.loads(diagnostic.split("=", 1)[1])["message"]})
                    if (work / "journal.json").exists():
                        return 1
            time.sleep(30)
    except KeyboardInterrupt:
        print("Coarse backup interrupted; retained journal will resume previously running containers", file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(runtime.startup_diagnostic(error, environment), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
