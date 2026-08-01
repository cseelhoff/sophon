# Service state is classified as reconstructible or irreplaceable

Every piece of state a Sophon service holds is one of two kinds, and the kind
determines how it is protected.

**Reconstructible state** is declared in Ansible and can be rebuilt by
re-running Deploy — the Keycloak realm, its LDAP federation and mappers, the
OIDC clients, the seeded LDAP users, Traefik's routing. It must survive a
container restart, but it does not need backing up.

**Irreplaceable state** cannot be regenerated from the repo — Gitea
repositories, LDAP entries users created themselves, `acme.json`, and the
contents of `artifacts/`. It is Kopia's responsibility.

## Why this needed deciding

- Keycloak runs `KC_DB=dev-mem`. Its realm, clients, mappers and group scope
  live in memory, so a container restart discards everything `configure.yml`
  builds — and takes Gitea's OIDC source down with it, since that source points
  at a realm that no longer exists. Reconstructible does not mean volatile.
- Kopia backs up nothing. `roles/kopia/README.md` specifies the contract —
  services dump into `/var/mnt/nfs/backups/<service>/` and Kopia snapshots that
  tree — but no service implements it. The Gitea dump sidecar is written up as
  an example rather than shipped, so Kopia snapshots an empty directory every
  six hours.
- All service volumes are plain named volumes on InfraVM's local disk. Nothing
  is on `/var/mnt/nfs`.

## Considered options

- **Bind every service volume onto NFS.** Durable with no sidecars, but running
  Postgres and SQLite over NFS is a locking hazard.
- **Treat all service state as disposable.** Attractive because ADR-0008 makes
  re-running cheap, but it does not survive a reboot, and Gitea repositories are
  not reconstructible from anything.

## Consequences

- Keycloak moves off `dev-mem` to Postgres on its existing volume. The Postgres
  image is already prestaged for Gitea, so this adds no artifact.
- The dump-sidecar contract gets implemented rather than documented.
- `artifacts/` becomes a Kopia source. ADR-0008 made losing it equivalent to
  losing access to the deployment.
