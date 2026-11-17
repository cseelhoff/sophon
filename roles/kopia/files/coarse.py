#!/usr/bin/env python3
"""Optional weekly zero-downtime read-only Btrfs volume snapshots (last-resort only)."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid

import backup
import runtime

WEEK = 7 * 86400
BTRFS_SUPER_MAGIC = 0x9123683E
BTRFS_SUBVOL_INODE = 256
BTRFS_SUBVOL_RDONLY = 2
BTRFS_IOC_SNAP_CREATE_V2 = 0x50009417
BTRFS_IOC_SUBVOL_GETFLAGS = 0x80089419
BTRFS_IOC_SUBVOL_SETFLAGS = 0x4008941A
BTRFS_IOC_SNAP_DESTROY_V2 = 0x5000943F
NAME_OFFSET = 56


class _Statfs(ctypes.Structure):
    _fields_ = [("f_type", ctypes.c_long), ("f_pad", ctypes.c_long * 15)]


def fs_magic(path):
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    info = _Statfs()
    if libc.statfs(os.fsencode(os.fspath(path)), ctypes.byref(info)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), str(path))
    return info.f_type & 0xFFFFFFFF


def is_btrfs_subvolume(path):
    path = Path(path)
    return (not path.is_symlink() and path.is_dir()
            and fs_magic(path) == BTRFS_SUPER_MAGIC and path.stat().st_ino == BTRFS_SUBVOL_INODE)


def create_readonly_snapshot(volumes, parent, name="volumes"):
    encoded = name.encode("utf-8")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or len(encoded) > 255:
        backup.fail("Invalid Btrfs snapshot name")
    src_fd = os.open(volumes, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    dst_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        buf = bytearray(4096)
        struct.pack_into("<qqQ", buf, 0, src_fd, 0, BTRFS_SUBVOL_RDONLY)
        buf[NAME_OFFSET:NAME_OFFSET + len(encoded)] = encoded
        fcntl.ioctl(dst_fd, BTRFS_IOC_SNAP_CREATE_V2, buf)
    except OSError as error:
        backup.fail(f"Coarse run skipped: Btrfs read-only snapshot failed (no plain-copy fallback): {error}")
    finally:
        os.close(src_fd)
        os.close(dst_fd)


def destroy_snapshot(parent, name="volumes"):
    target = Path(parent) / name
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink():
        backup.fail("Refusing to remove symlinked coarse snapshot target")
    encoded = name.encode("utf-8")
    if is_btrfs_subvolume(target):
        snap_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            flags, = struct.unpack("<Q", fcntl.ioctl(snap_fd, BTRFS_IOC_SUBVOL_GETFLAGS, struct.pack("<Q", 0)))
            if flags & BTRFS_SUBVOL_RDONLY:
                fcntl.ioctl(snap_fd, BTRFS_IOC_SUBVOL_SETFLAGS, struct.pack("<Q", flags & ~BTRFS_SUBVOL_RDONLY))
        finally:
            os.close(snap_fd)
        dst_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            buf = bytearray(4096)
            buf[NAME_OFFSET:NAME_OFFSET + len(encoded)] = encoded
            fcntl.ioctl(dst_fd, BTRFS_IOC_SNAP_DESTROY_V2, buf)
        finally:
            os.close(dst_fd)
    else:
        shutil.rmtree(target)


def cleanup_staging(work):
    stage = Path(work) / "staging"
    if stage.is_symlink():
        backup.fail("Coarse staging path is a symlink")
    if stage.exists():
        destroy_snapshot(stage, "volumes")
        shutil.rmtree(stage)


def capture(volumes, work, source, version):
    volumes, work, source = Path(volumes), Path(work), Path(source)
    if (source / "restore.json").exists() or (source / "restore.json").is_symlink():
        backup.fail("Finish operator restoration before a coarse snapshot")
    cleanup_staging(work)
    if fs_magic(work) != BTRFS_SUPER_MAGIC or not is_btrfs_subvolume(volumes):
        backup.fail("Coarse run skipped: source must be a Btrfs subvolume on the same Btrfs filesystem as working storage (no plain-copy fallback)")
    run_id = uuid.uuid4().hex
    stage = backup.private_dir(work / "staging")
    try:
        create_readonly_snapshot(volumes, stage, "volumes")
        manifest = {
            "format": "sophon-coarse-2",
            "mode": "btrfs-readonly-subvolume",
            "run": run_id,
            "sophon_version": version,
            "created_at": time.time(),
            "last_resort_only": True,
        }
        backup.write_json(stage / "manifest.json", manifest)
        subprocess.run(["sync", "-f", str(stage)], check=True, timeout=300)
        backup.write_json(work / "pending.json", {"version": 2, "run": run_id})
    except BaseException:
        cleanup_staging(work)
        raise


def validate_mounts(volumes, work):
    volumes, work = Path(volumes), Path(work)
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        decode = lambda value: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
        mounts[decode(fields[4])] = (fields[2], PurePosixPath(decode(fields[3])), fields[5].split(","))
    if str(volumes) not in mounts or str(work) not in mounts:
        backup.fail("Coarse source and working storage must be explicit bind mounts")
    (left_sb, left, source_flags), (right_sb, right, work_flags) = mounts[str(volumes)], mounts[str(work)]
    if "ro" not in source_flags or "rw" not in work_flags:
        backup.fail("Coarse volume source must be read-only and dedicated working storage writable")
    if left_sb != right_sb:
        backup.fail("Coarse run skipped: source must be a Btrfs subvolume on the same Btrfs filesystem as working storage (no plain-copy fallback)")
    if left.is_relative_to(right) or right.is_relative_to(left):
        backup.fail("Coarse source and working storage overlap; refusing recursive capture or writable source alias")


def initialize(work):
    marker = work / ".sophon-coarse.json"
    if marker.is_symlink():
        backup.fail("Coarse working-directory marker is a symlink")
    expected = {"version": 2, "mode": "btrfs-readonly-subvolume"}
    if marker.exists():
        current = json.loads(marker.read_text())
        if current == {"version": 1} and not (work / "journal.json").exists():
            if (work / "pending.json").exists():
                (work / "pending.json").unlink()
            cleanup_staging(work)
            backup.write_json(marker, expected)
        elif current != expected:
            backup.fail("Unrecognized coarse working directory")
    else:
        if any(work.iterdir()):
            backup.fail("Use an empty dedicated coarse working directory on first initialization")
        backup.write_json(marker, expected)


def run(environment):
    work = Path(environment.get("COARSE_ROOT", "/coarse"))
    source = Path(environment.get("SOURCE_ROOT", "/source"))
    volumes = Path(environment.get("COARSE_VOLUMES", "/volumes"))
    validate_mounts(volumes, work)
    initialize(work)
    with backup.locked(work):
        for name in ("config", "cache", "logs"):
            backup.private_dir(work / name)
        if not (work / "pending.json").exists():
            if not any(not (entry / "_data").is_symlink() and (entry / "_data").is_dir()
                       and (entry / "_data").samefile(source)
                       for entry in volumes.iterdir() if entry.is_dir() and not entry.is_symlink()):
                backup.fail("The selected source is not the Podman volumes subvolume containing backup coordination")
            capture(volumes, work, source, runtime.required(environment, "SOPHON_VERSION"))
        pending = json.loads((work / "pending.json").read_text())
        manifest = json.loads((work / "staging/manifest.json").read_text())
        if pending != {"version": 2, "run": manifest["run"]}:
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
        cleanup_staging(work)
        runtime.sync_ui(environment)
        print(json.dumps({"status": "complete", "snapshot": result["id"], "source": identity}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "run"))
    args = parser.parse_args()
    os.umask(0o077)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    environment = dict(os.environ)
    try:
        if args.action == "run":
            run(environment)
            return 0
        work = Path(environment.get("COARSE_ROOT", "/coarse"))
        volumes = Path(environment.get("COARSE_VOLUMES", "/volumes"))
        validate_mounts(volumes, work)
        initialize(work)
        schedule = work / "schedule.json"
        while True:
            due = json.loads(schedule.read_text())["due"] if schedule.exists() else 0
            if time.time() >= due:
                backup.write_json(schedule, {"due": time.time() + WEEK})
                try:
                    run(environment)
                except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    diagnostic = runtime.startup_diagnostic(error, environment)
                    print(diagnostic, file=sys.stderr, flush=True)
                    backup.write_json(work / "last-run.json", {
                        "status": "failed", "error": json.loads(diagnostic.split("=", 1)[1])["message"]})
            time.sleep(30)
    except KeyboardInterrupt:
        print("Coarse snapshot service stopped; workloads were never paused", file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(runtime.startup_diagnostic(error, environment), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
