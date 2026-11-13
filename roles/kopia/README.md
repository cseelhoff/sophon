# Kopia v3: operator guide

Sophon backs up application state, not the system. Rebuild by rerunning Configure,
then explicitly restore state. Images, binaries, caches and Configure-generated
configuration are not primary backups. **Nothing may exist only in Portainer:**
keep every stack definition in IaC and externally supplied secrets in a password
manager. The backup never requests Portainer's backup, stacks, environments or
an administrator API key.

## Deploy and operate

Prepare carries the custom Kopia 0.23.1 image and pinned HAProxy 3.2.6-alpine
socket-proxy image in the offline bundle. Configure deploys Kopia last, after
the source volumes exist. The backup runs in a normal stack; no backup host
service or timer is installed.

On an existing managed InfraVM, host bootstrap refreshes immutable Portainer
labels by recreating only its container with the same image ID and
`portainer_data` volume. It never deletes that volume or upgrades the image
as part of this metadata refresh; an unexpected data mount is refused.

Supply the SFTP destination, key/pins, repository password and UI credentials
through [Configure inputs](../../docs/inputs.md#kopia-backup-inputs). M1 expects
an **already initialized** SFTP repository. Startup only connects; there is no
create-on-error fallback. Interactive pairing/confirmed creation is the separate
M3 milestone, not an implemented M1 feature.

Run and inspect backups from the InfraVM console as `core`:

```sh
podman exec kopia python3 /app/backup.py run
podman exec kopia cat /source/last-run.json
podman logs kopia
```

Exit 0 means every group completed and uploaded. Exit 1 means a partial run or
another failure. A partial run **uploads successful groups**, but its manifest,
stderr and final report explicitly identify failed groups. A missing input mount
reports the volume name and asks you to add it to
`SOPHON_KOPIA_EXTRA_VOLUMES` and redeploy. An upload failure retains immutable
staging for diagnosis. Never treat a healthy UI/repository connection as proof
that the last backup was complete.

After verifying a first manual backup, set `SOPHON_KOPIA_BACKUPS_ENABLED=true`
and rerun Configure. The container supervisor runs backups at
`SOPHON_KOPIA_SNAPSHOT_INTERVAL` (default `6h`); positive integer `s`, `m`, `h`
or `d` intervals are accepted. It logs nonzero runs and keeps the UI server
independent. Native Kopia scheduling is disabled: UI “Snapshot now” on the
logical source is not the collector. Use the command above.

Snapshots use stable identity `backup@sophon:/sophon-state` by default, even
though each run has a unique immutable staging directory. Retention remains
latest 10, hourly 24, daily 14, weekly 8, monthly 12 and annual 3. Quick/full
maintenance intervals are 24/168 hours.

## What the core declares

| Role | Irreproducible state | Deliberately left out |
|---|---|---|
| Traefik | `acme.json`: generated ACME identity and certificate/private-key state. Traefik stops briefly for the clone. | Dynamic/file-provider configuration rendered by Configure; images and logs. |
| OpenLDAP | Native `slapcat` of the configured site's directory subtree, including user entries, password hashes and entry identity. MDB remains online for its native consistent export. | Live MDB files, vendor/Configure-created `cn=config`, legacy migration guard and certificates derived from ACME. Custom schemas/ACLs must be represented in site IaC. |
| Keycloak | Native `pg_dumpall` with Keycloak stopped while PostgreSQL remains running. It preserves user-created realms/accounts/clients and their identity/credential state as a coherent native database export. | Raw PostgreSQL files and Keycloak's transient runtime directory. |
| Gitea | Native PostgreSQL export; repositories, LFS, attachments, avatars, packages, generated JWT and SSH identity keys. From `app.ini`, only declared generated identity keys are copied to `identity.ini`. Gitea stops while cloning/exporting. | Database directory, indexes, logs, sessions, uploads-in-progress, temporary worktrees, regenerated SSH configuration and externally supplied configuration/passwords in `app.ini`. |
| CoreDNS | None: DNS records/configuration are reconstructed from IaC, labels and retained external Cloudflare inputs. | Binary, cache and transient inventory. |
| cloudflared | None: Configure reconnects the named tunnel using externally retained Cloudflare access. | Connector/token environment and image. No duplicate production connector is needed for restore. |
| Portainer | None: Configure recreates the management plane and its declarative stacks. | Its database, native backup, stack/environment capture and collector token. UI-only configuration is unsupported. |
| Kopia/proxy | None: repository connection, policy and proxy config are reconstructed from deployment inputs. | Config/cache/log volumes, staging, recovery journal and the repository itself. Keep repository decryption credentials outside it. |

Full native database exports necessarily contain related roles/schema and
configured seed rows as well as irreproducible application state; these are
coherent database recovery inputs, never a copy of the running database files.
Application-generated identity values remain encrypted by Kopia.

## v3 declarations

Only flat v3 split labels are accepted. Legacy v1/v2 recipes, archives, native
Portainer backups and version-1 manifests are not read or upgraded.

```yaml
labels:
  sophon.backup.version: "3"
  sophon.backup.group: application
  sophon.backup.stop: "true"
  sophon.backup.paths.0.name: files
  sophon.backup.paths.0.volume: site_application_data
  sophon.backup.paths.0.path: user-files
```

`paths.N.path` is a normalized relative path within that named volume, not an
engine archive path or host bind. The volume must be attached to the declaring
workload and mounted read-only into Kopia. `optional=true` is only for explicitly
declared lazily-created paths; missing required paths fail the group. Optional
absences are recorded in the manifest, not silently converted to data.

The optional `paths.N.ini` is a JSON array of explicit `section.key` selectors.
Only those values survive in the staged INI file. Gitea uses this to retain
generated identity without backing up its reconstructible database/server
configuration. Restore must merge these identity fields into freshly configured
settings, not replace the whole configuration.

A database declares stdout exports, never data-directory paths:

```yaml
labels:
  sophon.backup.version: "3"
  sophon.backup.group: application
  sophon.backup.stop: "false"
  sophon.backup.dumps.0.name: database.sql
  sophon.backup.dumps.0.restore: postgres-cluster
  sophon.backup.dumps.0.argv: '["sh","-ec","exec pg_dumpall -U \"$POSTGRES_USER\" --clean --if-exists"]'
```

Compose source must double `$` to `$$` to preserve the variable for in-container
execution. Commands are JSON argv arrays, not implicit shell strings; `user`
is optional. Export producers must stay running and provide `sh` and `setsid`
for bounded, recoverable process-group execution. Writers sharing a filesystem
volume must all belong to the same group with `stop=true`; otherwise the group
fails instead of using an unsafe live plain copy.

Indexes must be contiguous. Path/dump names must be unique within a member.
Unknown labels and unsafe names/paths fail their group. Stateless declarations
have a version, group and `stop=false` but no paths/dumps.

## Source mounts and undeclared volumes

Each core role declares its read-only backup-volume list in its defaults. The
Kopia role combines those lists with the site's JSON array:

```sh
export SOPHON_KOPIA_EXTRA_VOLUMES='["site_application_data"]'
```

Keep that list in site IaC, not only in a UI. Redeploy after changing it: labels
cannot add mounts to a running collector. This interface accepts volume names
only, never arbitrary host paths. See [outside-workload migration](../../docs/kopia-v3-migration.md).

Every manifest/report lists **undeclared volumes**. They are not copied and do
not fail the run. Raw database and collector cache/staging volumes intentionally
appear here; native exports protect the database state, not those raw files.
Review the list for real omissions after each workload change.

## Consistency, interruption and permissions

For each group, validate declarations, journal stop intent, stop writers,
clone paths with `cp --archive --reflink=always`, and stream native dumps into
staging. A plain copy is attempted only for recognized unsupported-reflink or
cross-filesystem errors, while all volume writers are stopped. Permission and
I/O errors are not mistaken for lack of reflink support. Writers resume before
upload; their pause does not depend on SFTP/upload speed.

The durable, fsynced v3 journal records exact container/image/mount identities
and pending export process groups. Startup recovers it before serving the UI:
cancel interrupted exports, then resume only writers that this run stopped.
A lost stop response is settled before its compensating start. Replaced
containers are never guessed by name. Manual retry is:

```sh
podman exec kopia python3 /app/backup.py recover
```

Do not delete an unresolved journal to make a run appear successful. Full
failed-run reports live outside immutable snapshot staging.

Kopia runs as **UID 0 inside rootless Podman**, not host root, with all
capabilities dropped except:

* `DAC_OVERRIDE`: read/traverse other mapped sub-UIDs and preserve user xattrs
  during copying; source mounts and the container rootfs stay read-only.
* `CHOWN`: retain source numeric UID/GID in staging.
* `FOWNER`: retain permissions/metadata after ownership changes.

The [M0 measurements](../../docs/kopia-m0.md) include negative controls for each
capability. No `SYS_ADMIN`, privileged container or whole-volume-directory
mount is used in the primary backup. `label=disable` permits these shared
rootless volumes without relabeling live application data; `no-new-privileges`
prevents gaining further privilege through executable files.

Only the separate, cap-free socket proxy mounts the engine socket. It is
unpublished on an internal network and allows list/inspect/exec/stop/start
routes only. It denies archive transfer, create/delete, image and volume APIs.
Permitted exec remains powerful; this is a reduced engine surface, not a claim
that arbitrary allowed commands are harmless.

## Snapshot and recovery boundary

A snapshot contains real files, native stdout dumps and a v3 manifest with
image references/IDs, source-content Sophon version, declared paths, dump
checksums, optional absences, group failures and undeclared volumes. It contains
no inspect records, container environments, Portainer stacks or system copy.

The source-content version is generated into `bootstrap/sophon-version` when
building the deployment ISO. Direct development deployments must supply an
explicit `sophon_version`.

## Explicit group restoration

Configure **never** invokes restoration. First rerun Configure with the original
external inputs and the **same workload image IDs** as the snapshot. Automatic
cross-version database/application migration is not supported. Stop any
independently deployed writers sharing the group's state and do not run
Configure or other engine mutations concurrently with recovery.

On the console, as the rootless Podman owner (`core`, never `sudo`):

```sh
sophon-restore plan --group gitea --snapshot latest
sophon-restore apply --group gitea --snapshot SNAPSHOT_ID --approve
sophon-restore status
sophon-restore cleanup --approve
```

Replace `SNAPSHOT_ID` with the exact ID printed by `plan`. A plan downloads real
files and verifies the group's membership, image IDs, declarations and export
checksums without modifying workloads. `latest` means the latest snapshot of the
configured stable source, not a silent fallback to an older successful group.
A failed/missing group is refused; inspect reports and choose another explicit ID.
Plans bind to the fresh container IDs/mounts and hash staged contents/metadata.
Changing targets or staging requires a new plan, not an override.

Approval authorizes destructive replacement of **only the declared paths** and
native database import. Generated INI identity fields merge into fresh settings;
the snapshot never replaces externally configured database/server settings.
Optional paths absent from the source remove corresponding fresh paths. Numeric
ownership, permissions and symlinks are retained; symlink destination parents are
refused. Producers start before consumers. Members originally stopped stay stopped.

Native exports are **trusted executable input**: PostgreSQL dumps execute SQL as
a temporary database superuser. Only restore from your authenticated repository.
`postgres-cluster` imports with the matching PostgreSQL image and an isolated
Unix socket, not a live data-directory copy. PostgreSQL's non-droppable initial
superuser is renamed to an owner-specific `sophon_system_*` role and disabled
(`NOLOGIN`, password cleared); temporary login/database resources are removed.
`ldap-subtree` requires `dumps.N.base` to equal the configured MDB suffix
(the dc-style site DN). It reconstructs MDB from that site's export using
`slapadd` and validates against freshly configured schema. Invalid vendor seed
entries outside that naming context are not reimported. Partial-suffix merges
are refused; custom schemas/ACLs belong in IaC.

The one-shot coordinator uses Podman alone: no Portainer, Gitea, Kopia UI or
running backup stack is required. It temporarily mounts the **full rootless
engine socket** to create/remove no-network native/file helpers. This is an
explicit operator privilege, never an expansion of the primary proxy allowlist.
Helpers mount only planned named volumes. Files use the primary three
capabilities; PostgreSQL additionally needs `SETUID`/`SETGID` for its vendor
entrypoint to drop to the database OS user. No helper is privileged or host root.

The default private state volume is `sophon-restore-v3`; use
`--state-volume NAME` for separate group plans. It contains decrypted staging and
a durable helper journal. **On failure/interruption the group stays stopped.**
Inspect `sophon-restore status` and the named helper's logs. Correct the external
cause, then retry the same state/group/exact snapshot with `--approve`. A retry
settles owned helpers before repeating replacement/import; it never guesses
replacement containers by name. `cleanup --approve` removes owned helpers,
staging and the journal but **does not resume workloads**. Run it after success
or deliberate abandonment, including interrupted planning, then remove the empty
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
Retain the backup coordination volume even when removing the UI/backup stack.
It serializes backup/recovery; pending backup stop journals must be recovered
before restoring. Run `backup.py recover` with the existing backup configuration
if needed, rather than deleting a pending journal.

The disposable [M2 drill](../../tests/spikes/kopia_m2.py) rebuilds application
containers/volumes from rendered repository templates and compares native
Keycloak realm exports, LDAP entries, Gitea commits/SSH identity and certificate
state. Its dev-only profile disables tunnels, runners, ACME and external OIDC,
uses synthetic certificates, and deliberately injects a post-import start failure
to exercise stopped-group retry. It is not a production rebuild command.

Old snapshots remain usable only with the locally tagged `kopia-legacy` tool.
[Historical evidence](../../docs/kopia-legacy-evidence.md) describes that old
system, not v3 recovery coverage.
