# Architecture

How Sophon's pieces fit together, and what breaks what.

See [CONTEXT.md](../CONTEXT.md) for the vocabulary used here.

## Bootstrap and the site-local runner

```
Bootstrap laptop -- management HTTPS --> Proxmox or VMware vCenter
                                         `-- sophon-infravm (blank disk, two ISOs)

Live console --> offline CoreOS install --> installed InfraVM
                                            |-- rootless Portainer
                                            `-- rootless controller --> Portainer HTTP
```

Every play targets `localhost`. The bootstrap laptop prepares artifacts, then
uses only the selected hypervisor's management API to create a blank InfraVM disk, attach the unchanged
full CoreOS live ISO plus the Sophon deployment ISO, and start the VM.
It never mounts NFS, runs a hypervisor shell, injects QEMU Ignition, or contacts
Portainer. Conflicting existing guests are preserved and rejected; a compatible
two-ISO VM is only started, not reinstalled.

The operator runs the installer from the live console, confirms a blank target,
and supplies static networking and the independent `core` console password.
The installer verifies the media, installs CoreOS offline and copies artifacts
into persistent storage. After reboot the host service starts rootless Portainer.
The operator runs `sophon-controller`, a local Python/Ansible Podman runner with the
writable project mounted at `/workspace`. Its task graph and environment exclude
hypervisor credentials and lifecycle operations. See
[ADR-0013](adr/0013-two-iso-infravm-installation.md).

| Path | Consumer |
|---|---|
| `/var/lib/sophon/project` | Writable project, image archives and collections on installed InfraVM |
| `/var/lib/sophon/runner.tar` | Offline runner archive on installed InfraVM |
| `/workspace` | The project's mount inside the rootless runner |

Other service images are sent through Portainer. Application data resides in
InfraVM-local named volumes, not generic NFS service directories.

There is no required NFS VM or export. Backup infrastructure and Kopia are
deferred to a later opt-in workflow. Both ISOs can be detached after installation.

**sophon-infravm** is Fedora CoreOS. The live installer creates local first-boot
Ignition from console inputs and supplies it to `coreos-installer`, not the
hypervisor. The installed host service performs minimal host setup and image
loads; the controller has no privileged mode, Podman socket or host-root mount.
Portainer alone receives the rootless socket. After first boot InfraVM runs
rootless Podman as `core` (uid 1000) with lingering enabled, so containers
survive logout, and with `net.ipv4.ip_unprivileged_port_start=0` so an
unprivileged user can bind `:53`, `:80` and `:443`.

## Two APIs and a local console, no SSH

Sophon provisions and configures systems through these surfaces; knowing which is in
play explains most failure modes.

**1. The selected hypervisor's API.** Used only during bootstrap for
authentication, VM lifecycle and ISO upload. Proxmox uses a REST ticket plus a
CSRF token held for the run. VMware vCenter uses `govc` with verified HTTPS,
environment-only credentials and no persistent session cache. vCenter placement
uses an existing datacenter, cluster, shared datastore and port group.

**2. Local console installation.** A script on the Sophon ISO installs CoreOS
from full upstream media onto a confirmed blank disk and writes first-boot
configuration and persistent deployment files. No guest networking is needed
to enter the console and no remote shell is used by the normal flow.

**3. The Portainer API.** Once InfraVM is up, Portainer is the *only* channel
Ansible uses to affect it. Stacks are created and updated through
`/api/stacks`; volumes are seeded through Portainer's Docker-API proxy at
`/api/endpoints/<id>/docker`. There is no SSH path to InfraVM in any role.

That last point is a real architectural constraint, not an implementation
detail — see [ADR-0010](adr/0010-portainer-is-the-deployment-substrate.md). If
Portainer is down, Ansible cannot fix it; you fix Portainer by hand and then
re-run.

## Artifact transport

```
 ── Prestage (needs internet) ──────────────────────────────────────
   podman pull   ──▶ artifacts/containers/*.tar
   podman build  ──▶ artifacts/containers/coredns-dockerdiscovery.tar
   download      ──▶ artifacts/coreos/fedora-coreos-live.iso
   nix / galaxy  ──▶ offline runner, static helper and collections
   allowlist     ──▶ artifacts/bootstrap/sophon-deployment.iso

 ── Bootstrap (management only) ───────────────────────────────────
   two ISOs ── hypervisor virtual CDs ──▶ blank InfraVM
   live installer ──▶ persistent CoreOS, project, archives ── reboot
   host bootstrap ──▶ rootless Portainer and offline runner image
   private certificate ISO ── installed console ──▶ carried ACME identity state

 ── Services (local InfraVM runner) ───────────────────────────────
   local artifacts/ ── Portainer /docker/images/load ──▶ remaining services
```

Prestage is `roles/prestage`, included by `bootstrap.yml` before anything is
provisioned and wrapped by `prestage.yml` for standalone use. It stats every
artifact it owns, verifies the cached CoreOS ISO and rebuilds the local bundle.
With a complete valid cache it does not access the network or rebuild the
runner/helper. Carry the matching project and complete `artifacts/` cache to a
disconnected laptop. See
[ADR-0001](adr/0001-air-gapped-a-supported-deployment-mode.md).

Prestage writes everything into `artifacts/`. Deploy uses two transports,
split by bootstrap order:

- **Portainer's own image tar** is copied from the Sophon ISO to persistent
  InfraVM storage, then loaded by the host bootstrap. Portainer cannot deliver itself.
- **Every other image** is POSTed to Portainer's Docker-API proxy at
  `/api/endpoints/<id>/docker/images/load` immediately before the stack that
  needs it. This runs on every play, so bumping a tag or rebuilding an image
  takes effect without rebuilding the VM.

The second transport lives in `roles/portainer_stack`: each role passes a
`*_image_tars` list, the role `stat`s every path and fails naming the missing
file before it touches the stack. Compose files set `pull_policy: never`, so a
gap in the artifact set cannot be papered over by a silent registry pull. See
[ADR-0013](adr/0013-two-iso-infravm-installation.md).

## Deployment order and why it is that order

`bootstrap.yml` and `site.yml` have separate task graphs:

```
Bootstrap: prestage → hypervisor authentication → ownership check → blank InfraVM + two ISOs
Console:   offline installation → reboot → host bootstrap → controller wizard
Services:  Portainer → coredns → traefik → openldap → keycloak → gitea
```

Bootstrap builds the substrate. The service roles are a dependency chain where
each link is load-bearing for everything after it:

```
CoreDNS resolves *.<domain> ──▶ Traefik routes by Host header
                             ──▶ ACME DNS-01 issues valid certs
                             ──▶ LDAPS to ldap.<domain> verifies
                             ──▶ Keycloak federates OpenLDAP
                             ──▶ Gitea and Portainer trust Keycloak OIDC
```

Break DNS and you do not get "DNS is broken" — you get Keycloak failing to
start because it cannot verify an LDAPS certificate whose name it cannot
resolve. Read failures in this chain from the left.

## DNS: two resolvers, one port

This is the part that most often gets misread.

Port 53 on InfraVM is shared by two different resolvers with two different
jobs:

| Listener | Who runs it | Answers for |
|---|---|---|
| `10.89.0.1:53` | **aardvark-dns**, Podman's own resolver | Container-to-container names on the Podman network |
| `127.0.0.1:53` and `<infravm_ip>:53` | **CoreDNS** | The `<domain>` zone, for the host and the LAN |

CoreDNS deliberately binds only those two addresses so it does not collide with
aardvark-dns on the bridge address. Fedora CoreOS's systemd-resolved stub
listener is disabled by installed host configuration for the same reason.

Containers talking to each other do *not* go through CoreDNS — aardvark-dns
handles that, and it forwards anything it does not own to CoreDNS. So CoreDNS
is not "just a nice-to-have zone server"; it is aardvark's upstream and makes
every service FQDN resolvable inside the shared network. Host bootstrap
creates or updates that network's DNS setting to InfraVM's address; the host
itself retains the installation's upstream resolver.

CoreDNS also runs a custom build — `coredns-dockerdiscovery`, built during
Prestage — which watches container labels and does two things with them:
publishes zone records, and synchronises Cloudflare tunnel ingress rules.

The Controller has to resolve `<domain>` too, and it must resolve it to
`infravm_ip` rather than to a tunnel hostname, or the Keycloak configuration
step will be talking to the wrong endpoint. After the operator supplies the
domain in Phase 2, the service playbook refreshes the domain apex and known
service names in a marked block inside the runner's `/etc/hosts`, so its first run
does not depend on CoreDNS already being available. The host resolver and
other clients remain unchanged. See
[ADR-0007](adr/0007-controller-resolves-service-names-through-coredns.md).

The bootstrap ISO and InfraVM boot configuration do not carry the domain.
Traefik's Phase 2 labels declare Portainer's hostname router, preserving DNS
discovery, while its file provider supplies the backend `http://portainer:9000`
on the shared proxy network. The separate Portainer API on InfraVM port 9443
remains available before Traefik is deployed.
Traefik retains Podman's resolver for its container-name backends. Only its
ACME DNS-01 checks use the explicitly configured public resolvers.

## Certificates

Traefik obtains certificates from Let's Encrypt using the **DNS-01** challenge
against Cloudflare. DNS-01 is what makes this work for a homelab: no inbound
HTTP reachability is required, only the ability to write a TXT record.

Consequences worth internalising:

- `domain_name` must be a **real, publicly registered domain in Cloudflare**.
  `.local` and `.internal` cannot be validated.
- A Cloudflare API token with zone DNS edit rights must be available to
  Traefik.
- Certificates are issued per router, one name each — no wildcards, no SAN
  lists.
- `acme.json` holds the ACME account key and every issued private key. It lives
  in the `traefik_traefik_data` volume, is snapshotted to
  `artifacts/traefik/acme.json` after each deploy, and is restored into a fresh
  volume on the next one. Losing it and re-issuing repeatedly will hit Let's
  Encrypt's duplicate-certificate rate limit.

## Identity

```
OpenLDAP  ──user federation──▶  Keycloak  ──OIDC──▶  Gitea
                                          ──OIDC──▶  Portainer
```

OpenLDAP is the source of user records. Keycloak federates it over LDAPS and is
the only identity provider the applications know about. Keycloak is configured
after it starts, by 27 REST calls from the Controller against
`https://auth.<domain>` — which is why Controller DNS resolution and valid
certificates are preconditions for identity working at all, not just for
convenience.

## State and backup

Service state falls into three kinds, and the distinction decides how it is
protected ([ADR-0009](adr/0009-service-state-is-reconstructible-or-irreplaceable.md)):

| Kind | Examples | Protection |
|---|---|---|
| **Generated** | Deployment artifacts and runtime state | Persistent InfraVM project |
| **Reconstructible** | Keycloak realm config, Traefik routers | Re-run the playbook |
| **Irreplaceable** | Gitea repositories, LDAP entries, `acme.json` | Operator-managed backup; Kopia is deferred |

Passwords are operator inputs, supplied through the environment or Ansible
overrides and retained in the operator's password manager. They are not generated
or persisted as controller password files; see
[credentials](preconditions.md#credentials).

The retained legacy Kopia/NFS roles are not part of the normal task graph and
do not constitute configured backup protection. A later opt-in backup workflow
must address dumps, repository credentials and failure-domain separation.

## Egress

The site is expected to have internet access, and two features depend on it:

- **ACME renewal** — Traefik reaching Let's Encrypt and Cloudflare.
- **Cloudflare tunnels** — remote access to services, with ingress rules
  maintained by CoreDNS from container labels.

Without egress, everything else runs unchanged on the LAN; those two degrade.
Certificates are then carried in by hand
([runbook](runbooks/sneakernet-refresh.md)). This is a supported degraded mode,
not the primary target — see
[ADR-0006](adr/0006-site-egress-is-expected-airgap-is-a-degraded-mode.md).

## Where the code disagrees with this document

Several parts of the above describe the intended design rather than current
behaviour. [known-gaps.md](known-gaps.md) lists every divergence.
