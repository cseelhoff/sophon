# Migrating outside workloads to backup v3

This is a clean break. Keep `kopia-legacy` for existing snapshots; v3 does not
decode old labels/manifests, unpack their tar artifacts or invoke their restore
recipes. Before replacing a running legacy collector, quiesce its schedule,
finish pending stop recovery, and retain its repository credentials and snapshot
history.

Workloads outside Sophon are not edited by this migration. Update their
authoritative Compose/IaC sources yourself and recreate their containers:
changing a template does not change immutable labels on an existing container.
Reconstructible management and routing stacks (`arcane`, `coredns`, `cloudflared`,
`kopia`, `dump-proxy`) carry `sophon.backup.enable: "false"` explicitly.

1. Inventory irreproducible data and application-generated keys. Exclude images,
   caches and configuration your deployment recreates.
2. Replace legacy `sophon.backup.*` / `sophon.restore.*` declarations with the
   concise Traefik-style [`sophon.backup.*` label contract](../roles/kopia/README.md#self-service-container-labels)
   (`sophon.backup.enable: "true"`).
3. Declare persistent file storage with `sophon.backup.volumes: /data` (plus
   optional `sophon.backup.exclude` `.kopiaignore` patterns) or selective
   `sophon.backup.paths`. Kopia automatically discovers named volumes under
   `/volumes:ro`—no `SOPHON_KOPIA_EXTRA_VOLUMES` variable or Kopia redeploy is needed.
4. Keep containers running (`sophon.backup.stop` is retired; backups are 100% hot).
   Database containers emit uncompressed native dumps on stdout (`sophon.backup.dump`
   with `pg_dump -Fc -Z0` or `pg_dumpall`), executed inside the DB container via
   `dump-proxy` without sharing DB credentials with Kopia. No live database-directory
   copy is a primary backup.
5. Remove `${SOPHON_BACKUP_DIR}` temporary-file hooks, tar export helpers, and
   archive/pre/post/restore recipes.
6. Represent reconstructible services with `sophon.backup.enable: "false"` (or omit
   `sophon.backup.*` labels). Undeclared volumes are visible in every report;
   they are neither automatically backed up nor a whole-run failure.
7. Keep deployment definitions and supplied secrets in Git / Gitea Actions Secrets,
   never only in Arcane. `dump-proxy` strips `Config.Env` and never captures
   container environment variables.
8. Run `podman exec kopia python3 /app/backup.py run` (or click **Snapshot Now**
   on `/sophon-state` in the Kopia UI), check `/source/last-run.json`, and verify
   a test restore (`sophon-restore --group <group>`) before relying on the schedule.

Examples requiring workload-specific decisions:

* n8n/SQLite apps: never copy a live SQLite file with uncoordinated concurrent
  writers. Prefer PostgreSQL with `sophon.backup.dump` (`pg_dump -Fc -Z0` +
  `sophon.backup.dump.restore: "postgres-custom"`), or stream an online SQLite
  `.backup` dump over stdout alongside `sophon.backup.volumes` / `sophon.backup.exclude`
  for separate persistent user/key files.
* Nextcloud/Gitea-style apps: combine hot whole-volume file backups
  (`sophon.backup.volumes` + `sophon.backup.exclude` for transient upload/indexer/session
  paths such as Gitea's `git/lfs/tmp/`) with `sophon.backup.ini` for generated
  encryption/signing identity and online native database dumps (`pg_dump -Fc -Z0`
  or `pg_dumpall`). Zero container stops are required.
* MongoDB: use its coherent native export, including user/oplog requirements
  appropriate to that deployment; do not copy the live database volume.
* Appsmith/Nexus: design and test a native database export instead of carrying
  the old cold/live whole-application archive recipe forward.
* AdGuard: declare irreplaceable user settings/state only (`sophon.backup.paths`
  or `sophon.backup.volumes` + `sophon.backup.exclude` for query logs/caches).
  The old bespoke tar helper is not a v3 shim.
* Runners: preserve identity only if needed for real recovery; never start
  recovered production runner identities in dev.

There is deliberately no automatic interpretation of old exclusions, empty
restore-command lists or deployment-environment metadata. The explicit
`sophon-restore` command accepts native profiles declared by **current IaC**:
`sophon.backup.dump.restore` (or `sophon.backup.dumps.<key>.restore`) set to
`postgres-custom` (`pg_restore --clean --if-exists --no-owner`),
`postgres-cluster` (`psql`), or `ldap-subtree` (`slapadd`, paired with
`sophon.backup.dump.base=dc=site,dc=example`). Its PostgreSQL profiles require
the current `/var/lib/postgresql` named-volume layout and matching database
`image_id`; LDAP requires the matching `image_id`, named MDB/config/certificate
mounts and freshly configured schema. File-only application containers need no
import profile and may restore volume data across rebuilt app images.

Other native export formats remain valid backups, but automatic group restoration
fails closed until an appropriate native import profile is implemented and tested.
Document/test their manual native import procedure; do not substitute a generic
shell restore label or historical archive recipe. The core profiles never execute
snapshot-supplied shell commands or restore container environments.
