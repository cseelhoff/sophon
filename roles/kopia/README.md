# Kopia

Encrypted Sophon application backups to a configurable external SFTP repository.
UI: `https://kopia.<domain>`. This role is part of the core InfraVM deployment;
it carries the tested collector, runtime and isolated recovery tool in Sophon.

## Deployment

Prepare builds the pinned custom image from [files](files), saves it when the
image ID changes, and includes its archive and this role in the deployment ISO.
Normal Configure deploys Kopia after Traefik and before Gitea; it waits for the
authenticated repository-connected health check. No Gitea project/runner,
runtime download or special recovery mode is involved. After a fresh install,
run the documented recovery operations from the installed Kopia container.
Automatic production restore/cutover is not part of deployment.

Supply the `SOPHON_KOPIA_*` [Configure inputs](../../docs/inputs.md#kopia-backup-inputs)
on the InfraVM console, never in Git. The role creates a separately revocable
collector API key once and reuses it from the `kopia` stack on reruns. It keeps
unrelated existing stack environment entries and the original five volume names.
Supply the same existing repository settings, identity and scheduling policy.
The former standalone Gitea project is not used by this role; the dated evidence
below records tests that preceded core integration, not a current dependency.

The container's runtime environment is managed as follows:

| Variable | Purpose |
|---|---|
| `PORTAINER_URL`, `PORTAINER_ENDPOINT_ID` | Derived core HTTPS management endpoint and local endpoint ID |
| `BACKUP_PORTAINER_API_KEY` | Role-managed collector access to containers and native Portainer backup |
| `KOPIA_PASSWORD` | Repository encryption password, also required for disaster recovery |
| `KOPIA_SERVER_USERNAME`, `KOPIA_SERVER_PASSWORD` | UI authentication |
| `KOPIA_SFTP_HOST`, `KOPIA_SFTP_PORT` | Configurable SFTP destination |
| `KOPIA_SFTP_USERNAME`, `KOPIA_SFTP_PATH` | Confined account and absolute repository path |
| `KOPIA_SFTP_PRIVATE_KEY_B64` | Single-line base64 of the SSH private key |
| `KOPIA_SFTP_KNOWN_HOSTS_B64` | Single-line base64 of verified, pinned SSH host keys |
| `KOPIA_CLIENT_USERNAME`, `KOPIA_CLIENT_HOSTNAME` | Stable source identity, `backup@sophon` |
| `KOPIA_BACKUPS_ENABLED` | `true` only after the first verified full backup |
| `KOPIA_SNAPSHOT_INTERVAL` | Default `6h` |
| `KOPIA_INIT_REPOSITORY` | Normally `false`; explicitly `true` only to create an empty repository once |

Base64 is **encoding, not encryption**. It prevents Portainer's dotenv writer
from corrupting multiline SSH material. Decode into private, mode-0600 tmpfs
files only. Keep the repository password and a recovery copy of the SSH key
outside the repository; a backup cannot bootstrap its own decryption.

Startup always connects to the supplied destination. Wrong passwords, wrong
host keys and unavailable destinations fail closed; there is no create-on-error
fallback. After creation set `KOPIA_INIT_REPOSITORY=false` immediately.
Changing the destination switches the connection; it does not migrate snapshots.
Internal HTTP is confined to the proxy network; Traefik and Cloudflared provide
external HTTPS. UI CSRF protection remains enabled.

## Versioned Sophon split-label contract

There is **no universal Docker backup/restore label standard**, and Kopia does
not discover Docker labels. This collector implements the explicit Sophon v1
and v2 contracts on containers. New Compose definitions use **v2**, with one
label per setting/path/command rather than a whole-recipe JSON label:

```yaml
labels:
  sophon.backup.version: "2"
  sophon.backup.group: "keycloak"
  sophon.backup.stop-during-backup: "false"
  sophon.backup.stop-timeout: "120"
  sophon.backup.covers.0.path: "/var/lib/postgresql"
  sophon.backup.paths.0.type: "archive"
  sophon.backup.paths.0.path: "$${SOPHON_BACKUP_DIR}.sql"
  sophon.backup.paths.0.name: "postgres-cluster"
  sophon.backup.archive-pre.0.argv: >-
    ["sh","-ec","umask 077; pg_dumpall -U \"$$POSTGRES_USER\" --clean --if-exists -f \"$$SOPHON_BACKUP_DIR.sql\""]
  sophon.backup.archive-post.0.argv: >-
    ["sh","-ec","rm -f -- \"$$SOPHON_BACKUP_DIR.sql\""]
```

| Labels below `sophon.backup.` | Meaning |
|---|---|
| `version`, `group`, `stop-during-backup`, `stop-timeout` | Required scalars; version `2`, group, lowercase `true`/`false`, positive integer seconds |
| `covers.N.path` | Persistent mount destinations protected by the recipe |
| `excludes.N.path`, `excludes.N.reason` | Excluded mount destination and nonempty reconstructible/duplicate reason |
| `paths.N.type`, `paths.N.name`, `paths.N.path` | Artifact type/name/path; type `portainer-backup` has no path |
| `prepare.N.argv`, optional `.user` | Preparation hooks **before** group members are paused |
| `archive-pre.N.argv`, optional `.user` | Export commands **after** selected group members are paused, before capture |
| `archive-post.N.argv`, optional `.user` | Cleanup/recovery hooks **after** originally running members are restarted |
| `restore.N.argv`, optional `.user` | Recorded commands for manual isolated recovery, never automatic production restore |

Every collection has independent, contiguous indexes starting at zero; omitted
collections are empty. Label-map order does not determine command order.
Each `argv` value is a JSON array of exact argument strings, not an implicitly
shell-interpreted command. Use an explicit `["sh","-ec","..."]` when a shell is
needed. Unknown fields, missing fields, malformed arrays and index gaps fail.
These names borrow established split-label/lifecycle vocabulary, **not** Offen
compatibility: our grouped ordering and durable recovery remain Sophon-specific.

`covers` names persistent mount destinations. `excludes` supplies
nonempty reconstructible/duplicate reasons. Shared volumes are covered by
immutable volume name, including volumes on exited initialization containers.
An uncovered named or anonymous volume fails the whole backup before mutations.
Bind mounts are not automatically covered by that volume check; protect
secret-bearing host paths with explicit archive entries.

Use `$${SOPHON_BACKUP_DIR}` in Compose labels so Compose preserves the collector's
fresh per-run placeholder. Shell environment references also require `$$`.
Keep passwords out of labels; commands can use the container's existing secrets
and environment.

For each group: prepare hooks, stop marked members, archive-pre export commands,
stream archives, restart originally running members, post-hooks.
`stop-during-backup=true` cannot have archive-pre commands.
Prepare hooks establish archive-post cleanup
intent before running. Restores are recorded recipes, **never automatic actions
against production**. A durable journal recovers interrupted stops/hooks before
the next discovery. Timed-out commands are cancelled by their exact process
group before writers resume. Forced kills reject consistency claims.

### Compatibility

Version `1` with its original `sophon.backup.spec` JSON remains readable for
rolling upgrades and older snapshots. Do not put flat fields on a v1 container
or retain `sophon.backup.spec` on a v2 container: mixed definitions fail rather
than silently choosing a recipe. Deploy the compatible collector before
recreating containers with v2 labels. Container labels are immutable: editing
Compose alone does not change existing containers.
Snapshot manifests and recovery journals retain their existing version 1
formats; this migration changes only the container-label representation.

Each snapshot contains a fresh manifest, hashes, native archives, complete
container configuration and private Portainer stack definitions/environment.
The read-only `SOPHON_PROJECT_PATH` bind (default `/var/lib/sophon/project`)
also protects the installed VM's `site.json` network/recovery settings.
Application and deployment secrets are in the encrypted container/stack
metadata, not in that small network-settings file.
Successful staging is removed by Kopia's after-root action; fully recovered
failed staging is removed without touching earlier snapshots or unresolved
recovery state. Failed or empty preparations never become successful snapshots.

### Current strategies

* Gitea: briefly stop its writers; export PostgreSQL roles/databases while the
  database stays running; archive repositories, LFS and configuration together.
* Keycloak: live PostgreSQL cluster export; its empty runtime data is derived.
* LDAP: native MDB/config LDIF exports plus the legacy guard volume. Do not
  change LDAP schemas/configuration during a backup. TLS files are regenerated
  from protected Traefik ACME state.
  The active-subtree export is directly restorable. A separate complete MDB
  LDIF retains the preexisting foreign `dc=nodomain` entry, which cannot be
  imported into the configured Sophon naming context. Retain it separately;
  do not silently discard it or use continue-on-error to hide restore failures.
* Rocket.Chat: live full MongoDB export with users, GridFS and oplog, plus
  replica key/config. The empty filesystem-upload volume is excluded because
  GridFS is enforced. Revisit the contract before changing upload storage.
* Nextcloud: briefly stop Apache and cron, without changing maintenance mode;
  live PostgreSQL export plus files, config, custom apps and themes.
  Image-provided application code is reconstructed from the recorded pinned image.
  Cron uses an init process and SIGTERM instead of the Apache image's SIGWINCH.
  Its hook removes the schedule, allows a complete reload interval and drains
  active PHP jobs before stopping; the recovery hook reinstates the schedule.
* Arcane/n8n: graceful short stop and full SQLite/config/key/file archives.
* Appsmith: graceful stop for a jointly consistent MongoDB, PostgreSQL 14,
  Git and configuration archive. Its native backup alone omits PostgreSQL.
* Nexus: graceful stop for jointly consistent H2, configuration and blob data.
* Homepage: configuration archive. Runners: immutable `.runner` registration;
  workspaces, lock files and caches are reconstructed.
* Traefik: ACME and dynamic configuration without stopping the proxy.
  Portainer: its native in-process backup, never a live database-file copy.
* AdGuard Home: DNS remains running. A no-network helper with read-only source
  volumes creates XFS COW exports using `cp --archive --reflink=always`; it never
  falls back to an ordinary live copy. Each file clone is atomic, not the
  multi-file tree: configuration, statistics and query logs may span different
  instants. This is not a transactionally consistent whole-application snapshot.
  Native inner tar files preserve the real UID/GID because Podman's archive API
  can replace them with zero. Unpack those inner tars into `/opt/adguardhome`;
  their roots are `conf` and `work`, not the producer's mount aliases.
* x870 controller: separately snapshot SFTP service/bootstrap/client and host
  keys, controller ACME state and diagnostic SSH/unit recovery material.
  Exclude the repository data itself and the SFTPGo user DB rebuilt from bootstrap.
* Redis, derived TLS outputs, one-shot Mongo client volumes and Kopia's own
  staging/cache/logs/restore workspace have explicit exclusions.
  Stateless services still appear in encrypted deployment metadata.

Pauses last only through collection of that group's artifacts; encryption/upload
happens after writers resume. They are deliberately preferred over pretending
an inconsistent raw live database copy is a backup.

## Recovery

1. Keep production untouched. Connect a separate Kopia client using the recorded
   SFTP settings, pinned host key, SSH key and repository password.
2. Restore a snapshot to a **new isolated directory**:
   `kopia snapshot restore <snapshot-id> /restore/new-snapshot`.
3. Validate every artifact and extract safely:
   `python3 backup.py verify /restore/new-snapshot --output /restore/new-artifacts`.
   This checks hashes/manifest/recipes; it does not execute database restores.
4. Restore into new networks and volumes using the recorded exact image versions.
   Use original tar archives with Docker's archive API or an appropriate tar
   utility to preserve UID/GID/modes. The verifier's extraction is for inspection,
   not a promise of ownership preservation.
   Check ownership against the recorded service user: Podman's native archive
   headers can report UID/GID zero. Reconstruct service ownership before startup;
   the tested Appsmith/Nexus/n8n entrypoints also initialize their data permissions.
   Native stopped-mount archives are normalized to the same basename-rooted
   layout as running Docker archives; archive `/appsmith-stacks` into its parent,
   rather than inadvertently nesting `appsmith-stacks` twice.
5. PostgreSQL: initialize an isolated cluster with superuser and database
   `sophon_restore` (neither exists in the exported cluster), then execute the
   recorded SQL via that database with `ON_ERROR_STOP=1`. A full PostgreSQL 18
   export drops/recreates `template1` and `postgres`; do not start the restore
   connected to either. This restores databases, roles, owners and credentials.
6. MongoDB: full user/oplog restoration requires an unpublished, no-egress
   bootstrap target with authorization temporarily disabled. Initialize the
   matching replica set and wait for PRIMARY **before** creating a temporary
   root account matching the recorded import credentials. Run the recorded
   full archive import with `--oplogReplay`. An authenticated-only import failed
   after restoring users invalidated its session; do not accept that partial
   import. Correct DB/config ownership to `999:999`, then restart with the
   original authenticated entrypoint, replica key and environment. Verify
   authentication is enforced and original application credentials work.
   Restore the original network/service aliases, including `mongodb` and
   `nats`; preserving just container names is insufficient.
7. LDAP: import config/MDB into empty offline directories, create
   `/var/run/slapd`, run `slaptest`, and correct ownership to `100:101`.
   Only after successful validation restore
   `/etc/ldap/slapd.d/initialized` with content `2.6` for the pinned image;
   otherwise normal startup repeats schema initialization and fails.
   Recover TLS material at the exact recorded `LDAP_TLS_CERT_FILE`,
   `LDAP_TLS_KEY_FILE` and `LDAP_TLS_CA_FILE` paths. Verify normal startup,
   strict LDAPS trust and the original root bind. The image lacks Debian's
   `/etc/ssl/certs/ca-certificates.crt`; provide an actual client CA bundle
   rather than disabling certificate verification. Keep the separate complete
   LDIF containing the foreign `dc=nodomain` root.
8. Filesystem archives: restore stopped services' data into new volumes. Recover
   their original encryption keys/secrets from the encrypted manifest before
   starting matching Appsmith, n8n, Arcane, Nexus or Nextcloud containers.
9. Portainer's native archive is restored only into an uninitialized isolated
   Portainer instance, not by overwriting a running production database.
   Version 3 requires its startup setup token in `X-Setup-Token`. Restart the
   isolated instance after restore before testing the recovered administrator.
10. Validate real application content and authentication before any cutover.

These are manual recovery procedures unless the workload explicitly has a
supported `sophon.restore.*` automation profile (Keycloak/PostgreSQL below).
The `sophon.backup.restore.<index>.argv` labels record native import
commands; they do **not** encode provisioning, helper-vs-application execution
context, temporary authentication modes, TLS setup or startup validation.
An empty command list for a filesystem workload does not mean it has no data.
Do not execute import commands against production.

### Filesystem and native-archive prerequisites

- Nextcloud: seed the new HTML volume from the **same pinned image** using
  `cp -a /usr/src/nextcloud/. /var/www/html/`, then overlay its protected
  config/data/custom-app/theme archives. Apply
  `chown -R 33:33 /var/www/html`, including reconstructed code and `.htaccess`.
  Without the image code, the entrypoint mistakes the recovered installation
  for a new one; without full ownership, its `.htaccess` update fails.
  Restore PostgreSQL and provider/encryption settings before Apache startup.
  Rebuild Redis rather than restoring its excluded disposable data.
- Use a separate volume helper when the application lacks extraction tools.
  Arcane's deployed image is distroless and cannot run `sh`/`tar`. Mount only
  fresh restoration volumes in the helper; never the production socket.
  Restore Gitea and n8n ownership to `1000:1000`, Nexus to `200:200`;
  Appsmith's pinned native entrypoint handles its bundled-service ownership.
  Stop helpers before starting applications with recovered secrets.
- Portainer: pass its original normalized native archive to the fresh
  instance's restore API using `X-Setup-Token`, then restart. The archive is
  unencrypted inside the encrypted Kopia snapshot, so the native restore
  password is empty. Never attach the production Docker socket in a lab.
- AdGuard: validate and unpack the native **inner** `conf`/`work` tar files
  into `/opt/adguardhome` with ownership `65534:65534`; the producer aliases
  are not restoration destinations. Remap clone-only HTTP/DNS bindings when
  its interface differs. Preserve/check `allowed_clients`: the source allows
  `10.89.0.0/24`, not every `10.89.*` subnet. A dropped lab-client DNS packet
  is not necessarily a failed restore. Local loopback DNS was tested.
- Gitea runners: recover `/data/.runner` byte-for-byte and check its identity
  structure while the normal entrypoint is disabled. Do not start duplicate
  registered identities or execute deployment jobs during recovery tests.
- Reconstruct stateless services from encrypted deployment metadata.
  Do not start duplicate Cloudflare tunnels, production-facing TLS automation
  or controller jobs just to demonstrate that their images start.

### Automated Keycloak recovery (profile version 1)

`restore.py` is packaged in the Kopia image and published in this repository.
It reads instructions from the **restored snapshot**, not current live labels
or container-name guesses. The separate `sophon.restore.*` namespace leaves
strict v1/v2 backup-label decoding and existing native import recipes unchanged.
Only one PostgreSQL/Keycloak group is supported in this first slice; other
workloads and old snapshots without these profiles fail explicitly rather than
being guessed or upgraded in place.

The Keycloak service declares:

```yaml
labels:
  - sophon.restore.version=1
  - sophon.restore.kind=keycloak
  - sophon.restore.depends-on=keycloak-db
  - sophon.restore.verify-realm=sophon
```

Its database declares:

```yaml
labels:
  - sophon.restore.version=1
  - sophon.restore.kind=postgres-cluster
  - sophon.restore.artifact=postgres-cluster
```

Both still require their existing backup group, coverage/exclusions, native
archive and import-command labels. `depends-on` names the source member
explicitly. `kind` is a versioned recovery recipe, not an image/name heuristic.
The read-only plan spells out dependency order, phases and execution contexts.

After independently restoring a snapshot with actions disabled:

```sh
python3 /app/restore.py plan /restore/snapshot --group keycloak
python3 /app/restore.py run /restore/snapshot --group keycloak \
  --state /restore/keycloak-test --trust-snapshot
python3 /app/restore.py cleanup --state /restore/keycloak-test
```

Execution/cleanup require HTTPS `PORTAINER_URL`, `PORTAINER_API_KEY` and optional
`PORTAINER_ENDPOINT_ID` (default 1). The separate recovery client needs these
explicitly; they are not taken from the snapshot. No Docker socket, SSH or new
dependency is needed. The source immutable image IDs must already exist on the
selected engine; the tool never pulls an approximate tag. Snapshot SQL and
commands are executable trusted recovery material. Hash checks detect corruption,
not maliciously rewritten instructions: inspect the plan and use
`--trust-snapshot` only for a trusted backup. Do not copy secrets into Git.

The executor creates random, owner-labelled resources on an internal network,
preserves required database service addressing, bootstraps PostgreSQL, uploads
the verified native SQL archive and executes its recorded import command. It
then removes the temporary bootstrap database, restarts PostgreSQL with the
original environment, starts Keycloak and requires native database content,
original master-administrator login and the declared recovered realm.
Bind mounts, Docker sockets, published ports, source-volume reuse, unsupported
profiles and inconsistent dependencies are refused. Disposable volumes have
valid backup exclusions, so live backup coverage remains strict during tests.
There is **no** cutover or production-replacement mode.

PostgreSQL's initial OID-10 role cannot be dropped or lose `SUPERUSER`. The tool
clears its password, disables login/other role attributes and renames it to a
run-specific `sophon_recovery_system_*` name, leaving `sophon_restore` available
for a future restore. Required verification confirms it has no login/password.
The mandatory system-superuser attribute is reported honestly, not claimed
removed. Restored source administrator accounts remain unchanged.

State is mode-600, atomic/fsynced, bound to the exact snapshot/target and locked
against concurrent operations. Run the same command/state to resume. Completed
stages are not blindly repeated; an interrupted SQL import rebuilds only this
run's disposable containers/volumes before reimporting, never a partial database.
A verified run rechecks authentication/data while retaining resource IDs.
Errors return nonzero; failed command output goes to a private diagnostic file,
not terminal logs. Cleanup checks **all** existing resource owners/IDs/mounts
before deletion and never deletes snapshot data. Remove the decrypted client
storage and private diagnostics separately after successful cleanup.

Retention: latest 10, hourly 24, daily 14, weekly 8, monthly 12, annual 3.
Quick maintenance every 24h; full maintenance every 168h. Owner `backup@sophon`.

The current SFTP destination is the x870 workstation, not an independent
offsite machine. Keep it powered on and preserve its rootless SFTPGo systemd
service, SSH host key, repository data and NixOS firewall rule. Moving data
off the Proxmox host protects against that host's loss, **not** loss of x870 or
the whole site. Monitor both staging capacity and SFTP free space.

## Verified installation (2026-11-01)

Snapshot `c14508652bf9581d3c630b347e429482` contains 740,857,363 bytes
and no failed files. Its isolated restore passed validation for all 36
artifacts; all 43 attached persistent volumes had coverage or explicit exclusions.
Native isolated PostgreSQL 18 clusters, MongoDB/oplog, LDAP and Portainer
restores were exercised. Restored n8n, Appsmith and Nexus accepted their original
accounts; Arcane passed SQLite integrity/owner checks and service startup.
AdGuard reached readiness with recovered configuration/work data and preserved
ownership. Its isolated clone needed different bind addresses, not production
DNS changes. Nextcloud's database and complete file artifacts were verified;
a complete restored Nextcloud/SSO application cutover was not tested.

VM backups are enabled with a native 21,600-second interval, essential
prepare/cleanup actions and stable `backup@sophon` identity.
The x870 controller's six-hour systemd timer is enabled. Its recovery snapshot
`00837017f172b887ed425a32c184b263` was isolated-restored, and all six files
(SSH key, SFTP unit, backup script, protected environment, controller unit/timer)
matched the originals byte for byte. Its other sources protect SFTP bootstrap,
client/host keys and controller ACME material.
Manual Kopia deployment run 8 passed after scheduling was enabled, preserving
all stack secrets and the native interval across redeployment. Native quick
maintenance completed successfully against the actual backed-up SFTP repository.

**Remaining host action:** x870's runtime firewall opening works, but the
persistent NixOS rule has not been saved. Allow only InfraVM `10.1.1.3` to
`10.1.1.129:2222` on `enp8s0`. A reboot/firewall restart can otherwise interrupt
VM backups. Keep the encryption password independently recoverable.

## Verified split-label rollout (2026-11-01)

All 33 live participants now use v2 split labels; none retains
`sophon.backup.spec`. Their decoded recipes exactly match the original v1
recipes. All 43 attached persistent volumes remain covered or explicitly
excluded, and protected mounts, deployment secrets, commands and original
running/stopped states were verified through container recreation.
Core templates, host-bootstrap and authoritative Gitea Compose sources were
updated. Strict v1 parsing remains available for old snapshot metadata.

Encrypted snapshot `f5223eea8f545efffd12af956efe321a` reports 769,724,822 bytes
and zero failed files. Its isolated restore validated every hash/archive and
restore recipe for all 36 artifacts. No recovery journal or successful staging
remained; native workload health and published application endpoints passed
afterward. The original six-hour VM schedule is enabled again. Snapshot
manifests and recovery journals still use version 1.

Validation passed: 48 collector tests, plus 142 repository tests and 56
subtests (six skips). The repository run used pytest without stdin capture
for its existing mocked interactive console tests.
Actual manual deployment run 10 then passed using the migrated runner, keeping
all v2 participants, original secrets and enabled scheduling. The connected
server reported `/source` as `IDLE` with a 21,600-second interval.

## Verified application-clone recovery (2026-11-01)

The same snapshot `f5223eea8f545efffd12af956efe321a` was restored through a
separate read-only Kopia client with snapshot actions disabled. All 36 artifacts
were reverified. Application/database clones used new session-owned volumes
and an internal network without a default route, public ports, production
mounts or Docker sockets. Original FQDNs resolved to a private file-provider-only
Traefik proxy using the recovered wildcard certificate, never production.

| Workload | Actual recovery evidence |
| --- | --- |
| Keycloak/PostgreSQL | Original administrator login; two users, master/Sophon realms and restored OIDC clients. |
| Gitea/PostgreSQL | Original account, ten repositories, and full Git object integrity for `siteadmin/kopia`. |
| Nextcloud/PostgreSQL | Installed 35.0.2 application, original local admin/WebDAV, and an actual recovered file with exact SHA-256. |
| LDAP -> Keycloak -> Nextcloud | Real authorization-code login through the restored provider and strict LDAPS, using a temporary clone-only LDAP user. This is separate from original-account recovery. |
| LDAP | Native offline imports, normal image startup, strict TLS/root bind and all 13 original active entries. |
| Rocket.Chat/MongoDB | Complete users/oplog import followed by normal authenticated MongoDB startup; three users; original Rocket.Chat admin, one channel and one recovered message. |
| Arcane | Original user/token and authenticated environment endpoint; both SQLite databases passed integrity and the owner record survived. No Docker-management operations were attempted. |
| n8n | Original owner login and SQLite integrity; one owner, zero workflows/credentials in this snapshot. Fresh Python/JavaScript launchers registered with the cloned broker; no workflow execution was claimed. |
| Appsmith | Original owner session/user endpoint and one recovered workspace. |
| Nexus | Original administrator and seven restored repository definitions. No artifact download/upload was claimed. |
| Homepage | Restored configuration and HTTP application readiness. Production widgets/external services were not contacted. |
| Portainer | Native setup-token restore, isolated restart, original admin and all 24 stack definitions. No deployment operations were attempted. |
| AdGuard | Native inner archives/ownership, original admin and recovered configuration; real loopback DNS answer from a clone-only reserved-domain fixture. Production DNS and original client restrictions stayed unchanged. |
| Ten Gitea runners | Registration-file SHA-256 and required identity fields; normal entrypoints never started. |
| CyberChef | Fresh stateless image reconstruction and actual application HTTP response. |

This is restoration testing, not a production cutover or exhaustive feature
test. The original existing SSO user's password was not reset or assumed;
the federation test used a synthetic account only in the lab. Original images,
secrets and databases were retained. Restore resources are disposable and
must be removed after validation; the encrypted repository must be retained.
The x870 persistent-firewall limitation remains unchanged.

Cleanup passed: all 39 lab containers, 37 lab volumes, the internal network and
39 private remote environment files were removed. No owner-labelled test
container/volume remained. All 40 production containers retained their original
images, environments, labels, commands, protected mounts and running/stopped
states. Post-cleanup strict inventory found 33 v2 participants and all 43
production volumes covered or explicitly excluded. The connected production
repository's `/source` was IDLE with scheduling enabled at 21,600 seconds.

## Checks

From the Sophon root, `python3 tests/backup_checks.py` runs the 48 dependency-free collector tests;
`python3 tests/restore_checks.py` runs the eight recovery tests, including approval,
hash/profile/dependency/mount validation, isolated execution, exact-resource
resume, dirty-import rebuild, snapshot/state separation and foreign-resource
cleanup refusal.

`tests/test_kopia.py` also exercises normal core ordering/offline packaging,
required/private Configure inputs and real Ansible-to-HTTP deployment fixtures:
fresh collector-key creation, key reuse/environment preservation on rerun,
and explicit refusal of a revoked key. The regular Sophon suite includes these
checks; the build image contains no tests or Gitea deployment workflow.

### Verified automation (2026-11-01)

Real snapshot `80f493f79da23e8fb8dc2985940b1e4a` contains 776,173,048 bytes,
36 artifacts and zero failed files. It was independently restored read-only
with actions disabled. Gitea deployment runs 11 and 12 passed; run 12 packages
the final recovery tool. Its bytes were matched against the published source,
then the packaged tool performed a genuinely fresh automatic recovery:
two users, both master/Sophon realms and original master-administrator login
passed. The bootstrap system role was verified renamed, login-disabled and
password-cleared while retaining PostgreSQL's mandatory `SUPERUSER` attribute.

Rerunning the same command/state retained exact container/volume/network IDs
and revalidated credentials/data. Both target containers had no default route,
host binds or published ports. The real cleanup command removed its own two
containers, two volumes and internal network. Earlier real attempts exposed
the PostgreSQL system-role restriction and Podman's start-time `HOSTNAME`
injection; both have regression coverage rather than bypassed guards.
The source Keycloak backup recipes, image IDs, secrets, commands and volumes
were preserved through label rollout, and the VM's deployment template was
updated so a subsequent core configure retains the approved labels.

The full core repository suite also passed: 142 tests, six skips and 56
subtests. No production cutover or automated LDAP recovery is claimed; only
the PostgreSQL/Keycloak group is automated in profile version 1.

All executor targets and the independent client's container, three volumes
and protected remote environment file were removed. Final strict inventory
still reports 33 production backup participants and 43 covered/excluded
volumes. All 40 production containers preserve recorded commands, secrets,
protected mounts and running/stopped states; only the planned Kopia image
changed. Production application HTTPS checks passed, `/source` is IDLE with no
unresolved journal, and enabled native scheduling retains its 21,600-second
interval. Decrypted operational scratch is disposable, not a retained backup.

### Executable n8n and Nexus artifact follow-up (2026-11-01)

Newest encrypted snapshot `d330628cf5ddea255eae0ffb70a6dbbc` reports
772,975,868 bytes and zero failed files. A separate read-only client with
actions disabled restored it; all 36 artifacts verified. Internal clones again
used only fresh owned volumes, no published ports or production mounts, and
no default route. A real connection attempt to production Portainer was blocked.

The production n8n snapshot had no workflows/credentials, so a synthetic active
workflow and encrypted header credential were created only in the first clone.
The webhook executed JavaScript and Python through fresh external runners
(each returned `42`), then authenticated to a private fixture service using the
stored credential. After graceful shutdown, the same labelled filesystem
boundary was cold-archived, SHA-256 checked, and restored into a second new
volume. Original owner login, recovered webhook/workflow execution, both task
runners, encrypted-credential authentication and persisted successful execution
all passed without recreating the workflow or credential. Recovered SQLite
integrity was `ok`, with one workflow and one non-plaintext credential row.
Preserve the source encryption key and matching runner image/authentication;
restore ownership `1000:1000`.

The first Nexus clone accepted the original administrator and seven original
repository definitions. A synthetic raw hosted repository accepted an
authenticated 1,048,611-byte upload and exact download. Graceful shutdown,
full `/nexus-data` cold archive, SHA-256 verification and restoration into a
second fresh volume preserved its repository, asset metadata and artifact.
Authenticated download and Nexus's own asset SHA-256 both matched:
`70d4557262912f4c5f143a04710775fc27b6988f4bbdb67e10e78fc4c88c43df`.
Preserve H2 and blobs together, the source image, ownership `200:200`, and
the 120-second shutdown allowance.

These are real native fixture round trips after recovering the encrypted
production snapshot, **not** a new encrypted snapshot containing the fixtures.
They do not prove recovery of pre-existing production n8n workflows or every
Nexus repository format. Neither workload has an automated recovery profile.
All 15 lab containers, seven volumes, one internal network and eight protected
remote environment files were removed. All 40 production containers retained
their exact original configurations, images, mounts and running/stopped states.
Blank-host follow-up is recorded below; no production cutover was attempted.

### Offline blank-host recovery follow-up (2026-11-01)

With explicit approval, disposable Proxmox VM 101 was created with 12 GiB RAM,
four vCPUs and a fresh 100 GiB disk. It was **not a clone of InfraVM**. The normal
Sophon installer installed Fedora CoreOS onto the blank disk using existing
offline deployment media and a test-only bootstrap. The VM had no virtual NIC,
only loopback, and no default route. SELinux was verified `Enforcing`.
Production VM 100's configuration was unchanged before and after the drill.

The encrypted repository was independently connected read-only with actions
disabled and synchronized into an offline filesystem mirror. Only the exact
source Kopia, Portainer, PostgreSQL and Keycloak images were exported for this
test. Checksummed media crossed the pinned-TLS hypervisor management channel;
repository passwords/API credentials were delivered separately, not embedded
in the data ISO. The fresh VM verified all media file hashes, loaded the images,
opened the encrypted filesystem repository read-only with actions disabled,
and restored snapshot `d330628cf5ddea255eae0ffb70a6dbbc`.
All 36 artifacts verified again, including 775,272,853 native payload bytes.

| Fresh-host check | Verified result |
|---|---|
| Portainer native archive restore | Recovered TLS material, original administrator login and all 24 original stack definitions |
| Target discrimination | Restored manager used only the new VM's rootless Podman socket; engine identity differed from production |
| Actual management operation | Restored Portainer deployed a new harmless offline Compose stack, retained its exact Compose file, served the packaged recovery script with matching SHA-256, then deleted the stack/container |
| Original stack preservation | All 24 recovered definitions, endpoint references and environments remained unchanged; none was deployed |
| Packaged Keycloak recovery | Published source bytes matched; real PostgreSQL import recovered two users/two realms, original administrator login and the `sophon`/`master` realms |
| Resume and cleanup | Repeat retained exact resource IDs and repeated checks; real executor cleanup removed its owned resources |

The initial PostgreSQL system role was login-disabled, password-cleared and
renamed; its mandatory `SUPERUSER` attribute remains explicitly reported.
Recovery targets had an internal network, no published ports and no
application host binds. The management socket belonged only to the disposable
host, never to production.

#### Offline bootstrap prerequisites discovered

- Fedora CoreOS did not include `qemu-guest-agent`. This NIC-less drill needed
  an explicitly supplied guest-agent executable and matching shared libraries
  before native hypervisor guest execution worked.
- Agent media required the existing Fedora executable SELinux type
  `virt_qemu_ga_exec_t`, verified with `matchpathcon /usr/bin/qemu-ga`.
  Correct the service/media label rather than disabling SELinux. The installed
  test service was corrected during the drill; the first generated installer
  ISO alone is **not** a proven unattended bootstrap recipe.
- Create the filesystem destination before native `repository sync-to`.
  Verify checksums before image loading/restoration, preserve immutable source
  image IDs, and keep the repository password available outside the repository.
- A recovered Portainer archive restores definitions, not an already running
  site. Authenticate to the fresh manager, issue a fresh target-only API key,
  assert its engine identity, and verify isolation before deploying anything.
  Never blindly start the restored production stacks.

**This proves offline bootstrap plus Portainer and Keycloak/PostgreSQL recovery,
not a complete automatic full-site rebuild.** Only four application source
images were transferred. Other workload recovery profiles, all-workload
blank-host startup, external integrations and production cutover are not proved
by this drill. The n8n/Nexus second-generation fixture proofs above remain
separate native cold-archive tests.

After verification, VM 101 was gracefully shut down and removed, its dedicated
disk and all four test ISOs were deleted, and their absence was checked.
Original shared installer/deployment media and VM 100 were preserved.
Final production checks found all 40 containers, 33 v2 backup participants and
43 covered/excluded volumes, no recovery journal, a connected repository and
the unchanged 21,600-second interval. n8n, Nexus, Keycloak, Nextcloud, Gitea,
Rocket.Chat and Homepage all passed HTTPS checks. Temporary private recovery
scratch is disposable, not a retained backup.
