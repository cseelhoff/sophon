# M4: optional weekly coarse layer

## Approved scope

This implements the simplified design approved after the initial complexity
assessment: no dependency graph/order. Remember previously running application
IDs, stop them, strictly reflink-clone the volume tree, restart those IDs in any
order, then upload. Previously stopped containers stay stopped.

The [operator guide](../roles/kopia/README.md#optional-weekly-coarse-layer)
documents opt-in inputs, dedicated same-filesystem bind storage, recovery,
retention, failure handling and last-resort-only restoration. Nothing is enabled
by default. No host units, filesystem snapshots or other storage backends were
added. The primary backup remains selective files/native database exports.

## Repository checks actually run

```sh
bash sophon-provision.sh test
```

```text
Ran 157 tests in 92.849s
OK (skipped=3)
Deployment step checks passed
```

Focused checks after the additional infrastructure fixture:

```sh
docker run --rm -v "$PWD:/workspace" --entrypoint bash sophon-tools \
  -lc 'cd /workspace/tests && python3 -m unittest test_coarse test_kopia'
```

```text
Ran 11 tests in 23.951s
OK
```

Cases cover stop/clone/restart ordering, no environment metadata capture,
previously stopped and infrastructure containers left alone, strict-reflink
failure with no plain-copy attempt, settling a lost stop response, deleted-ID
refusal while other restarts proceed, and retained recovery holds. Rendering
checks prove default absence and opt-in same-image services, separate private
proxy networking and read-only source binding. Input checks reject root,
overlapping and non-normalized host paths.

## Actual offline Prepare

```sh
bash sophon-provision.sh prepare
```

```text
localhost : ok=18 changed=2 unreachable=0 failed=0 skipped=7 rescued=0 ignored=0
```

No new upstream image was needed: the shared Kopia image is rebuilt and the
already pinned HAProxy image is reused. Checked extraction inside the tools
image, with a fresh temporary directory:

```sh
xorriso -osirrox on -indev artifacts/bootstrap/sophon-deployment.iso \
  -extract /project/roles/kopia/files/coarse.py "$temp/coarse.py" \
  -extract /project/roles/kopia/files/coarse-proxy.cfg "$temp/coarse-proxy.cfg" \
  -extract /project/roles/kopia/templates/docker-compose.yml.j2 "$temp/compose.j2" \
  -extract /project/bootstrap/sophon-version "$temp/version"
cmp roles/kopia/files/coarse.py "$temp/coarse.py"
cmp roles/kopia/files/coarse-proxy.cfg "$temp/coarse-proxy.cfg"
cmp roles/kopia/templates/docker-compose.yml.j2 "$temp/compose.j2"
cat "$temp/version"
```

```text
source-sha256:257754a166ffe75f41984d44e16bdf66ff81d9d678631a4cf51a794f97b03b13
Offline M4 controller/proxy/stack byte comparisons: PASS
```

```sh
docker run --rm -v "$PWD:/workspace" --entrypoint python3 \
  localhost/sophon-kopia:0.23.1 -c \
  'from pathlib import Path;import coarse;assert Path("/app/coarse.py").read_bytes()==Path("/workspace/roles/kopia/files/coarse.py").read_bytes();print("Prepared shared image contains current coarse controller: PASS")'
```

```text
Prepared shared image contains current coarse controller: PASS
```

## Real dev checks

Only isolated VM 107, `10.0.60.66`, was used. No production VM or repository was
accessed. The [dev harness](../tests/spikes/kopia_m4.py) requires an idle dedicated
rootless engine, uses disposable workloads/data/SFTP storage, and never starts
Cloudflare, certificate issuance, Gitea runners or production-facing identities.

After transferring the current build context and harness:

```sh
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'podman --cgroup-manager=cgroupfs build --pull=never \
     -t localhost/sophon-kopia:m4-dev ~/m4-kopia'
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'python3 ~/m4-kopia/kopia_m4.py --dev-only --context ~/m4-kopia \
     --work-parent /srv/sophon-m0 --sftp-root /srv/sophon-m0/sftp \
     --key ~/.ssh/m0-sftp --known-hosts ~/.ssh/m0-sftp-known-hosts \
     --output ~/m4-results.json'
```

Observed build:

```text
Successfully tagged localhost/sophon-kopia:m4-dev
f4707b886f894bb3442215aedb94daa9725bc3e05cd5e349ff27c5bbe6aaae17
```

Final output:

```text
proxy_list_allowed_inspect_exec_archive_delete_images_denied: true
unowned_work_wrong_volume_root_and_overlap_rejected_before_stops: true
weekly_due_seconds: 604800
separate_source_retains_exactly_four_after_five_runs: true
restored_real_files_and_numeric_ownership: [1001, 1002, 256]
previously_stopped_container_stayed_stopped: true
upload_failure_keeps_ready_copy_and_retry_does_not_pause_again: true
pending_coarse_recovery_blocks_primary_and_restore: true
sigkill_mid_stop_recovers_running_ids_without_repository: true
tmpfs_reflink_failure_skips_and_resumes_without_upload: true
```

Mode `256` is octal `0400`. A real Kopia restore produced the original
81,920-byte file content/digest and numeric UID/GID/mode. Reading that restored
file as the host user originally failed because the preserved sub-UID owns it;
verification correctly runs inside the rootless namespace with the tested
capabilities, rather than weakening the permissions.

The actual proxy returns 200 for list and 403 for inspect/exec/archive/delete/
image routes. The dev-only fixture publishes that restricted proxy on loopback
for these checks; the production template publishes no proxy ports.
Host-side test inspection/exec is not permission granted to the coarse service.

Five successful runs leave exactly four snapshots on the separate source.
The stored scheduler deadline is checked against seven actual days in seconds,
not inferred from a configurable interval. Upload failure is induced by making
the disposable repository path unavailable: applications are already running
and a ready clone remains. Restoring SFTP and retrying leaves their start
timestamps unchanged. Only Kopia's exact newly-created `.shards` layout marker
is removed by the test before restoring the renamed fixture repository.

For crash recovery, the worker is SIGKILLed after a workload stops, before the
stop sequence completes. Primary collection and v3 restoration both refuse the
pending hold. With SFTP made unavailable, restarting the same coordinator
resumes the original running IDs while leaving the originally stopped workload
alone. This exercises the actual service's startup recovery, not a mock.

A separate tmpfs-backed source/staging arrangement cannot reflink; the run exits
1 with the no-fallback error, keeps no ready snapshot, and resumes workloads.
Unowned working data, a wrong source root and source/work alias mounts are
rejected before any application stop. All eight final resource cleanup
operations returned 0; temporary misconfiguration/restore-probe resources were
also explicitly removed.

## Limits and completion

This is not a full live Portainer Configure rollout or Fedora CoreOS/SELinux
validation. No claim is made for huge datasets, unusual volume drivers,
uncoordinated external filesystem writers, real power-loss testing, live pod
infrastructure or reconstructing a whole site from the coarse snapshot.
Dependency ordering was deliberately removed by the approved clarification.

The working journal/config/cache/staging stay outside the source tree. The
small shared coordination hold is written before capture and is static during
the clone; it must not be blindly restored from a coarse copy. Core capabilities
remain `DAC_OVERRIDE`, `CHOWN`, `FOWNER`, with read-only rootfs and no-new-privileges.
No inspect/exec permission was added to the coarse proxy.

Review against the M3 branch point covered the approved M4 specification and
repository standards. M0-M4 implementation is complete in the documented dev
scope; merging the stacked PRs and any production rollout remain operator work.
