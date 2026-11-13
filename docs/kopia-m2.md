# M2: explicit restoration and the dev rebuild drill

## Scope and boundary

M2 adds the console [recovery command](../guest/sophon-restore), packaged in the
deployment ISO, and [native group restoration](../roles/kopia/files/restore.py).
Configure never calls it. The operator procedure, capabilities, approval,
interruption/retry and decrypted-staging cleanup are in the
[operator guide](../roles/kopia/README.md#explicit-group-restoration).

Only the isolated Debian/rootless-Podman dev VM at `10.0.60.66` was used.
The approved rebuild boundary was application containers and named volumes,
not replacing the VM. No production VM, production repository, production NFS
server or Proxmox API was contacted during M2.

The [renderer](../tests/spikes/render_kopia_m2.py) uses repository core templates.
The [drill](../tests/spikes/kopia_m2.py) retains their backup declarations,
application images, native database layouts and LDAP/Gitea initialization.
Its explicit dev overrides use `dev.invalid`, synthetic local TLS, loopback-only
Gitea HTTP, no external OIDC, disabled Gitea actions, no runners/tunnels, and a
Traefik command with no Docker provider, resolver or ACME issuance. It does not
run the full Portainer/Ansible Configure orchestration or certs-dumper.

## Commands and observed outputs

### Repository validation

```sh
bash sophon-provision.sh test
```

```text
Ran 140 tests in 78.664s
OK (skipped=3)
Deployment step checks passed
```

The seven focused restore cases cover plan binding/no environment capture,
numeric ownership and mode, failed snapshots/changed images/unsafe source
paths, explicit approval before engine mutation, destination-parent symlinks,
staging corruption, missing-helper recovery, partially started group shutdown,
retry, and failed journal persistence without losing compensating stops.
Expected negative cases print errors; they are not suite failures.

```sh
docker run --rm -v "$PWD:/workspace" --entrypoint python3 \
  sophon-tools tests/backup_checks.py
```

```text
Ran 11 tests in 0.043s
OK
```

These are the existing primary collector failure/isolation/recovery checks,
plus rejection of collection while an operator restore is pending. They are
not substitutes for live native imports. One earlier full run exposed an
offline-build fixture whose mock exited without consuming its stdin, causing
an Ansible broken pipe. The mock now consumes the supplied build input like the
real engine; no cache/network assertions were removed or weakened.

### Actual offline media

```sh
bash sophon-provision.sh prepare
```

```text
localhost : ok=18 changed=2 unreachable=0 failed=0 skipped=7 rescued=0 ignored=0
```

Prepare only was run, never provisioning. The actual checked extraction/comparison commands (inside the tools image,
with `temp` a newly created private temporary directory) were:

```sh
xorriso -osirrox on -indev artifacts/bootstrap/sophon-deployment.iso \
  -extract /guest/sophon-restore "$temp/sophon-restore" \
  -extract /project/roles/kopia/files/restore.py "$temp/restore.py" \
  -extract /project/roles/kopia/files/backup.py "$temp/backup.py" \
  -extract /project/bootstrap/sophon-version "$temp/version"
cmp guest/sophon-restore "$temp/sophon-restore"
cmp roles/kopia/files/restore.py "$temp/restore.py"
cmp roles/kopia/files/backup.py "$temp/backup.py"
test -x "$temp/sophon-restore"
cat "$temp/version"
```

```text
source-sha256:3ec18ff3482f338875aa1b397e3b3f7a0b8890cb8f3f2ee0b9f9103a24573402
Offline restore launcher/module and backup-hold byte comparisons: PASS
```

The prepared image was also checked directly:

```sh
docker run --rm -v "$PWD:/workspace" --entrypoint python3 \
  localhost/sophon-kopia:0.23.1 -c \
  'from pathlib import Path; import restore,backup; assert all(Path("/app/"+name).read_bytes()==Path("/workspace/roles/kopia/files/"+name).read_bytes() for name in ("restore.py","backup.py")); print("Prepared image contains current restore and backup-hold modules: PASS")'
```

```text
Prepared image contains current restore and backup-hold modules: PASS
```

### Real application rebuild and restoration

Render private fixtures with the tools image, transfer them and the current
Kopia build context/launcher/drill to the dedicated dev account, and build there:

```sh
docker run --rm -v "$PWD:/workspace" -v "$SESSION_FILES:/session" \
  --entrypoint python3 sophon-tools \
  tests/spikes/render_kopia_m2.py /session/kopia-m2-render
```

```text
Rendered dev.invalid core fixtures: m2-dev-source:dc4dacc8c71031f2577460035e88246f43f7bd665f0a9f697f5d4fd796f766a8
```

`SESSION_FILES` is the private session-files directory, not a backup target.
The local engine is rootless: its container UID 0 maps to the invoking user.
An initial explicit host-UID mapping failed to write this private directory;
it was replaced with the default mapping, not loosened directory permissions.

```sh
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'podman --cgroup-manager=cgroupfs build --pull=never \
     -t localhost/sophon-kopia:m2-dev ~/m2-kopia'
```

```text
Successfully tagged localhost/sophon-kopia:m2-dev
dea11a19334de2750a18d8c45a063a03a383c1df89c87ff64797e0ac9e9d2416
```

```sh
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'python3 ~/m2-kopia/kopia_m2.py --dev-only --rendered ~/m2-render \
     --sftp-root /srv/sophon-m0/sftp --key ~/.ssh/m0-sftp \
     --known-hosts ~/.ssh/m0-sftp-known-hosts \
     --restore ~/m2-kopia/sophon-restore --output ~/m2-results.json'
```

The drill creates a dedicated encrypted dev SFTP repository, seeds real
Keycloak realm/user/client state, adds a real LDAP entry, and creates a real
Gitea API repository/commit. It takes a successful latest v3 snapshot, removes
the backup containers and application containers/volumes, then recreates fresh
applications from the same rendered IaC. It asserts the repository is absent
and generated INI/certificate/LDAP state differs before restoring.

Each group runs `plan --snapshot latest`, checks its exact ID, then
`apply --snapshot ID --approve` and explicit cleanup. Portainer is absent
throughout; the Kopia UI/proxy/backup containers are removed before restoration.
The console launcher uses only Podman, its packaged image and current explicit
connection overrides. No service definition or environment comes from a snapshot.

Selected output of the final successful run:

```text
snapshot: 254b7c4acab1fa1a15c2e80cfd5e3d49
fresh_application_volumes: true
partial_native_import_stopped_entire_group: true
backup_blocked_during_failed_restore: true
post_import_failure_stopped_entire_group: true
keycloak_login_and_ldap_password_bind: true
all_comparisons_equal: true
backup_stack_stopped_during_restore: true
normal_backup_resumed: true
slaptest: config file testing succeeded
```

The source and restored values were identical:

| Comparison | Both values |
|---|---|
| Keycloak native realm export | `649be48fd6d3a8f170602dd7d1998026ca6fc33f6068d9180a602656f1f461b0` |
| LDAP entries | `92f914e09f4a815ba777097f20360c2bc0b89c05407ece2899af83f898a113ad` |
| Certificate state | `e8aa7f18b440f63eecc1db0e4273c475d8d2a23922fbf7353d3025b7e5af5429` |
| Actual Gitea commit | `2e872fbb3ea64b662c09710a97e078f611691014` |
| Gitea SSH private identity digest | `27d433fb34c613ed80db27675ebca5d804769c13b21c689a28100b27725d0efa` |
| Gitea selected INI identity digest | `022daa253da74473b3022d084e95b473149838e3cd603bea82043d84ee230345` |

The test-only API shim first replays only the leading global SQL before database
creation, exits 97, and asserts the entire group remains stopped and collection
is blocked. A second attempt completes import but deliberately fails consumer
start; the entire group again stops. A third, unmodified attempt succeeds.
After comparisons and actual Keycloak/LDAPS authentication, the ordinary
backup/proxy stack is recreated and a new normal collector run uploads successfully.
All 19 recorded final cleanup operations returned 0; each per-group state/helper
cleanup was also checked before its recovery volume was removed.

## Diagnosis and limitations

Earlier drill attempts failed, rather than passing with relaxed comparisons:
LDAP writes needed a proper verified LDAPS administrator bind; stopped collector
containers still held source-volume references; and Podman's dependent containers
needed ordered removal. These were fixed in the dev harness.

Native LDAP import then exposed the vendor's leftover `dc=nodomain` entry outside
the configured suffix. Reimporting it fails. The profile now requires the declared
base to equal the MDB suffix and imports only that site's exported state into
fresh schema/configuration. It refuses partial-suffix merges.

A real PostgreSQL retry initially failed because `pg_dumpall --clean` does not
drop `postgres`/`template1` before dropping roles; prior imports leave ownership
and privilege dependencies there. The importer settles those dependencies under
the non-droppable initial role before replay. It disables that role afterward.
A subsequent partial-replay probe showed that `template1` itself may already
have been dropped at interruption. The importer reconstructs absent system
databases using native `CREATE DATABASE ... TEMPLATE template0` before replay,
not filesystem copies. The successful drill above exercises both partially
applied and previously completed imports. Native import failures never resume applications
with partial state. A persisted coordination hold prevents backup of that state
after coordinator death; only success or explicit abandonment removes it.

Native Keycloak exports are canonicalized as sorted JSON; LDAP entries and
attributes are sorted, unfolding LDIF and ignoring only `contextCSN` (replication
bookkeeping). Gitea comparisons use the actual API commit SHA and SHA-256 digests
of generated SSH/INI identity. Certificate comparison is the synthetic
`acme.json` byte digest, not issuance or an end-to-end TLS/certs-dumper test.

Not verified: Fedora CoreOS/SELinux enforcement, whole-VM rebuilding, full-site
Configure/SSO integration, cross-image migrations, production cutover, very large
datasets, every external workload's native export, or an end-to-end SIGKILL drill
against the restore coordinator. Unit recovery tests and real failed native
import/restart retries are not a claim that every crash window was exercised.
The VM and cached images remain available; fixtures/repositories are disposable.

Review against the M1 branch point covered standards and the M2 specification.
It addressed interrupted helper creation, staged-content tampering, partial
startup, journal-write failure, PostgreSQL repeat/partial replay, LDAP suffix
validation and backup/restore coordination. No known M2 blocker remains in the
implemented, measured dev scope; the limits above are not production claims.

M3 onboarding and M4's optional coarse layer remain separate gated milestones.
