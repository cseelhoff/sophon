# Migrating outside workloads to backup v3

This is a clean break. Keep `kopia-legacy` for existing snapshots; v3 does not
decode old labels/manifests, unpack their tar artifacts or invoke their restore
recipes. Before replacing a running legacy collector, quiesce its schedule,
finish pending stop recovery, retain its repository credentials and snapshot
history, and revoke its old Portainer collector key when no longer needed.

Workloads outside Sophon are not edited by this migration. Update their
authoritative Compose/IaC sources yourself and recreate their containers:
changing a template does not change immutable labels on an existing container.
The existing host-bootstrap entry point refreshes the managed Portainer
container's labels while retaining its exact image and `portainer_data` volume;
it refuses an unexpected data mount rather than adopting it.

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

* n8n/Arcane/SQLite: use SQLite's native export/backup tool for the database.
  Stop writers and declare only the separate persistent user/key files, not
  SQLite database files, transient locks or caches. A cold whole-directory copy
  is not a substitute for the native primary database export.
* Nextcloud/Gitea: combine stopped user-file writers with native database
  exports. Preserve application-generated encryption/signing identity.
* MongoDB: use its coherent native export, including user/oplog requirements
  appropriate to that deployment; do not copy the live database volume.
* Appsmith/Nexus: design and test a native database export instead of carrying
  the old cold/live whole-application archive recipe forward.
* AdGuard: declare irreplaceable user settings/state only and explicitly
  decide which writers must stop. The old bespoke tar helper is not a v3 shim.
* Runners: preserve identity only if needed for real recovery; never start
  recovered production runner identities in dev.

There is deliberately no automatic interpretation of old exclusions, empty
restore-command lists or deployment-environment metadata. The explicit
`sophon-restore` command accepts native profiles declared by **current IaC**:
`dumps.N.restore=postgres-cluster`, or `ldap-subtree` with
`dumps.N.base=dc=site,dc=example`. Its PostgreSQL profile requires the current
`/var/lib/postgresql` named-volume layout and matching image; LDAP requires the
matching image, named MDB/config/certificate mounts and freshly configured schema.
File-only groups need no import profile.

Other native export formats remain valid backups, but automatic group restoration
fails closed until an appropriate native import profile is implemented and tested.
Document/test their manual native import procedure; do not substitute a generic
shell restore label or historical archive recipe. The core profiles never execute
snapshot-supplied shell commands or restore container environments.
