# Architecture

How the pieces fit and what breaks what. Vocabulary is in [../CONTEXT.md](../CONTEXT.md).

## Shape

```text
laptop ── hypervisor API ──▶ blank InfraVM + installer and deployment ISOs   (steps 1-2)
installer ISO ── install.sh --auto ──▶ persistent CoreOS    (step 3, unattended)
InfraVM: rootless Podman socket + runner container ──▶ Compose stacks + Arcane GUI (step 4)
```

Every play targets `localhost`. The laptop only creates a blank VM; it never reaches into the guest
(no SSH, guest agent, NFS or disk conversion). The two media are reusable and hold no site data or secrets:
`sophon-coreos.iso` (the stock CoreOS live ISO with a small live Ignition config baked in by
`coreos-installer iso customize`) and `sophon-apps.iso` (the service project, runner image, container archives and
installer). The `core` password is chosen at the first boot, the network, domain and all service credentials in step 4.
See [decisions.md](decisions.md).

These surfaces explain deployment and backup failures:

1. **The hypervisor API** (steps 1-2 only). Proxmox: REST ticket + `community.proxmox`. vCenter: `govc`
   with verified HTTPS and environment-only credentials. Hyper-V: PowerShell over WinRM (`pypsrp`); every value
   reaches the host as base64 JSON, never inside PowerShell source. Existing VMs are only started, and only if this
   workflow marked them; anything else is left untouched.
2. **The live system** (step 3). `guest/sophon-autoinstall` (baked into the installer ISO) starts `guest/install.sh --auto` from
   the deployment ISO; it verifies the media checksums, requires exactly one unpartitioned disk with no filesystem
   signatures, renders Ignition locally and runs `coreos-installer --offline`.
3. **The rootless Podman socket & Compose substrate** (step 4). `guest/host-bootstrap.sh` starts `podman.socket`,
   loads the offline runner image and prepares `/var/lib/sophon/stacks`. The runner loads offline image archives and
   reconciles Compose projects directly over the rootless Podman socket (`roles/compose_stack`), completely decoupled
   from the management UI ([decisions.md](decisions.md)). Arcane mounts `/var/lib/sophon/stacks` and the Podman socket
   as a reconstructible OIDC-enabled GUI (`arcane.<domain>`).
4. **The backup data path** (normal container stack). Kopia mounts the Podman
   `volumes` subvolume read-only (`/volumes:ro`) so newly deployed developer
   volumes are discovered automatically from `sophon.backup.*` container labels
   without redeploying Kopia. Its private, unpublished `dump-proxy` container
   exposes only sanitized container inventory (`Config.Env` stripped) and
   label-bound dump streaming (`POST /v1/dump/<id>/<name>`)—never arbitrary
   `/exec`, `/stop`, `/start` or `/archive`. Backups are 100% hot and never stop
   containers; Arcane is absent from this path.

Before deploying any stacks, the console runner pairs/preflights the explicitly
declared SFTP destination directly. Prepare includes Kopia 0.23.1, OpenSSH client
tools and pinned Paramiko in the runner. A replaceable ed25519 client key and
confirmed host pins live under project `credentials/kopia` (mode 0700, files
0600), outside source-volume backups, Git and reusable media. Fingerprints must
be verified out of band; changed key sets fail closed. Only an exact native
missing-repository result plus confirmation can create an empty repository.
Startup itself never creates. Deployment reads only the runtime's structured
redacted failure on an unhealthy/restarting service, not raw secret-bearing logs.

## Artifacts

Step 1 writes everything into `artifacts/` and is cached: with a complete cache it touches no network.

- The runner image (`sophon:latest`) is loaded by `guest/host-bootstrap.sh` from `/var/lib/sophon/runner.tar`.
- Every service image (including Arcane) is loaded into the local Podman engine right before its Compose stack.
  Compose files use `pull_policy: never`, so a missing archive fails by name instead of pulling silently.

`prepare_container_images` in `roles/prepare/defaults/main.yml` lists upstream
images. Prepare also builds CoreDNS and the Kopia collector/proxy image from
Sophon sources. Their archives join the same manifest and deployment ISO; keep
that complete list in step with each role's `*_image_tars`. Kopia is rebuilt
through the engine's content cache and its archive is replaced when the image
ID changes, so editing the collector cannot reuse an obsolete image archive.
Bundle creation hashes the copied IaC sources into `bootstrap/sophon-version`,
which snapshots record alongside the workload image references and immutable IDs.

## Order of the services

```text
CoreDNS resolves *.<domain> ─▶ Traefik routes by Host and gets certificates
   ─▶ LDAPS at ldap.<domain> verifies ─▶ Keycloak federates OpenLDAP
   ─▶ Gitea (Git/LFS) and Nexus (OpenLDAP RBAC + hosted/proxy registries) start
   ─▶ Arcane and the LAN-only Status & Cutover UI (https://status.<domain>) trust Keycloak (OIDC)
   ─▶ Kopia connects with native actions enabled and triggers the initial hot snapshot
```

Each link is load-bearing: a DNS problem shows up as Keycloak failing on an LDAPS certificate. Read
failures from the left.

Kopia is deployed last so core services are already healthy for the initial
snapshot. Its code and image come from Sophon, not a Gitea project. Backups and
scheduling run natively inside `kopia server start --enable-actions` via Kopia's
own `--snapshot-interval` policy and `--before-snapshot-root-action=/app/backup.py`
(`--action-command-mode=essential`, `--no-persist-action-script`). Before each
snapshot of `backup@sophon:/sophon-state`, `backup.py` stages declared volume/path
reflinks, writes `.kopiaignore` exclusions, streams hot logical dumps through
`dump-proxy`, emits `KOPIA_SNAPSHOT_PATH=<staged_dir>` for Kopia to deduplicate
and upload, and cleans staging in `--after-snapshot-root-action=/app/backup.py`.
Every run report in `/source/last-run.json` records undeclared volumes. No host
backup timer or unit is created.

An opt-in `coarse` service uses the same image and requires no Podman socket or
proxy container. Step 3 formats `/var` as Btrfs (`sophon-var`, mounted with
`noatime,discard=async,user_subvol_rm_allowed`) and host bootstrap creates Podman's `volumes`
directory as a Btrfs subvolume. The `coarse` service mounts that subvolume
read-only alongside a disjoint `/var/home/sophon/sophon-coarse` working bind, with
only `DAC_OVERRIDE`. Once a week it takes an atomic read-only Btrfs subvolume
snapshot (`BTRFS_IOC_SNAP_CREATE_V2`) with zero workload pauses, uploads to its
`/sophon-coarse` source (retaining four), and destroys the local snapshot.
This crash-consistent layer includes reconstructible/raw database state
intentionally as a last-resort exception, never the default native-export restore path.

## DNS

Two resolvers share port 53 on InfraVM. Podman's aardvark-dns serves `10.89.0.1:53` for container names;
CoreDNS binds only `127.0.0.1:53` and `<infravm_ip>:53` for the zone, and anything it does not own goes to
the operator's upstream DNS (`SOPHON_VNET_DNS`, default `1.1.1.1`; it must be a real resolver and never a machine that
forwards its own DNS back to InfraVM, or lookups loop). Containers reach each other through aardvark-dns, which
forwards unknown names to CoreDNS. CoreDNS is a custom build (`coredns-dockerdiscovery`) that publishes
records from container labels and can sync Cloudflare tunnel routes; its role creates the named tunnel when the
account has none, and repoints any CNAME on a service name that still targets another tunnel (CoreDNS never edits a
record it did not create). A `cloudflared` container (the `cloudflared`
role, deployed after Traefik) is the tunnel's connector: it reads the tunnel's token from the Cloudflare API and
forwards public requests to Traefik. Without it Cloudflare answers error 1033. Before CoreDNS exists the runner maps
the service names to InfraVM in its own `/etc/hosts` ([4-configure.yml](../4-configure.yml)); the host
resolver and other clients are never changed.

An optional, separately deployed AdGuard Home stack can own LAN DNS instead.
Its `sophon.dns.frontend=adguardhome` label and host networking make Configure
retain CoreDNS on loopback TCP/UDP port 1053, with independent Quad9 upstreams.
AdGuard serves `10.1.1.3:53` and delegates the site's domain to CoreDNS; it must
not become CoreDNS's upstream or unknown local names can loop. Rootless bridge
port publishing and resolver forwarding lose original LAN client addresses,
so AdGuard must be the host-networked first hop for per-device reporting.
Its admin HTTP listener needs a persistent host firewall restriction, while
Traefik publishes HTTPS. The separate AdGuard repository carries that host setup;
AdGuard is not included in the offline ISO. With this frontend, rootless bridge
networks use Podman's host-mapped `169.254.1.2` DNS upstream: pasta clones InfraVM's
LAN address inside its namespace, where it cannot reach the real host listener.
A user containers.conf drop-in supplies that DNS default for future containers;
aardvark still handles peer names. Host bootstrap preserves the proxy upstream
instead of adding the cloned LAN IP again.

## Certificates

Traefik uses one wildcard certificate (`<domain>` and `*.<domain>`, an ECDSA P-256 key) from Let's Encrypt DNS-01
(Cloudflare) for every service, so no inbound reachability is needed. Each router names the wildcard itself, so Traefik
never requests per-service certificates. Without egress, a small `acme.json` holding just that certificate and key is
carried in on a small secret ISO ([certificates.md](certificates.md)). The certs-dumper container extracts it for LDAPS and
names the files after the certificate's main domain (`<domain>.crt`, `<domain>.key`).

## State

Reconstructible state (the Keycloak realm, seeded users, routing, Nexus upstream `proxy-cache` blob store) is rebuilt by a rerun. Irreplaceable
state (Gitea repositories and Git LFS objects, Nexus `hosted` blob store + keystores + datastore encryption key + PostgreSQL metadata, user-created LDAP entries, `acme.json`, the ACME account) is not;
back it up ([decisions.md](decisions.md), [nexus-and-gitops-plan.md](nexus-and-gitops-plan.md)). Concise `sophon.backup.*` labels declare
whole volumes (with `.kopiaignore` exclusions), selective paths, INI identity keys,
and hot native stdout dumps (`pg_dumpall`, `pg_dump -Fc -Z0`, `slapcat`)—all
captured online with zero container downtime. Raw database directories are never
copied in primary backups. Snapshots contain real files, uncompressed dumps and
a v3 manifest, never tar artifacts, container environments, Arcane state, images
or reconstructible configuration.

Arcane is recreated from IaC, not restored. Retain external inputs and the
SFTP repository password/key/pins outside the encrypted repository. Restoration
is explicit, never invoked by Configure. The console's `sophon-restore` runs a
one-shot coordinator from the offline Kopia image using Podman alone, independently
of Arcane, Gitea and the Kopia UI. It validates a snapshot/group against fresh
IaC, waits for explicit approval, stops that group (including Compose peer
services), replaces declared volume files directly via `/volumes` with numeric
ownership preserved, imports native exports using isolated matching-image
helpers, then starts producers before consumers. Failures leave the group stopped
with a durable helper journal for retry/explicit cleanup. Planning shares the
backup lock; a durable `/source/restore.json` hold prevents scheduled backups of
partial recovery state after interruption until success or explicit abandonment.
The operator coordinator temporarily gets `/volumes` and the rootless socket;
the primary `dump-proxy` remains restricted. Decrypted recovery staging lives
only in a private, labelled recovery volume and must be cleaned explicitly. Old
snapshots require `kopia-legacy`, not a v3 shim.
See the [Kopia backup & restore guide](../roles/kopia/README.md) and
[outside-workload migration](kopia-v3-migration.md).
