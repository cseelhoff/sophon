# Known gaps

Places where the code does not match the design recorded in
[docs/adr/](adr/). Each entry names the file and the ADR it contradicts.

This is a living list. Fix an item, delete the entry.

## Security

**Credentials were committed to `README.md` and remain in git history.**
A Proxmox root password, a Cloudflare API token, account/zone/tunnel IDs, a
cloudflared tunnel token, a Portainer licence key and two admin passwords were
present in the tracked README. They have been removed from `HEAD`, but removal
does not undo disclosure — every one of those secrets needs rotating, and the
tunnel needs deleting and recreating.

Root cause: there was nowhere for real values to live, so they went into the
one file everybody edits. ADR-0008 is the fix.

## Artifacts

**`prestage_container_images` and the `*_image_tars` lists are kept in sync by
hand.** [roles/prestage/defaults/main.yml](../roles/prestage/defaults/main.yml)
decides what gets pulled and saved; each role's `defaults/main.yml` decides what
gets loaded. Nothing checks that they agree. Adding a service means editing
both, and forgetting the prestage side is only caught at deploy time by the
`stat` check in
[roles/portainer_stack/tasks/main.yml](../roles/portainer_stack/tasks/main.yml).
— ADR-0005

**Image tar filenames are derived by string surgery.** Both prestage and the
role defaults reconstruct `registry-repo_tag.tar` from the image reference
independently. A reference shaped differently from the others — a bare
`postgres:16-alpine`, say, or a digest pin — produces two different filenames
and the mismatch shows up as a missing file. — ADR-0005

**Legacy NFS-only role: the fuse mount is never unmounted.** [roles/nfs/tasks/main.yml](../roles/nfs/tasks/main.yml)
creates a tempdir, mounts into it, copies, and leaves it mounted for the life
of the run. The current installation copies artifacts from the Sophon ISO to
InfraVM's local disk and does not invoke this role.

## Address discovery

**Legacy vnet discovery installs `arp-scan` on the Proxmox node at runtime.**
[roles/vnet/tasks/main.yml](../roles/vnet/tasks/main.yml) runs
`apt-get update -qq && apt-get install -qq -y arp-scan` on the hypervisor. This
requires internet access on the Proxmox host, mutates the hypervisor as a side
effect of an unrelated play, and swallows its own failure with `;` and
`2>/dev/null` — so on a disconnected host `arp-scan` is silently absent and
discovery silently returns nothing. The current installer prompts for explicit
network settings at the live console and does not invoke this role.
— ADR-0001, ADR-0013

**`proxmox_airgapped` is declared but never branched on.**
[roles/proxmox/defaults/main.yml](../roles/proxmox/defaults/main.yml) defines
it, and no task reads it. Remove it. — ADR-0001

## Generated state

Passwords and the Portainer OIDC client secret are explicit environment inputs,
validated before deployment. Old `artifacts/secrets/` files are no longer read
or written; existing deployments must
[migrate their existing values](preconditions.md#existing-deployments).

**`artifacts/` is not a Kopia source.**
It may hold legacy SSH keys and other private deployment state, and nothing
backs it up. Backups are deferred in the current two-ISO workflow.
— ADR-0008, ADR-0009, ADR-0013

## Service state

**Legacy Kopia configuration snapshots an empty tree.**
The `/var/mnt/nfs/backups/<service>/` dump contract is documented in
[roles/kopia/README.md](../roles/kopia/README.md) and implemented by no service.
Gitea, OpenLDAP and Postgres each need a dump sidecar. If invoked, the legacy
backup system can report success while protecting nothing. The normal workflow
does not deploy Kopia; backup design is deferred. — ADR-0009, ADR-0013

## Naming

**Diagrams carry a deployment-specific label.**
[sophon.drawio.xml](../sophon.drawio.xml) and
[sophon.drawio.svg](../sophon.drawio.svg) contain `177CPT` labels.

## Networking and DNS

**CoreDNS forwards to `9.9.9.9` and `1.1.1.1`.**
[roles/coredns/templates/docker-compose.yml.j2](../roles/coredns/templates/docker-compose.yml.j2)
hardcodes public resolvers. With no egress every non-local lookup stalls until
timeout. `vnet_dns` is now an explicit bootstrap input and is the correct
forwarder. — ADR-0006

**No NTP source on InfraVM.**
Fedora CoreOS with no egress cannot reach the default NTP pool and will drift.
ACME rejects requests with significant clock skew, and certificate validity
checks fail on a drifted host. Point `chronyd` at the Proxmox host or a local
source. — ADR-0006

## Portainer

**`portainer_license_key` defaults to empty on a Business Edition image.**
[group_vars/all.yml](../group_vars/all.yml) pulls
`docker.io/portainer/portainer-ee` with no licence. The Keycloak integration
depends on `OAuthAutoMapTeamMemberships` and `AdminGroupClaimsRegexList`, which
Community Edition accepts and silently ignores — producing SSO logins with no
privileges and no error. Make the licence key a hard requirement. — ADR-0010

## Dead code

- [roles/openldap/tasks/main.yml](../roles/openldap/tasks/main.yml) has a
  commented-out task.
- CoreDNS and Traefik compose templates have commented-out health checks.
