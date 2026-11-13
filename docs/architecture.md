# Architecture

How the pieces fit and what breaks what. Vocabulary is in [../CONTEXT.md](../CONTEXT.md).

## Shape

```text
laptop ── hypervisor API ──▶ blank InfraVM + installer and deployment ISOs   (steps 1-2)
installer ISO ── install.sh --auto ──▶ persistent CoreOS    (step 3, unattended)
InfraVM: rootless Portainer + runner container ──▶ services (step 4)
```

Every play targets `localhost`. The laptop only creates a blank VM; it never reaches into the guest
(no SSH, guest agent, NFS or disk conversion). The two media are reusable and hold no site data or secrets:
`sophon-installer.iso` (the stock CoreOS live ISO with a small live Ignition config baked in by
`coreos-installer iso customize`) and `sophon-deployment.iso` (the service project, runner image, container archives and
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
3. **The Portainer API** (step 4). Stacks, image loads and volume seeding all go through Portainer's HTTP
   API for deployment mutations ([decisions.md](decisions.md)).
   If Portainer is down, fix it by hand, then rerun.
4. **The backup data path** (normal container stack). Declared source volumes are
   read-only mounts in Kopia. Its private, unpublished HAProxy service exposes only
   list/inspect/exec/stop/start on the existing rootless Podman socket. Native dumps
   stream stdout, not archives. Portainer is absent from this path.

## Artifacts

Step 1 writes everything into `artifacts/` and is cached: with a complete cache it touches no network.

- Portainer's own image is loaded by `guest/host-bootstrap.sh` from the persistent project directory,
  because Portainer cannot deliver itself.
- Every other image is POSTed to Portainer's Docker proxy right before its stack, on every run.
  Compose files use `pull_policy: never`, so a missing archive fails by name instead of pulling silently.

`prepare_container_images` in `roles/prepare/defaults/main.yml` lists upstream
images. Prepare also builds CoreDNS and the Kopia collector image from
Sophon sources. Their archives join the same manifest and deployment ISO; keep
that complete list in step with each role's `*_image_tars`. Kopia is rebuilt
through the engine's content cache and its archive is replaced when the image
ID changes, so editing the collector cannot reuse an obsolete image archive.
The pinned HAProxy image joins the same offline manifest. Bundle creation hashes
the copied IaC sources into `bootstrap/sophon-version`, which snapshots record
alongside the workload image references and immutable IDs.

## Order of the services

```text
CoreDNS resolves *.<domain> ─▶ Traefik routes by Host and gets certificates
   ─▶ LDAPS at ldap.<domain> verifies ─▶ Keycloak federates OpenLDAP
   ─▶ Gitea and Portainer trust Keycloak (OIDC)
   ─▶ Kopia mounts the now-existing declared source volumes
```

Each link is load-bearing: a DNS problem shows up as Keycloak failing on an LDAPS certificate. Read
failures from the left.

Kopia is deployed last so external named-volume mounts already exist. Its code
and images come from Sophon, not a Gitea project. Backups and their supervisor
operate without Portainer, Gitea or the Kopia UI. The UI remains a repository
browser, not the collector trigger. A console command stages a uniquely named
immutable run, resumes writers, then uploads with a stable logical source
identity (`backup@sophon:/sophon-state`). Partial runs upload successful groups
and report failure; every report includes undeclared volumes.

The supervisor runs scheduled backups only when explicitly enabled. Its own
Kopia server child handles browsing; native snapshot scheduling stays disabled
to prevent snapshots bypassing the collector. No host backup timer or unit is
created. The primary stack never mounts the whole volumes directory.

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

Reconstructible state (the Keycloak realm, seeded users, routing) is rebuilt by a rerun. Irreplaceable
state (Gitea repositories, user-created LDAP entries, `acme.json`, the ACME account) is not;
back it up ([decisions.md](decisions.md)). V3 labels declare irreproducible paths,
native stdout dumps and stopped writers. Only declared named volumes are mounted;
native database exports do not require exposing database directories.
Snapshots contain real files, dumps and a v3 manifest, never tar artifacts,
container environments, Portainer state, images or reconstructible configuration.
The journal and per-run report are outside snapshot staging.

Portainer is recreated from IaC, not restored. Retain external inputs and the
SFTP repository password/key/pins outside the encrypted repository. Restoration
is explicit, never invoked by Configure; M2's new group restore/drill is separate
from the M1 collector. Old snapshots require `kopia-legacy`, not a v3 shim.
See the [operator guide](../roles/kopia/README.md) and
[outside-workload migration](kopia-v3-migration.md).
