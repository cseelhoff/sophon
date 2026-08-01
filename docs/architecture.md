# Architecture

How Sophon's pieces fit together, and what breaks what.

See [CONTEXT.md](../CONTEXT.md) for the vocabulary used here.

## The three machines

```
Controller                Proxmox host              vnet
(your workstation)  ───▶  (hypervisor)      ┌──▶ sophon-nfs    (Alpine)
                                            └──▶ sophon-infravm (Fedora CoreOS)
```

**Controller** is wherever you run `ansible-playbook`. Every play in this repo
targets `localhost` with `ansible_connection: local`; nothing is done over SSH
to a managed node. All remote work happens through APIs, which means the
Controller needs *network* reach to three things: the Proxmox API, the NFS
export, and the Portainer API on InfraVM.

**sophon-nfs** is a small Alpine VM built locally by
[nfs_vm_build/build-alpine-image](../nfs_vm_build/build-alpine-image). It exports
`/export` and does four jobs at once:

| Path | Consumer |
|---|---|
| `/export/template/iso` | Proxmox, as storage `sophon-nfs` — VM disk images |
| `/export/containers` | InfraVM, at boot — container image tars |
| `/export/<service>` | Services, as bind-mounted data |
| `/export/kopia/repo` | Kopia, over SFTP as user `kopia` |

The export is squashed to `anonuid=1000,anongid=100`, matching the `core` user
on InfraVM, so rootless containers can write to it.

**sophon-infravm** is Fedora CoreOS. It has no package manager step and no
configuration management agent — its entire configuration is baked into an
Ignition file, produced from [infravm.bu.j2](../roles/infravm/templates/infravm.bu.j2)
by Butane and handed to QEMU through `-fw_cfg`. After first boot it runs
rootless Podman as `core` (uid 1000) with lingering enabled, so containers
survive logout, and with `net.ipv4.ip_unprivileged_port_start=0` so an
unprivileged user can bind `:53`, `:80` and `:443`.

## Three transports, no SSH

Sophon reaches remote systems three different ways, and knowing which one is in
play explains most failure modes.

**1. The Proxmox REST API.** Used by the `proxmox`, `nfs` and `infravm` roles
for VM lifecycle, storage registration and image upload. Authentication is a
ticket plus a CSRF token, held for the run.

**2. Shell on the Proxmox node, via [library/proxmox_shell.py](../library/proxmox_shell.py).**
A custom module that runs commands *on the hypervisor itself* over the
termproxy WebSocket, or inside a VM through the QEMU Guest Agent. This is how
the NFS VM gets its root password, network config and directory layout before
it has any network identity of its own. It is powerful and unsandboxed —
anything it runs, runs as root on the hypervisor.

**3. The Portainer API.** Once InfraVM is up, Portainer is the *only* channel
Ansible uses to affect it. Stacks are created and updated through
`/api/stacks`; volumes are seeded through Portainer's Docker-API proxy at
`/api/endpoints/<id>/docker`. There is no SSH path to InfraVM in any role.

That last point is a real architectural constraint, not an implementation
detail — see [ADR-0010](adr/0010-portainer-is-the-deployment-substrate.md). If
Portainer is down, Ansible cannot fix it; you fix Portainer by hand and then
re-run.

## Prestage and Deploy

```
 ── Prestage (needs internet) ──────────────────────────────────────
   skopeo copy   ──▶ artifacts/containers/*.tar
   docker build  ──▶ artifacts/containers/coredns-dockerdiscovery.tar
   libguestfs    ──▶ artifacts/alpine-nfs.qcow2

 ── Deploy (needs nothing external) ────────────────────────────────
   artifacts/  ──fuse-nfs──▶  /export/containers  ──podman load──▶ InfraVM
```

Prestage writes everything into `artifacts/`. Deploy copies it onto the NFS
export — the Controller mounts `/export` with `fuse-nfs`, which is vendored
from source in [flake.nix](../flake.nix) precisely so this works without root.
InfraVM's `sophon-init.service` then `podman load`s every tar it finds under
`/var/mnt/nfs/containers` before any stack starts.

Compose files reference images by the exact tag Prestage saved, and rely on
those tars being present. See
[ADR-0005](adr/0005-artifacts-reach-the-site-over-a-fuse-mounted-nfs-export.md).

## Deployment order and why it is that order

`site.yml` runs these roles in sequence:

```
proxmox → nfs → infravm → coredns → traefik → openldap → keycloak → gitea → kopia
```

The first three build the substrate. The last six are a dependency chain where
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
listener is disabled in the Ignition config for the same reason.

Containers talking to each other do *not* go through CoreDNS — aardvark-dns
handles that, and it forwards anything it does not own to CoreDNS. So CoreDNS
is not "just a nice-to-have zone server"; it is the host resolver, aardvark's
upstream, and the thing that makes every FQDN in the chain above resolvable.

CoreDNS also runs a custom build — `coredns-dockerdiscovery`, built during
Prestage — which watches container labels and does two things with them:
publishes zone records, and synchronises Cloudflare tunnel ingress rules.

The Controller has to resolve `<domain>` too, and it must resolve it to
`infravm_ip` rather than to a tunnel hostname, or the Keycloak configuration
step will be talking to the wrong endpoint. See
[ADR-0007](adr/0007-controller-resolves-service-names-through-coredns.md).

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
| **Generated** | passwords, allocated IPs, SSH keys | `artifacts/`, on the Controller |
| **Reconstructible** | Keycloak realm config, Traefik routers | Re-run the playbook |
| **Irreplaceable** | Gitea repositories, LDAP entries, `acme.json` | Kopia snapshot |

Kopia backs up `/source` — the `kopia_backups` volume — to an encrypted
repository reachable over SFTP on the NFS VM at `/export/kopia/repo`. Services
holding irreplaceable state are expected to write dumps into
`/var/mnt/nfs/backups/<service>/` for Kopia to pick up.

Note that the backup target lives on the NFS VM, on the same hypervisor. That
protects against service-level loss, not against losing the Proxmox host.

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
