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

**The fuse mount is never unmounted.** [roles/nfs/tasks/main.yml](../roles/nfs/tasks/main.yml)
creates a tempdir, mounts into it, copies, and leaves it mounted for the life
of the run.

## Address discovery

**`arp-scan` is installed on the Proxmox node at runtime.**
[roles/vnet/tasks/main.yml](../roles/vnet/tasks/main.yml) runs
`apt-get update -qq && apt-get install -qq -y arp-scan` on the hypervisor. This
requires internet access on the Proxmox host, mutates the hypervisor as a side
effect of an unrelated play, and swallows its own failure with `;` and
`2>/dev/null` — so on a disconnected host `arp-scan` is silently absent and
discovery silently returns nothing. — ADR-0001, ADR-0011

**Addresses are discovered rather than supplied.**
`nfs_ip` and `infravm_ip` should be operator inputs, prompted, validated and
persisted. A powered-off VM's address looks free to ARP scanning. — ADR-0011

**`proxmox_airgapped` is declared but never branched on.**
[roles/proxmox/defaults/main.yml](../roles/proxmox/defaults/main.yml) defines
it, `site.yml` documents it, and no task reads it. Remove it. — ADR-0001

## Generated state

Generated passwords now persist under `artifacts/secrets/` — `site.yml`'s
admin and seed-user passwords, `portainer_admin_password` from
[group_vars/all.yml](../group_vars/all.yml), `keycloak_db_password`, and the
Portainer OIDC client secret.

**`artifacts/` is not a Kopia source.**
It holds the SSH keys, ACME account key and generated credentials for the whole
deployment, and nothing backs it up. — ADR-0008, ADR-0009

## Service state

**Kopia snapshots an empty tree.**
The `/var/mnt/nfs/backups/<service>/` dump contract is documented in
[roles/kopia/README.md](../roles/kopia/README.md) and implemented by no service.
Gitea, OpenLDAP and Postgres each need a dump sidecar. As it stands the backup
system runs, reports success, and protects nothing. — ADR-0009

## Naming

**Diagrams carry a deployment-specific label.**
[sophon.drawio.xml](../sophon.drawio.xml) and
[sophon.drawio.svg](../sophon.drawio.svg) contain `177CPT` labels.

## Networking and DNS

**CoreDNS forwards to `9.9.9.9` and `1.1.1.1`.**
[roles/coredns/templates/docker-compose.yml.j2](../roles/coredns/templates/docker-compose.yml.j2)
hardcodes public resolvers. With no egress every non-local lookup stalls until
timeout. `vnet_dns` is already discovered from the Proxmox network
configuration and is the correct forwarder. — ADR-0006

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

- `infravm_cloudflared_tunnel_token` and `infravm_cloudflared_image` are
  declared in [roles/infravm/defaults/main.yml](../roles/infravm/defaults/main.yml)
  and never used. The working tunnel integration lives in the `coredns` role.
- [site.yml](../site.yml) has a commented-out `infravm_ansible_host`
  tunnel-or-IP switch.
- [roles/openldap/tasks/main.yml](../roles/openldap/tasks/main.yml) has a
  commented-out task.
- CoreDNS and Traefik compose templates have commented-out health checks.
- [site.yml](../site.yml) header comments still describe the three-phase
  `nfs-upload.yml` workflow, which does not exist.
