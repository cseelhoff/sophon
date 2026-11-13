# Kopia redesign: M1 implementation and evidence

M1 implements the v3 collector and core declarations. **Stop for review before
M2.** The overall redesign is not complete: the group restore/drill, simpler
SFTP onboarding and optional coarse layer remain M2–M4.

## Delivered

* Named read-only source mounts aggregated from each core role's declaration
  plus `SOPHON_KOPIA_EXTRA_VOLUMES`; no whole-volumes-directory mount.
* Pinned HAProxy 3.2.6-alpine engine proxy in the actual offline bundle. Only
  list, inspect, exec, stop and start are exposed; the collector has no socket.
* V3-only flat path/dump/group/stop labels and a new v3 real-files manifest.
  The Portainer transport/token, native manager backup, tar formats, v1/v2
  parser and old restore/action wrappers are removed, not adapted.
* Native stdout exports, stopped-writer reflink clones and guarded plain-copy
  fallback. Writers resume before upload.
* Fsynced exact-identity stop/export recovery, including lost stop responses,
  failed intent persistence, cancellation before resume and replaced-writer
  refusal.
* Successful groups upload even if another fails; the manifest/report says
  `partial` and the operator command returns 1. Upload state is explicit, and
  incomplete native Kopia results are not accepted as complete backups.
* A container supervisor schedules collector commands; no host backup unit or
  timer, Portainer/Gitea dependency, or UI snapshot action is required.
* Every core role uses v3. Portainer bootstrap refreshes existing managed
  metadata without changing its image ID or deleting its data volume, and
  refuses an unexpected data mount.
* [Operator documentation](../roles/kopia/README.md),
  [outside-workload migration](kopia-v3-migration.md), updated architecture,
  decisions and inputs, and a separate [legacy evidence archive](kopia-legacy-evidence.md).

Core filesystem inputs are only Traefik's ACME identity and selected Gitea
user/identity paths. PostgreSQL and LDAP data directories are never mounted
into the collector. Gitea's mixed configuration file is filtered to declared
generated identity keys rather than copied wholesale.

## Dev boundary

All live testing used the new `sophon-m0-dev` VM 107, Debian 13 / rootless Podman
5.4.2 / XFS, and new temporary SFTP repositories under `/srv/sophon-m0/sftp`.
No production VM or production repository connection was made.

The fixture runs the rendered backup stack with real PostgreSQL 18.6, Gitea
28.2 and OpenLDAP 2.6.10 producers. A synthetic certificate-state file stands
in for ACME state; Traefik/certificate issuance is never started. Gitea actions
are disabled, its offline mode is enabled, and there is no Cloudflare connector,
Gitea runner or Portainer container.

The dev fixture version for the measured collector/runtime source was:

```text
m1-dev-source:57470ffbb8242eb1d34d350d3c1379ef37553da8c526299b9660f6b87349f97c
```

This is a dev fingerprint, not a claim that the full site was rebuilt. The
actual deployment ISO separately records its full copied-IaC content version.

## Commands and observed results

Credential paths below are parameterized; their values/contents are not in Git.
The live command used `dev@10.0.60.66` with the dedicated dev SSH identity and
pinned known-hosts file.

### Rendering, image and real dev stack

```sh
docker run --rm -v "$PWD:/workspace" -v "$SESSION_FILES:/evidence" \
  --entrypoint python3 sophon-tools \
  tests/spikes/render_kopia_m1.py /evidence/m1-render
```

```text
Rendered dev-only M1 fixtures: m1-dev-source:57470ffbb8242eb1d34d350d3c1379ef37553da8c526299b9660f6b87349f97c
```

The rendered fixtures and current source were copied to the dev VM. Then:

```sh
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'podman --cgroup-manager=cgroupfs build -t localhost/sophon-kopia:m1-dev ~/m1-kopia &&
   python3 ~/m1-kopia/kopia_m1.py --dev-only --rendered "$HOME/m1-render" \
     --sftp-root /srv/sophon-m0/sftp --key "$HOME/.ssh/m0-sftp" \
     --known-hosts "$HOME/.ssh/m0-sftp-known-hosts" \
     --output "$HOME/m1-results.json"'
```

Observed build:

```text
Successfully tagged localhost/sophon-kopia:m1-dev
603ddd9a9e8f9eecf07c491f6b56b283642f34cbf95fd0d26c09ce782c691ecf
```

Selected JSON output from the successful run:

```text
portainer_present: false
complete_run.status: complete
complete_run.failures: []
snapshot.id: 7088afe26c19c3feba28ee7f20e69558
snapshot.source: backup@m1-dev:/sophon-state
snapshot.summary: size=311941 files=33 symlinks=1 numFailed=0
groups: gitea, kopia, ldap, traefik
restored_contents: real-files/native-SQL/native-LDIF/generated-identity: PASS
writers_resumed: true
```

The harness used native Kopia restore only into an isolated inspection
directory, not application replacement. It checked SQL containing a synthetic
PostgreSQL table, native site LDIF, Git loose-object SHA-1 integrity, the
generated SSH identity, filtered INI state without the database section, a
symlink preserved as a symlink rather than dereferenced, and no tar wrappers.
It also asserted that the unrelated container-environment marker was absent
from the report/manifest.

It then added an invalid declared volume and reran the collector. The harness
asserted operator exit **1**, a different uploaded snapshot, the same stable
source identity, and:

```text
partial_run.status: partial
snapshot.id: de3b87784d15e3430ab97dad17a0c5ab
failed group: missing
Volume not-mounted is not mounted into Kopia. Add "not-mounted" to
SOPHON_KOPIA_EXTRA_VOLUMES and redeploy the Kopia stack
```

Finally it killed the collector during a pending slow native export, with a
writer stopped, then restarted the backup container:

```text
interrupted_stop_recovery:
  collector_exit: 137
  writer_resumed: true
  journal_removed: true
  export_cancelled: true
  interrupted_staging_removed: true
```

All 12 recorded stack/container/volume/network cleanup operations returned 0.
The encrypted repositories were disposable dev fixtures and were removed.
The dev VM and cached images remain available for the next approved milestone.

### Repository and focused checks

```sh
bash sophon-provision.sh test
```

```text
Ran 133 tests in 77.602s
OK (skipped=3)
Deployment step checks passed
```

The cached-Prepare fixture now supplies the required cached Kopia archive/ID
and proxy archive. Its existing no-network, exact-manifest and changed-image
assertions remain; the previously incomplete fixture is not skipped or weakened.
Legacy restore/profile tests were retired with that intentionally removed
feature; replacement group-restore coverage belongs to M2, not this result.

```sh
docker run --rm -v "$PWD:/workspace" --entrypoint python3 sophon-tools tests/backup_checks.py
```

```text
Ran 10 tests in 0.052s
OK
```

Those cases include failure isolation, partial upload/nonzero return, failed
and empty exports, clone-before-restart ordering, no environment/archive
capture, retired-label rejection, pending restart recovery, replaced-writer
refusal, lost stop response and failed intent persistence. Expected negative
cases emit `ERROR group=...`; they are not suite failures.

The existing-Portainer metadata refresh was tested separately through the
bootstrap command fixture, including retention of the original immutable image,
retention of `portainer_data`, and refusal to stop/remove an unmanaged mount.
It was not exercised against a real or production Portainer database.

### Actual offline media

```sh
bash sophon-provision.sh prepare
```

```text
localhost : ok=18 changed=4 unreachable=0 failed=0 skipped=7 rescued=0 ignored=0
```

This ran Prepare only, not hypervisor provisioning. It pulled/saved the pinned
proxy, rebuilt/saved the v3 image and built the actual deployment ISO.
Checked `xorriso` listing of that ISO returned:

```text
'Dockerfile'
'backup.py'
'engine-proxy.cfg'
'runtime.py'
library-haproxy_3.2.6-alpine.tar 32996352 bytes
Offline v3 file/proxy-archive assertions: PASS
```

The extracted bootstrap compared byte-for-byte equal to the current v3 label
migration code. The built image had no `restore.py` or old action wrapper.
The extracted source-content version was:

```text
source-sha256:3fa9cd9d2360aef689db0fe71966a1e966a809330395e73dc4bb74789fca21aa
```

The first media-listing attempt used unsupported `xorriso -find -print`;
that listing failed and was replaced by a checked `-ls`/`-lsl` invocation.
The extraction and subsequent checked listing/image assertions passed.

## Review and remaining work

M1 review addressed stop-response uncertainty, failed durable intent,
per-group recovery-error isolation, unsafe INI selectors, incomplete upload
results and stale immutable management labels. There are no outstanding
known M1 findings in the implemented scope.

Not verified here: Fedora CoreOS/SELinux enforcement, full-site Configure,
production migration, actual Keycloak restore, application-state replacement,
all-workload rebuild drill, new SFTP pairing, or the weekly coarse layer.
The inspection restore above is not M2's operator restore or drill.

M2, M3 and M4 require their own milestones and PRs. No production rollout or
automatic restore has been performed.
