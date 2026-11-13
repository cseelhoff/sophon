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
2. Replace all `sophon.backup.*` and old `sophon.restore.*` declarations with
   the [v3 contract](../roles/kopia/README.md#v3-declarations). Do not mix versions.
3. Declare each filesystem path as a volume **name** and relative path. Add that
   actual name to `SOPHON_KOPIA_EXTRA_VOLUMES`, retained in site IaC, and redeploy
   Kopia to mount it read-only.
4. Put every running filesystem writer in the same group with `stop=true`.
   Database containers remain running and emit native dumps on stdout. No
   live database-directory copy is a primary backup.
5. Remove `${SOPHON_BACKUP_DIR}` temporary-file hooks, tar export helpers,
   archive/pre/post/restore recipes, coverage and exclusion labels. A dump is
   a single argv command; use an explicit shell only when needed. The producer
   requires `sh` and `setsid`.
6. Represent “reconstructible” with no paths/dumps, not an invented backup of
   configuration. Unlabelled/undeclared volumes are visible in every report;
   they are neither automatically backed up nor a whole-run failure.
7. Keep deployment definitions and supplied secrets outside Portainer.
   The collector no longer captures stack definitions or environment values.
8. Run `podman exec kopia python3 /app/backup.py run`, check its exit status and
   `/source/last-run.json`, and inspect a restored dev snapshot before enabling
   the schedule.

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
restore-command lists or deployment-environment metadata. M2 will provide the
explicit new group restore; do not substitute historical recovery recipes.
