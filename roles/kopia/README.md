# Kopia: native hot-backup & restore guide

Sophon backs up application state, not the system. Rebuild by rerunning Configure,
then explicitly restore state. Images, binaries, caches and Configure-generated
configuration are not primary backups. **Nothing may exist only in Arcane:**
keep every stack definition in IaC (or Gitea repositories deployed via Gitea Actions)
and externally supplied secrets in a password manager or Gitea Secrets. The backup
never requests Arcane's database, stacks, container environment variables
(`Config.Env`), or an administrator API key.

## Deploy and operate

Prepare builds the custom Kopia 0.23.1 image (containing `backup.py`, `dump_proxy.py`,
`runtime.py`, `restore.py`, and `coarse.py`) and carries it in the offline bundle.
Configure deploys Kopia last, after the core stacks are running. Backups run in a
normal Compose stack (`kopia` + `dump-proxy`); no host backup service or timer is installed.

Use the newly prepared runner when installing/updating this version: pairing
needs its Kopia binary, OpenSSH client and Paramiko. Preparing media alone does
not update an installed VM. For an explicitly managed in-place update, carry the
new runner archive and load it as `core` with
`podman load -i /path/to/updated/runner.tar` before running the updated console;
an existing `sophon:latest` tag is not proof that its dependencies are current.

Supply the SFTP host/account/path, repository password and UI credentials through
[Configure inputs](../../docs/inputs.md#kopia-backup-inputs). The server/account
and writable directory must already exist. Before deploying any stacks, Configure
generates/reuses an ed25519 identity under project `credentials/kopia`, shows its
ready-to-paste public key, scans the host and requires independently verified
SHA256 fingerprints. Install that public key on the confined SFTP account.
For example, compare against this on the server console:

```sh
for key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$key" -E sha256; done
```

Client directory/files are 0700/0600 and excluded from Git, media and primary
backups. Rebuilds can authorize a new key. Every rerun checks the offered key
set against saved trust; a change, including an override of saved pins, fails
hard until an independently verified intentional re-pairing.

Preflight requires authentication and write/read/delete integrity, and reports
free space when supported. Only Kopia 0.23.1's exact missing-repository diagnostic
on an unused path offers operator-confirmed creation; all other errors fail
closed. `--run` can use saved/generated material or optional base64 overrides.
Explicit `SOPHON_KOPIA_CREATE_REPOSITORY=true` confirms unattended creation only
when missing; it never reinitializes existing storage and needs no reset step.
Startup only connects. A later startup/health failure surfaces the redacted Kopia
diagnostic through Ansible instead of hiding the reason behind `no_log`.

### Native Kopia scheduling & before/after snapshot actions

`runtime.py` connects to the SFTP repository with `--enable-actions`, configures
the policy on `/sophon-state` with:

* `--before-snapshot-root-action=/app/backup.py`
* `--after-snapshot-root-action=/app/backup.py`
* `--no-persist-action-script` (action scripts stay local to the container image, never stored in the repository)
* `--action-command-mode=essential` (any failed group or dump aborts the snapshot immediately so a broken or partial backup is never silently marked healthy)
* `--snapshot-interval=<SOPHON_KOPIA_SNAPSHOT_INTERVAL>` (default `6h`, or `0s` when `SOPHON_KOPIA_BACKUPS_ENABLED=false` or `KOPIA_READONLY_MODE=true`)

and runs `kopia server start --enable-actions` directly as the long-lived process.

Whenever a scheduled snapshot fires—or whenever an operator clicks **Snapshot Now**
on `/sophon-state` in the Kopia Web UI or runs `podman exec kopia python3 /app/backup.py run`:

1. **Before-snapshot action (`KOPIA_ACTION=before-snapshot-root`):** `backup.py` queries
   `dump-proxy` (`GET /v1/inventory`), stages declared named volumes and selective paths
   via Btrfs CoW reflinks (`cp --archive --reflink=always`, falling back to `--reflink=never`)
   into `/source/runs/<run_id>/`, writes `.kopiaignore` exclusion rules, filters INI
   identity files, streams hot database dumps via `POST /v1/dump/<container_id>/<dump_name>`,
   writes `manifest.json` + `/source/last-run.json`, and prints
   `KOPIA_SNAPSHOT_PATH=/source/runs/<run_id>` so Kopia snapshots that immutable staged tree
   under the logical identity `backup@sophon:/sophon-state`.
2. **After-snapshot action (`KOPIA_ACTION=after-snapshot-root`):** `backup.py` deletes
   `/source/runs/<run_id>` and marks `/source/last-run.json` `upload_status: "complete"`.

Run or inspect backups from the InfraVM console as `core`:

```sh
podman exec kopia python3 /app/backup.py run
podman exec kopia cat /source/last-run.json
podman logs kopia
```

Snapshots use stable identity `backup@sophon:/sophon-state` by default (`zstd`
content compression, content-defined chunking deduplication). Retention remains
latest 10, hourly 24, daily 14, weekly 8, monthly 12 and annual 3. Quick/full
maintenance intervals are 24/168 hours.

## What the core declares (100% hot, zero container stops)

| Role | Irreproducible state | Deliberately left out |
|---|---|---|
| Traefik | `acme.json` (`sophon.backup.paths: /acme/acme.json`): generated ACME identity and certificate/private-key state, copied online (Traefik writes `acme.json` atomically via temp file + rename). | Dynamic/file-provider configuration rendered by Configure; images and logs. |
| OpenLDAP | Online `slapcat` (`sophon.backup.dump`) of the configured site's directory subtree (`sophon.backup.dump.restore: ldap-subtree`), preserving user entries, password hashes and entry identity while LMDB MVCC stays online. | Live MDB files, vendor/Configure-created `cn=config`, legacy migration guard and certificates derived from ACME. Custom schemas/ACLs must be represented in site IaC. |
| Keycloak | Online `pg_dumpall` (`sophon.backup.dump`, `sophon.backup.dump.restore: postgres-cluster`) from `keycloak-db` via PostgreSQL MVCC without stopping Keycloak. | Raw PostgreSQL files and Keycloak's transient runtime directory. |
| Gitea | Whole `/data` volume (`sophon.backup.volumes: /data`) including Git repositories, **Git LFS content-addressed objects** (`git/lfs`), attachments, avatars, packages, and SSH/JWT keys; selective generated secrets from `/data/gitea/conf/app.ini` (`sophon.backup.ini`); and online `pg_dumpall` from `gitea-db`. Never stops Gitea. | Excluded via `.kopiaignore` (`sophon.backup.exclude`): `git/lfs/tmp/` (in-flight LFS uploads before atomic SHA-256 rename), `gitea/indexers/`, `gitea/queues/`, `gitea/sessions/`, `gitea/log/`, `gitea/tmp/`, plus externally supplied passwords in `app.ini` (auto-excluded from whole-volume copy and merged on restore). |
| CoreDNS | None (`sophon.backup.enable: "false"`): DNS records/configuration are reconstructed from IaC, labels and retained external Cloudflare inputs. | Binary, cache and transient inventory. |
| cloudflared | None (`sophon.backup.enable: "false"`): Configure reconnects the named tunnel using externally retained Cloudflare access. | Connector/token environment and image. |
| Arcane | None (`sophon.backup.enable: "false"`): Configure recreates the management UI and reconciles declarative Compose stacks directly under `/var/lib/sophon/stacks/`. | Its SQLite database, local encryption key (`credentials/arcane/`), stack/environment capture and admin credentials. UI-only configuration is unsupported. |
| Kopia/proxy | None: repository connection, native policy and proxy are reconstructed from deployment inputs. | Config/cache/log volumes, staging, and the repository itself. Keep repository decryption credentials outside it. |

Full native database exports necessarily contain related roles/schema and
configured seed rows as well as irreproducible application state; these are
coherent database recovery inputs, never a copy of the running database files.
Application-generated identity values remain encrypted by Kopia.

## Optional weekly coarse layer

**Last resort only.** Keep native exports/selective files and the explicit v3
group restore as the routine recovery path. This optional cold layer includes
raw databases, caches, reconstructible state and secrets found in named volumes.
It is not a VM/system backup and does not include unrelated host bind mounts.

It is off by default. Step 3 formats `/var` as Btrfs (`sophon-var`, mounted
`noatime,user_subvol_rm_allowed`) and `host-bootstrap.sh` creates
`/var/home/core/.local/share/containers/storage/volumes` as a Btrfs subvolume and
`/var/home/core/sophon-coarse` as its disjoint working directory. Both paths are
the defaults when enabling:

```sh
export SOPHON_KOPIA_COARSE_ENABLED=true
sophon-deploy
```

Override `SOPHON_KOPIA_COARSE_VOLUMES_DIR` and `SOPHON_KOPIA_COARSE_WORK_DIR`
only if Podman storage was relocated onto another Btrfs subvolume. First use
refuses nonempty/unowned working storage. Mount identity checks reject
recursive/writable-source aliases, and the selected volume subvolume must
actually contain the shared coordination volume.

The separate `kopia-coarse` service uses the primary image with **only
`DAC_OVERRIDE`** and **no Podman socket or proxy container**. It runs
`kopia server start --without-ui --enable-actions` with Kopia's native
`--snapshot-interval=168h` (seven days) and `--before-snapshot-root-action=/app/coarse.py`
/ `--after-snapshot-root-action=/app/coarse.py`. Zero containers are stopped or
restarted: before each snapshot `coarse.py` invokes `BTRFS_IOC_SNAP_CREATE_V2`
(`RDONLY`) on `/volumes` to create an atomic crash-consistent read-only Btrfs
subvolume snapshot at `/coarse/staging/volumes`, syncs it, emits
`KOPIA_SNAPSHOT_PATH=/coarse/staging/volumes` for Kopia to snapshot under
`backup@sophon:/sophon-coarse` (`--keep-latest=4` with hourly/daily/weekly/monthly/annual
extras disabled), and destroys the local Btrfs snapshot via `BTRFS_IOC_SNAP_DESTROY_V2`
in the after-snapshot action (and on startup cleanup if an earlier run was interrupted).
Non-Btrfs or non-subvolume sources fail explicitly with no plain-copy fallback.

Configure triggers an initial coarse snapshot immediately when enabled, after
which Kopia's native 168-hour policy maintains the weekly cadence. Primary
source/retention are unchanged.

```sh
podman logs kopia-coarse
podman exec kopia-coarse cat /coarse/last-run.json
podman exec kopia-coarse python3 /app/coarse.py run
```

Active operator restores (`restore.json`) fail the coarse before-snapshot action
until recovery completes so partial recovery state is never snapshotted.

Inspect coarse snapshots only into isolated working storage:

```sh
podman exec kopia-coarse kopia snapshot list backup@sophon:/sophon-coarse
podman exec kopia-coarse kopia snapshot restore SNAPSHOT_ID /coarse/inspection \
  --no-skip-owners --no-skip-permissions --no-ignore-permission-errors
```

Recovery from this layer is explicit, offline/operator-controlled and requires
matching application images and validation. It is not accepted by v3 group
restore or called by Configure. Prefer rebuilding from IaC, then restoring chosen
application `_data` contents only. **Do not blindly overwrite Podman volume
metadata, Arcane state or Kopia config/cache/coordination volumes**: rebuild
those, especially stale copied stop/restore/coarse holds. Retain external inputs
and repository credentials separately.

## Self-service container labels

Developers deploying workloads via Gitea Actions + Gitea Secrets opt into hot
backups and `sophon-restore` solely through concise Traefik-style `sophon.backup.*`
labels in their own `compose.yaml`—without touching Sophon's Ansible codebase,
restarting Kopia, or sharing database credentials with Kopia.

### Developer Compose example (files + PostgreSQL custom dump)

```yaml
services:
  web:
    image: ghcr.io/example/my-app:1.2.0
    volumes:
      - uploads:/var/lib/app/uploads
    labels:
      sophon.backup.enable: "true"
      # Defaults to com.docker.compose.project if omitted:
      sophon.backup.group: "my-app"
      # Accepts container mount path (/var/lib/app/uploads) or Compose volume name (uploads):
      sophon.backup.volumes: "/var/lib/app/uploads"
      sophon.backup.exclude: "tmp/,cache/"

  db:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASSWORD}   # Injected from Gitea Secrets; never exposed to Kopia
      POSTGRES_DB: app
    volumes:
      - pgdata:/var/lib/postgresql
    labels:
      sophon.backup.enable: "true"
      sophon.backup.group: "my-app"
      # Runs inside `db` via dump-proxy; -Fc -Z0 disables Postgres compression so Kopia zstd deduplicates blocks:
      sophon.backup.dump: 'exec pg_dump -U "$$POSTGRES_USER" -Fc -Z0 "$$POSTGRES_DB"'
      sophon.backup.dump.name: "app.dump"
      sophon.backup.dump.restore: "postgres-custom"

volumes:
  uploads:
  pgdata:
```

> **Compose `$` escaping:** Double `$$POSTGRES_USER` in `compose.yaml` so Docker/Podman
> Compose passes literal `$POSTGRES_USER` into the container label for in-container
> shell evaluation when `dump-proxy` triggers the dump.

### Supported `sophon.backup.*` labels

| Label | Meaning |
|---|---|
| `sophon.backup.enable` | Required (`"true"` or `"false"`). `"false"` explicitly marks a container reconstructible. |
| `sophon.backup.group` | Logical backup/restore group name. Defaults to `com.docker.compose.project` (or container name). |
| `sophon.backup.volumes` | Comma-separated container mount destinations (`/data`) or volume names (`uploads`) for whole-volume hot backup. |
| `sophon.backup.exclude` | Comma- or newline-separated `.kopiaignore` patterns applied to `sophon.backup.volumes` (e.g. `git/lfs/tmp/,cache/`). |
| `sophon.backup.paths` | Comma-separated selective paths inside attached named volumes (`/acme/acme.json` or `vol:sub/path`), supporting `?optional=true`. |
| `sophon.backup.ini` + optional `sophon.backup.ini.keys` | Captures only listed `section.key` entries from an INI file (`/data/gitea/conf/app.ini:security.SECRET_KEY,...`) and merges them into the fresh INI file on restore. Automatically excluded from any whole-volume copy of the same volume. |
| `sophon.backup.dump` | Shell command (executed via `sh -ec`) or JSON argv array (`["slapcat", ...]`) streaming a logical dump to stdout. |
| `sophon.backup.dump.name` | Artifact filename in snapshot (defaults by restore kind: `postgres-cluster.sql`, `database.dump`, `directory.ldif`, or `dump.out`). |
| `sophon.backup.dump.restore` | Automated `sophon-restore` profile: `postgres-cluster` (`psql`), `postgres-custom` (`pg_restore --clean --if-exists --no-owner`), or `ldap-subtree` (`slapadd`, paired with `sophon.backup.dump.base`). |
| `sophon.backup.dump.user` | Optional in-container user/UID for dump execution. |
| `sophon.backup.dumps.<key>.cmd` (or `.argv`, `.name`, `.restore`, `.user`, `.base`) | Multi-database support when one container exports multiple databases. |

## Dynamic volume discovery and undeclared volumes

Kopia mounts `/var/home/core/.local/share/containers/storage/volumes` read-only at
`/volumes:ro`. Whenever a container with `sophon.backup.enable=true` declares a
named volume or path, `backup.py` resolves `<volume>/_data` automatically—no
`SOPHON_KOPIA_EXTRA_VOLUMES` list or Kopia stack redeploy is required.

Every manifest/report lists **undeclared volumes** (attached named volumes not
covered by a `volumes` or `paths` declaration). They are not copied and do
not fail the run. Raw database (`*_db_data`, `pgdata`) and collector cache/staging
volumes intentionally appear here because native dumps protect database state instead
of copying live database files.

## Hot consistency, interruption and least privilege

Backups never stop or restart running containers:

* **Databases** (`postgres-cluster`, `postgres-custom`, `ldap-subtree`) stream
  transactional MVCC snapshots over stdout via `dump-proxy`. Every dump stream
  ends with an out-of-band exit-status trailer frame; non-zero exit codes,
  truncated streams or zero-byte stdout fail the action (`essential` mode) so
  Kopia aborts the snapshot.
* **Files & Git LFS** are cloned online from `/volumes:ro` with `cp --archive --reflink=always`
  (falling back to `--reflink=never`) into an isolated per-run directory under
  `/source/runs/<run_id>/`, pruned according to `sophon.backup.exclude`, and stamped
  with `.kopiaignore`. Stale `/source/runs/*` directories from an interrupted process
  are cleaned automatically at startup and before the next action.
* **Pending operator restores** write `/source/restore.json`: `backup.py` refuses
  to run while `restore.json` exists so Kopia can never snapshot half-restored state.

`kopia` runs as **UID 0 inside rootless Podman**, not host root, with all
capabilities dropped except:

* `DAC_OVERRIDE`: read/traverse other mapped sub-UIDs in `/volumes:ro` and preserve
  user xattrs during copying; `/volumes:ro` and the container rootfs stay read-only.
* `CHOWN`: retain source numeric UID/GID in staging.
* `FOWNER`: retain permissions/metadata after ownership changes.

Only the separate, zero-capability (`cap_drop: [ALL]`, `read_only: true`,
`no-new-privileges:true`) `dump-proxy` container mounts `/run/user/<uid>/podman/podman.sock`.
It sits on an internal-only network (`backup-engine`) and exposes only three routes:

* `GET /healthz`: engine reachability check
* `GET /v1/inventory`: container metadata with `Config.Env` and non-backup labels stripped
* `POST /v1/dump/<container_id>/<dump_name>`: re-inspects `<container_id>` on the Podman
  socket, verifies `sophon.backup.enable=true` and `<dump_name>` in that container's own
  labels, and executes **only** that label-declared command

Arbitrary `/exec`, `/stop`, `/start`, `/kill`, `/archive`, container creation/deletion,
and secret environment inspection are unconditionally rejected (`403`).

## Snapshot and recovery boundary

A snapshot contains real files, native stdout dumps and a v3 manifest with
image references/IDs, source-content Sophon version, declared paths, dump
checksums, optional absences, group failures and undeclared volumes. It contains
no inspect records, container environments, Arcane stacks or system copy.

The source-content version is generated into `bootstrap/sophon-version` when
building the deployment ISO. Direct development deployments must supply an
explicit `sophon_version`.

## Explicit group restoration

Configure **never** invokes restoration. First rerun Configure with the original
external inputs and matching database/LDAP engine images. Native database and
LDAP producers (`sophon.backup.dump` / `sophon.backup.dumps.*`) must match the
exact `image_id` recorded in the snapshot (automatic cross-major database
migration is not supported), while file-only application containers allow
restoring volume data across rebuilt app images. Stop any independently deployed
writers sharing the group's state and do not run Configure or other engine
mutations concurrently with recovery.

On the console, as the rootless Podman owner (`core`, never `sudo`), restore all
stateful groups interactively with a single command (or pass `--group gitea` /
`--snapshot SNAPSHOT_ID` / `--yes`):

```sh
sophon-restore
```

Low-level phase commands also remain available for step-by-step inspection:

```sh
sophon-restore plan --group gitea --snapshot latest
sophon-restore apply --group gitea --snapshot SNAPSHOT_ID --approve
sophon-restore status
sophon-restore cleanup --approve
```

Replace `SNAPSHOT_ID` with the exact ID printed by `plan`. A plan downloads real
files and verifies the group's membership, database/LDAP producer image IDs,
declarations, `manifest.json` SHA-256 and dump checksums without modifying
workloads. `latest` means the latest snapshot of the configured stable source,
not a silent fallback to an older successful group. A failed/missing group is
refused; inspect reports and choose another explicit ID. Plans bind to the fresh
container IDs and volume mounts; changing targets or staging requires a new plan,
not an override.

Approval authorizes destructive replacement of **only the declared paths** and
native database import. Generated INI identity fields merge into fresh settings;
the snapshot never replaces externally configured database/server settings.
Optional paths absent from the source remove corresponding fresh paths. Numeric
ownership, permissions and symlinks are retained; symlink destination parents are
refused. Producers start before consumers. Members originally stopped stay stopped.

Native exports are **trusted executable input**: PostgreSQL dumps execute SQL as
a temporary database superuser. Only restore from your authenticated repository.
`postgres-cluster` (`psql`) and `postgres-custom` (`pg_restore --clean --if-exists --no-owner`)
import with the matching PostgreSQL image and an isolated Unix socket, not a live
data-directory copy. PostgreSQL's non-droppable initial superuser is renamed to an
owner-specific `sophon_system_*` role and disabled (`NOLOGIN`, password cleared);
temporary login/database resources are removed. During `apply`, `sophon-restore`
also stops and restarts any running containers in the same Compose project (`peers`)
so web/app containers do not hold open database connections while `pg_restore` or
`psql` replaces the database. `ldap-subtree` requires `sophon.backup.dump.base`
to equal the configured MDB suffix (the dc-style site DN). It reconstructs MDB
from that site's export using `slapadd` and validates against freshly configured
schema. Invalid vendor seed entries outside that naming context are not reimported.
Partial-suffix merges are refused; custom schemas/ACLs belong in IaC.

The one-shot coordinator uses Podman alone: no Arcane, Gitea, Kopia UI or
running backup stack is required. It mounts `/volumes` directly to restore
named-volume files in-process and temporarily mounts the **full rootless
engine socket** to stop/restart target groups and create/remove no-network
native PostgreSQL/OpenLDAP import helpers. This is an explicit operator
privilege, never an expansion of the primary proxy allowlist. Native database
helpers mount only planned named volumes; PostgreSQL additionally needs
`SETUID`/`SETGID` for its vendor entrypoint to drop to the database OS user.
No helper is privileged or host root.

Planning also holds the backup coordination lock, so a scheduled backup cannot
race with recovery planning. Approval persists a `/source/restore.json` hold
before stopping any target container; it survives coordinator death and clears
only on successful restoration or explicit abandonment.

The default private state volume is `sophon-restore-v3`; use
`--state-volume NAME` for separate group plans. It contains decrypted staging and
a durable helper journal. **On failure/interruption the group stays stopped.**
Inspect `sophon-restore status` and the named helper's logs. Correct the external
cause, then retry the same state/group/exact snapshot with `--approve`. A retry
settles owned helpers before repeating replacement/import; it never guesses
replacement containers by name. `cleanup --approve` removes owned helpers,
staging and the journal but **does not resume workloads**. Run it after success
or deliberate abandonment, including interrupted planning. Cleanup of a failed
restore releases the backup hold but does not validate partial state: disable
scheduling before abandonment until you have rebuilt or validated that state.
Then remove the empty
state volume if desired:

```sh
podman volume rm sophon-restore-v3
```

If `kopia` exists, the launcher reads its current connection settings without
printing/persisting its environment. Otherwise export the Configure
`SOPHON_KOPIA_PASSWORD`, SFTP key/pin/destination and stable client identity
variables, plus `SOPHON_RESTORE_SOURCE_VOLUME` (normally `kopia_source-v3`).
`SOPHON_KOPIA_RESTORE_IMAGE` optionally selects an already loaded custom recovery
image; the launcher resolves it to an immutable local ID and never pulls.
Retain the backup coordination volume (`kopia_source-v3`) even when removing the
UI/backup stack, as it serializes backup and recovery via `restore.json`.
