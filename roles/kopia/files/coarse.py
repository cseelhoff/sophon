#!/usr/bin/env python3
"""Optional weekly zero-downtime read-only Btrfs volume snapshots via Kopia native actions."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid

import backup
import runtime

BTRFS_SUPER_MAGIC = 0x9123683E
BTRFS_SUBVOL_INODE = 256
BTRFS_SUBVOL_RDONLY = 2
BTRFS_IOC_SNAP_CREATE_V2 = 0x50009417
BTRFS_IOC_SUBVOL_GETFLAGS = 0x80089419
BTRFS_IOC_SUBVOL_SETFLAGS = 0x4008941A
BTRFS_IOC_SNAP_DESTROY_V2 = 0x5000943F
NAME_OFFSET = 56
TARGET = "/sophon-coarse"


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
        return stage, manifest
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
        if json.loads(marker.read_text()) != expected:
            backup.fail("Unrecognized coarse working directory")
    else:
        if any(work.iterdir()):
            backup.fail("Use an empty dedicated coarse working directory on first initialization")
        backup.write_json(marker, expected)


def before_snapshot_action(environment):
    work = Path(environment.get("COARSE_ROOT", "/coarse"))
    source = Path(environment.get("SOURCE_ROOT", "/source"))
    volumes = Path(environment.get("COARSE_VOLUMES", "/volumes"))
    validate_mounts(volumes, work)
    with backup.locked(work):
        if not any(not (entry / "_data").is_symlink() and (entry / "_data").is_dir()
                   and (entry / "_data").samefile(source)
                   for entry in volumes.iterdir() if entry.is_dir() and not entry.is_symlink()):
            backup.fail("The selected source is not the Podman volumes subvolume containing backup coordination")
        stage, manifest = capture(volumes, work, source, runtime.required(environment, "SOPHON_VERSION"))
        backup.write_json(work / "last-run.json", {"status": "complete", "upload_status": "pending",
                                                   "failures": [], "run": manifest["run"]})
        print("KOPIA_SNAPSHOT_PATH=" + str(stage), flush=True)


def after_snapshot_action(environment):
    work = Path(environment.get("COARSE_ROOT", "/coarse"))
    with backup.locked(work):
        cleanup_staging(work)
        last_run = work / "last-run.json"
        if last_run.exists() and not last_run.is_symlink():
            record = json.loads(last_run.read_text())
            record["upload_status"] = "complete"
            backup.write_json(last_run, record)


def serve(environment):
    # ponytail: delegate weekly scheduling + 4-snapshot retention to Kopia's native server policy
    work = Path(environment.get("COARSE_ROOT", "/coarse"))
    volumes = Path(environment.get("COARSE_VOLUMES", "/volumes"))
    validate_mounts(volumes, work)
    initialize(work)
    for name in ("config", "cache", "logs"):
        backup.private_dir(work / name)
    with backup.locked(work):
        cleanup_staging(work)
    server_env = runtime.control_env(environment)
    server_env.setdefault("KOPIA_SERVER_ADDRESS", "http://127.0.0.1:51516")
    runtime.connect(server_env, enable_actions=True)
    client_user = runtime.required(server_env, "KOPIA_CLIENT_USERNAME")
    client_host = runtime.required(server_env, "KOPIA_CLIENT_HOSTNAME")
    runtime.kopia(server_env, "repository", "set-client", "--username=" + client_user, "--hostname=" + client_host)
    runtime.kopia(
        server_env, "policy", "set", TARGET,
        "--keep-latest=4", "--keep-hourly=0", "--keep-daily=0",
        "--keep-weekly=0", "--keep-monthly=0", "--keep-annual=0",
        "--ignore-identical-snapshots=false",
        "--before-snapshot-root-action=/app/coarse.py",
        "--after-snapshot-root-action=/app/coarse.py",
        "--no-persist-action-script",
        "--action-command-mode=essential",
        "--action-command-timeout=900s",
        "--snapshot-interval=" + server_env.get("KOPIA_COARSE_INTERVAL", "168h"),
    )
    runtime.run_server(
        ["kopia", "server", "start", "--address=" + server_env["KOPIA_SERVER_ADDRESS"],
         "--insecure", "--control-api", "--enable-actions", "--shutdown-grace-period=180s"],
        server_env, initial_target=TARGET, state_root=work,
    )


def run(environment):
    work = Path(environment.get("COARSE_ROOT", "/coarse"))
    env = dict(environment)
    env.setdefault("KOPIA_SERVER_ADDRESS", "http://127.0.0.1:51516")
    record = runtime.trigger_snapshot(env, work, TARGET)
    print(json.dumps(record), flush=True)


def main():
    os.umask(0o077)
    environment = dict(os.environ)
    kopia_action = environment.get("KOPIA_ACTION", "").strip()
    try:
        if kopia_action == "before-snapshot-root":
            before_snapshot_action(environment)
            return 0
        if kopia_action == "after-snapshot-root":
            after_snapshot_action(environment)
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("action", choices=("serve", "run"))
        args = parser.parse_args()
        if args.action == "run":
            run(environment)
        else:
            serve(environment)
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(runtime.startup_diagnostic(error, environment), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
