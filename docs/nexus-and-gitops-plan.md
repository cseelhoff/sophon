# Sonatype Nexus Core Integration & Pure GitOps Architecture Plan

This document specifies the design and implementation plan for integrating **Sonatype Nexus Repository Community Edition** into **Sophon Core**, disabling Gitea's built-in package registry to eliminate split-brain artifact storage, standardizing **Pure GitOps** workload delivery across Gitea, Nexus, and Arcane, and automating deterministic workload reconciliation during **Safe Standby Restore (`standby_restore`)** drills.

---

## 1. Architectural Decisions & Rationale

### 1.1 Why Nexus Lives in Sophon Core (Stage 1) Rather Than Stage 2
Integrating Nexus directly into Sophon Core (`4-configure.yml` and `sophon-apps.iso`) instead of deploying it later as a Stage-2 Gitea workload provides four decisive advantages:

1. **Zero dependency on Gitea's built-in registry (`GITEA__packages__ENABLED=false`):** Every container image, Python wheel, npm tarball, Debian `.deb`, Maven JAR, Cargo crate, and generic binary has a single canonical home in Nexus (`nexus.<domain>`, `registry.<domain>`, `registry-push.<domain>`). Operators and CI pipelines never face two competing registries or a bootstrap chicken-and-egg exception.
2. **Single-pass disaster recovery (`sophon-restore`):** During recovery or weekly `provision-dev` restore drills, `sophon-restore` restores Core services (`traefik`, `ldap`, `keycloak`, `gitea`, `nexus`) in one deterministic pass *before* Arcane reconciles Stage-2 application workloads. Every workload image and upstream cached layer is immediately available at `https://registry.<domain>` even when the site or drill VM is completely air-gapped.
3. **Zero extra database engine in `sophon-apps.iso`:** Nexus 3.77+ Community Edition supports external PostgreSQL natively without a Pro license (up to 100,000 components and 200,000 HTTP requests/day). Sophon Core already bundles `docker.io/library/postgres:18.6-alpine3.24` for Keycloak and Gitea, so `nexus-db` reuses the exact same pinned PostgreSQL image archive.
4. **Automated Day-1 governance in Ansible (`roles/nexus`):** Nexus requires post-boot API initialization (rotating the random initial admin password, accepting the Nexus 3.77+ Community Edition EULA, disabling anonymous public access, binding OpenLDAP, separating `hosted` vs. `proxy-cache` blob stores, and creating repositories). Keeping this idempotent bootstrap in `roles/nexus` makes every Sophon install ready for builds and pulls on Day 1.

**Accepted resource trade-offs:**
- Default thin-provisioned VM sizing uses **`12288` MiB RAM** and **`1024` GiB thin virtual disk** (`SOPHON_INFRAVM_MEMORY` / `SOPHON_INFRAVM_DISK_GB`) on InfraVM and **`1536` GiB thin virtual disk** (`SOPHON_BACKUP_VM_DISK_GB`) on the paired SFTP Backup VM, with hypervisor `discard=on` + guest `discard=async` / `fstrim` reclaiming deleted blocks automatically and Nexus's JVM explicitly bounded (`-Xms1024m -Xmx2048m -XX:MaxDirectMemorySize=1536m`). Lower validation minimums (`1024` MiB / `40` GiB on InfraVM, `16` GiB on Backup VM) remain unchanged for constrained lab overrides.
- `sophon-apps.iso` grows by the `sonatype/nexus3` image archive (~800 MB) and Step 4 cold boot adds ~60–90 seconds for Nexus initialization.

---

### 1.2 Why Kopia Does Not Receive Host Podman or Arcane Admin Access
Kopia (`https://kopia.<domain>`) remains strictly isolated from `/run/user/1000/podman/podman.sock` and Arcane admin credentials:

1. **No restore hooks in Kopia:** Kopia Actions only execute `before-snapshot-root` / `after-snapshot-root` hooks during backups; Kopia has no `before-restore` or `after-restore` action lifecycle.
2. **Logical hot exports require ordered host-side orchestration:** Sophon v3 snapshots intentionally capture online logical exports (`pg_dumpall`, `slapcat`, selective INI keys, and `sophon.backup.paths` Btrfs reflinks) via the narrow `dump-proxy` sidecar rather than raw live database files. Restoring them requires `sophon-restore` on the InfraVM host to stop target peers, import SQL/LDIF dumps against verified image digests, restore volume paths atomically, and restart stacks in dependency order.
3. **Least privilege & circularity:** `kopia` is exposed through Traefik (`https://kopia.<domain>`) and runs with `KOPIA_READONLY=true` during standby drills. Granting it host Podman or Arcane admin control would turn a backup viewer compromise into full rootless host takeover and create a circular failure mode if Kopia stopped Traefik, Keycloak, or itself mid-restore.

---

## 2. Nexus Core Service Architecture (`roles/nexus`)

### 2.1 Containers, Persistence & JVM Sizing
`roles/nexus` deploys a two-container Compose stack on the external `proxy` network:

| Container | Image | Purpose | Named Volume |
| :--- | :--- | :--- | :--- |
| `nexus` | `docker.io/sonatype/nexus3:3.98.1` | Artifact repository & pull-through cache | `nexus_data:/nexus-data` |
| `nexus-db` | `docker.io/library/postgres:18.6-alpine3.24` | Metadata, security, RBAC & component index DB | `nexus_db_data:/var/lib/postgresql` |

Key runtime settings on `nexus`:
- `NEXUS_DATASTORE_ENABLED=true` with JDBC `jdbc:postgresql://nexus-db:5432/nexus`.
- `NEXUS_SECURITY_RANDOMPASSWORD=true` so first boot writes a one-time `/nexus-data/admin.password` rotated immediately by `roles/nexus`.
- `NEXUS_SECRETS_KEY_FILE=/nexus-data/etc/nexus.secrets.json`: a persistent 32-byte AES secret key generated once before first start so secrets stored inside PostgreSQL (such as the OpenLDAP bind password and upstream proxy credentials) remain decryptable after `sophon-restore` on a replacement VM.
- Pre-created volume subdirectories (`/nexus-data/blobs/hosted`, `/nexus-data/blobs/scratch`, `/nexus-data/blobs/proxy-cache`, `/nexus-data/keystores`, `/nexus-data/etc`) owned by UID/GID `200:200` before `nexus` starts so `sophon-restore`'s `bounded_target()` check always finds valid parent directories even when `SOPHON_PREPARE_RESTORE=true`, and Traefik sets `--entrypoints.websecure.transport.respondingTimeouts.readTimeout=0` so multi-GB ISO and multi-hundred-GB forensics HTTP `PUT` streams never time out after 60 seconds.

### 2.2 Single-Label Wildcard Endpoints (`*.<domain>`)
Sophon's Let's Encrypt DNS-01 certificate covers `<domain>` and single-label `*.<domain>`. Traefik routes three dedicated hostnames to `nexus` over TLS:

| Public / Internal URL | Container Port | Backing Nexus Target | Purpose |
| :--- | :--- | :--- | :--- |
| `https://nexus.<domain>` | `8081` | Nexus Core HTTP | Web UI, REST API, PyPI, npm, APT, Maven, Cargo, Raw |
| `https://registry.<domain>` | `5000` | `docker-group` connector | Unified OCI pull endpoint (`docker-hosted` + Docker Hub, GHCR, Quay proxies) |
| `https://registry-push.<domain>` | `5001` | `docker-hosted` connector | Dedicated OCI push endpoint for Gitea Actions CI runners |

Because `coredns-dockerdiscovery` discovers all `traefik.http.routers.*.rule=Host(...)` labels automatically, all three FQDNs resolve locally via CoreDNS to `infravm_ip` and—only when `SOPHON_CF_TUNNEL_ENABLED=true` and the node is active—publish through Cloudflare Tunnel.

### 2.3 Security Posture & Native OpenLDAP Integration
Because `nexus.<domain>` and `registry.<domain>` may be reachable via Cloudflare Tunnel when enabled, **anonymous access is disabled by default** (`SOPHON_NEXUS_ANONYMOUS_ENABLED=false`). Every Web UI login, package install, and container pull/push is authenticated.

#### Seeded OpenLDAP Groups (`roles/openldap`)
`roles/openldap/templates/docker-compose.yml.j2` seeds two new `groupOfNames` entries under `ou=groups,<base_dn>` alongside `cn=arcane-admins`:
- `cn=nexus-admins,ou=groups,<base_dn>` (seeded member: `uid=john.doe.admin,ou=people,<base_dn>`)
- `cn=nexus-developers,ou=groups,<base_dn>` (seeded members: `uid=john.doe,ou=people,<base_dn>` and `uid=john.doe.admin,ou=people,<base_dn>`)

#### Nexus LDAP Connection, Realms & Role Mapping
`roles/nexus/files/configure_nexus.py` configures Nexus idempotently over `http://127.0.0.1:8081/service/rest`:
1. **TLS Truststore:** Reads `/run/secrets/ldap/certs/<domain>.crt` (dumped from Traefik's `acme.json` by `openldap`'s `certs-dumper`) and imports the certificate into Nexus's SSL truststore (`/v1/security/ssl/truststore`) with `useTrustStore: true` so `ldaps://ldap.<domain>:636` verifies cleanly in both Let's Encrypt production and air-gapped/custom-CA environments.
2. **LDAP Server (`sophon-openldap`):** Connects via `ldaps://ldap.<domain>:636` using `cn=keycloak-bind,ou=services,<base_dn>` (`SIMPLE` bind), searching users under `ou=people` (`inetOrgPerson`, `uid`, `cn`, `mail`) and static groups under `ou=groups` (`groupOfNames`, `cn`, `member`) with `ldapGroupsAsRoles: true`.
3. **RBAC Mapping:**
   - Nexus role with ID `nexus-admins` (matching LDAP group `cn=nexus-admins`) grants `nx-admin`.
   - Nexus role with ID `nexus-developers` (matching LDAP group `cn=nexus-developers`) grants `nx-repository-view-*-*-browse`, `nx-repository-view-*-*-read`, `nx-repository-view-*-*-add`, `nx-repository-view-*-*-edit`, `nx-search-read`, and `nx-apikey-all` (allowing developers and CI tokens to read all group/proxy repos and publish to hosted repos without administrative privileges).
   - Nexus role `sophon-reader` + local service user `sophon-pull` (with generated random password saved in `/var/lib/sophon/project/credentials/nexus/pull_password` and written into `/var/home/core/.config/containers/auth.json` and `/var/home/core/.docker/config.json` for `registry.<domain>`) gives InfraVM's rootless Podman and Arcane zero-touch authenticated pulls from `https://registry.<domain>` without enabling public anonymous access.
4. **Active Security Realms (`/v1/security/realms/active`):**
   `["NexusAuthenticatingRealm", "LdapRealm", "DockerToken", "NpmToken"]`.

---

## 3. Blob Store Separation & Kopia v3 Hot Backup/Restore

### 3.1 `hosted`, `scratch`, and `proxy-cache` Blob Stores
Nexus separates irreplaceable first-party/vault artifacts from re-obtainable local files and disposable internet caches at the blob-store boundary:

| Blob Store | Container Path | Contents | Kopia Backup Policy |
| :--- | :--- | :--- | :--- |
| `hosted` | `/nexus-data/blobs/hosted` | First-party OCI images, wheels, `.deb`s, npm packages, JARs, crates, general `raw-hosted` files, and write-once `raw-vault` forensics/archive captures | **Backed up** (`sophon.backup.paths`) |
| `scratch` | `/nexus-data/blobs/scratch` | Large re-obtainable local artifacts in `raw-scratch` (OS ISOs, RSAT, vendor installers) | **Excluded** (re-obtainable; zero backup bloat) |
| `proxy-cache` | `/nexus-data/blobs/proxy-cache` | Pull-through upstream caches (Docker Hub, GHCR, Quay, PyPI, npmjs, Debian APT, Maven Central, crates.io) | **Excluded** (re-populated on demand) |

Default sample repositories shipped by Nexus (`maven-releases`, `maven-snapshots`, `maven-central`, `maven-public`, `nuget-*`) that reference the unbacked-up `default` blob store are deleted during Day-1 bootstrap and replaced with Sophon's explicit `hosted` / `proxy-cache` repositories so **zero hosted artifacts can accidentally land in `default`**.

### 3.2 Kopia v3 Labels
- On `nexus`:
  ```yaml
  labels:
    - 'sophon.backup.enable=true'
    - 'sophon.backup.group=nexus'
    - 'sophon.backup.paths=/nexus-data/blobs/hosted,/nexus-data/keystores,/nexus-data/etc/nexus.secrets.json'
  ```
- On `nexus-db`:
  ```yaml
  labels:
    - 'sophon.backup.enable=true'
    - 'sophon.backup.group=nexus'
    - 'sophon.backup.dump=exec pg_dumpall -U "$$POSTGRES_USER" --clean --if-exists'
    - 'sophon.backup.dump.name=postgres-cluster.sql'
    - 'sophon.backup.dump.restore=postgres-cluster'
  ```

Why `sophon.backup.paths` is ideal here:
1. Kopia's `backup.py` recognizes `nexus_data` as a declared container volume (reporting zero undeclared volumes in `/source/last-run.json`).
2. `backup.py` reflinks *only* `/nexus-data/blobs/hosted`, `/nexus-data/keystores`, and `/nexus-data/etc/nexus.secrets.json`—completely skipping `/nexus-data/blobs/proxy-cache`, `/nexus-data/cache`, `/nexus-data/tmp`, and `/nexus-data/log`.
3. During `sophon-restore`, `restore.py` stops `nexus`, imports `postgres-cluster.sql` into `nexus-db`, atomically restores the `hosted` blob store, signing keystores, and `nexus.secrets.json`, leaves `/nexus-data/blobs/proxy-cache` untouched, and restarts `nexus` with all PostgreSQL-stored repository definitions, LDAP mappings, and encrypted credentials intact.

---

## 4. Pre-Configured Repository Matrix

When `prepare_restore` is `false` (Day-1 initialization or idempotent `sophon-deploy` re-runs), `roles/nexus` provisions the following repositories:

| Ecosystem | Hosted (`hosted` blob store) | Proxy (`proxy-cache` blob store) | Unified Group / Access URL |
| :--- | :--- | :--- | :--- |
| **Docker / OCI** | `docker-hosted` (port `5001`, `https://registry-push.<domain>`) | `docker-hub-proxy` (`registry-1.docker.io`), `ghcr-proxy` (`ghcr.io`), `quay-proxy` (`quay.io`) | `docker-group` (port `5000`, `https://registry.<domain>`) |
| **Python (PyPI)** | `pypi-hosted` | `pypi-proxy` (`https://pypi.org`) | `pypi-group` (`https://nexus.<domain>/repository/pypi-group/simple`) |
| **Node.js (npm)** | `npm-hosted` | `npm-proxy` (`https://registry.npmjs.org`) | `npm-group` (`https://nexus.<domain>/repository/npm-group/`) |
| **Debian APT** | `apt-hosted` (dist `sophon`, auto-generated OpenPGP RSA signing keypair) | `apt-debian-proxy` (`deb.debian.org/debian`), `apt-debian-security-proxy` (`security.debian.org/debian-security`) | `/repository/apt-hosted/`, `/repository/apt-debian-proxy/`, `/repository/apt-debian-security-proxy/` *(NXRM3 APT has no group format)* |
| **Maven** | `maven-releases`, `maven-snapshots` | `maven-central` (`https://repo1.maven.org/maven2/`) | `maven-public` (`https://nexus.<domain>/repository/maven-public/`) |
| **Rust (Cargo)** | `cargo-hosted` | `crates-io-proxy` (`https://index.crates.io/`) | `cargo-group` (`https://nexus.<domain>/repository/cargo-group/`) *(best-effort if enabled in runtime edition)* |
| **Generic / Raw** | `raw-vault` (`ALLOW_ONCE` write-once), `raw-hosted` (`ALLOW`); plus unbacked-up `raw-scratch` (`scratch` blob store, `ALLOW`) | — | `raw-group` (`https://nexus.<domain>/repository/raw-group/` unifying `raw-vault`, `raw-hosted`, `raw-scratch`) |

---

## 5. Pure GitOps CI/CD & Automated Restore-Drill Workflow

### 5.1 Separation of CI (Gitea Runners) and CD (Arcane Pull GitOps)
1. **Isolated CI (`gitea/act_runner`):**
   - Runs as a rootless container workload (ideally on a dedicated builder VM or isolated rootless container without InfraVM's `podman.sock`).
   - Pulls base images and OS/language dependencies through `https://registry.<domain>` and `https://nexus.<domain>/repository/...` (warming `proxy-cache` automatically).
   - Pushes built application images with immutable tags (`:<git-sha>`) to `https://registry-push.<domain>` and packages to `*-hosted` repositories using a scoped LDAP/Nexus CI credential.
2. **Pure GitOps Repository (`ops/workloads` in Gitea):**
   - A repository qualifies as **Pure GitOps** when every runtime input needed to run the stack (`compose.yaml`, pinned image tags `registry.<domain>/app/web:<sha>`, Traefik labels, and `sophon.backup.*` labels) is committed declaratively in Git.
   - After pushing an image to `registry-push.<domain>`, the CI workflow commits the new immutable image digest/tag to `ops/workloads` in `https://git.<domain>` and triggers Arcane's zero-parameter **Inbound Webhook** (`POST https://arcane.<domain>/api/webhooks/trigger/<token>`).
   - Arcane pulls the commit from `https://git.<domain>` and runs `compose up -d`, pulling the image from `https://registry.<domain>` using InfraVM's pre-configured `sophon-pull` registry credentials.
   - Imperative `arcane-cli` deployments authenticated via Keycloak OIDC Federated Credentials remain available as an escape hatch for dynamic ephemeral preview stacks or externally injected runtime secrets, without ever storing long-lived admin keys in Gitea.

### 5.2 Deterministic Workload Reconciliation During `standby_restore` Drills
Because `arcane` is a reconstructible management stack (`sophon.backup.enable=false`), a newly provisioned drill VM (`provision-dev` / `sophon-dev-infravm`) starts with a clean `arcane_data` volume while `gitea` and `nexus` are restored from the latest Kopia snapshot. To ensure restore drills validate both Core and pure GitOps workloads safely:

1. **Local Split-Horizon Isolation (`extra_hosts` & CoreDNS):**
   - `arcane`, `gitea`, and `nexus` include `extra_hosts` entries mapping `auth.<domain>`, `ldap.<domain>`, `git.<domain>`, `nexus.<domain>`, `registry.<domain>`, and `registry-push.<domain>` to `infravm_ip` (the drill VM's IP during `standby_restore`, rewritten to the production IP by `5-activate.yml` if `sophon-activate` is ever run).
   - This guarantees that when Arcane on `sophon-dev-infravm` syncs `https://git.<domain>/ops/workloads.git` and pulls `https://registry.<domain>/...`, it talks **exclusively to the local restored Gitea and Nexus on the drill VM**, never reaching out to production.
2. **Production Safety Guards:**
   - `cloudflared` is not deployed (`SOPHON_PREPARE_RESTORE=true`), so public Cloudflare DNS and tunnels continue pointing to production.
   - Kopia mounts the SFTP repository with the confined read-only `sophon-restore` SSH key and `KOPIA_READONLY=true`.
   - CI runners are never auto-started on a `standby_restore` node so drill VMs cannot poll or steal jobs from production Gitea.
3. **Optional Declarative GitOps Reconciler (`sophon-reconcile-gitops`):**
   - A guest helper `/usr/local/bin/sophon-reconcile-gitops` (invoked automatically by `sophon-autodeploy` after `sophon-restore` when `ops/workloads` exists in local Gitea, or manually by operators) authenticates to local Arcane using `arcane_admin_password`, registers `registry.<domain>` (`sophon-pull`), registers any `compose.yaml` stack directories discovered in local `https://git.<domain>/ops/workloads.git` (skipping stacks carrying `x-sophon-standby: skip` such as `gitea-runner`), triggers GitOps sync, and restores any Kopia backup groups belonging to those reconciled workloads (`sophon-restore --group <workload>`).

---

## 6. Implementation Map

1. **[`roles/openldap/templates/docker-compose.yml.j2`](../roles/openldap/templates/docker-compose.yml.j2)** — Seed `cn=nexus-admins` and `cn=nexus-developers` under `ou=groups,<base_dn>`.
2. **[`roles/nexus/`](../roles/nexus)** —
   - `defaults/main.yml`: Pin `sonatype/nexus3:3.98.1` and `postgres:18.6-alpine3.24`, ports (`8081`, `5000`, `5001`), JVM flags, blob-store paths, and OpenLDAP settings.
   - `templates/docker-compose.yml.j2`: `nexus` + `nexus-db` with `sophon.backup.*` v3 labels, `NEXUS_SECRETS_KEY_FILE`, Traefik routers (`nexus`, `registry`, `registry-push`), and `extra_hosts`.
   - `files/configure_nexus.py`: Pure-Python idempotent configurator (admin password rotation, CE EULA acceptance, anonymous disablement, SSL truststore import, OpenLDAP server + role mappings + realms, `sophon-pull` reader account, blob stores, cleanup of default `default`-blobstore repos, and creation of Docker/PyPI/npm/APT/Maven/Cargo/Raw hosted/proxy/group repositories including pure-Python OpenPGP RSA signing key generation for `apt-hosted`).
   - `tasks/main.yml`: Pre-create `/nexus-data` directories + `nexus.secrets.json`, deploy Compose stack, wait for `/service/rest/v1/status`, run `configure_nexus.py` (when not `prepare_restore`), and write Podman/Docker `auth.json` for `registry.<domain>`.
3. **[`roles/gitea/templates/docker-compose.yml.j2`](../roles/gitea/templates/docker-compose.yml.j2)** — Set `GITEA__packages__ENABLED=false`.
4. **[`roles/arcane/templates/docker-compose.yml.j2`](../roles/arcane/templates/docker-compose.yml.j2)** — Add `git.<domain>`, `nexus.<domain>`, `registry.<domain>`, and `registry-push.<domain>` to `extra_hosts`.
5. **Core Playbooks, Bundle & Sizing** —
   - [`roles/prepare/defaults/main.yml`](../roles/prepare/defaults/main.yml) & [`src/bundle.py`](../src/bundle.py): Bundle `docker.io/sonatype/nexus3:3.98.1` and include `roles/nexus` in `sophon-apps.iso`.
   - [`group_vars/all.yml`](../group_vars/all.yml), [`src/deployment.py`](../src/deployment.py), [`.env.example`](../.env.example): Add `SOPHON_NEXUS_ADMIN_PASSWORD` and optional `SOPHON_NEXUS_DB_PASSWORD` (defaulting to `arcane_admin_password`, matching `SOPHON_GITEA_DB_PASSWORD`), plus bump InfraVM defaults to `12288` MiB RAM and `1024` GiB thin disk (`1536` GiB thin disk on Backup VM).
   - [`4-configure.yml`](../4-configure.yml) & [`5-activate.yml`](../5-activate.yml): Deploy `nexus` after `openldap`/`gitea`, include `nexus`, `registry`, and `registry-push` in `/etc/hosts`, and rewrite `nexus` `extra_hosts` during `sophon-activate` IP promotion.
   - [`guest/sophon-autodeploy`](../guest/sophon-autodeploy), [`guest/sophon-reconcile-gitops`](../guest/sophon-reconcile-gitops), & [`src/verify_restore.py`](../src/verify_restore.py): Verify `nexus` container health and `https://nexus.<domain>/service/rest/v1/status`, write `registry.<domain>` pull auth after restore, and reconcile pure GitOps workloads when `ops/workloads` is present.
6. **Documentation & Tests** —
   - Update [`README.md`](../README.md), [`CONTEXT.md`](../CONTEXT.md), [`docs/architecture.md`](architecture.md), [`docs/decisions.md`](decisions.md), [`docs/inputs.md`](inputs.md), and [`docs/operator-guide.md`](operator-guide.md).
   - Add [`tests/test_nexus.py`](../tests/test_nexus.py) (including unit tests for `configure_nexus.py`, OpenPGP key generation, template rendering, and live `sonatype/nexus3:3.98.1` + `postgres:18.6-alpine3.24` integration verification) and update [`tests/test_versions.py`](../tests/test_versions.py), [`tests/test_openldap.py`](../tests/test_openldap.py), [`tests/test_deployment.py`](../tests/test_deployment.py), and [`tests/test_autodeploy_and_activate.py`](../tests/test_autodeploy_and_activate.py).
