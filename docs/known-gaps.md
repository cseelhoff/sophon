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

## Artifacts never reach the site

**Only Portainer's image tar is copied to the NFS export.**
[roles/nfs/tasks/main.yml](../roles/nfs/tasks/main.yml) fuse-mounts `/export`
and copies `{{ portainer_image_tarname }}` alone. Every other prestaged tar
stays on the Controller. — ADR-0005

**The load-all-tars loop is commented out.**
[roles/infravm/templates/infravm.bu.j2](../roles/infravm/templates/infravm.bu.j2)'s
`sophon-init.service` loads only the Portainer tar; the loop over
`/var/mnt/nfs/containers/*.tar` is disabled. — ADR-0005

**Compose files lack `pull_policy: never`.**
Without it, Portainer pulls from the registry when a tag is not already
present locally, so a disconnected deploy fails at stack creation rather than
at the missing-artifact step. — ADR-0005

**NFS mount failure warns instead of failing.**
If the fuse mount does not come up, the play continues and the failure surfaces
several roles later as a missing image. — ADR-0005

Together these three mean the air-gapped path has never actually worked
end-to-end; connected deploys succeed because Portainer silently pulls
everything else.

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

**Secrets are regenerated on every run.**
[site.yml](../site.yml) uses
`lookup('password', '/dev/null length=24 chars=ascii_letters,digits')` for
`keycloak_admin_password`, `gitea_admin_password`, `openldap_admin_password`
and the example user passwords. `/dev/null` means nothing is stored, so every
run mints new values and the previous ones are lost — including the ones
already configured inside running services. Point the lookup at a real path
under `artifacts/`. — ADR-0008

Same pattern in [roles/infravm/tasks/main.yml](../roles/infravm/tasks/main.yml)
for `portainer_admin_password`.

**`artifacts/` is not a Kopia source.**
It holds the SSH keys, ACME account key and generated credentials for the whole
deployment, and nothing backs it up. — ADR-0008, ADR-0009

## Service state

**Keycloak runs `KC_DB=dev-mem`.**
[roles/keycloak/defaults/main.yml](../roles/keycloak/defaults/main.yml) sets an
in-memory database. The realm, clients, mappers and group mappings — 27 REST
calls' worth — vanish on container restart. Move to Postgres on the existing
volume. — ADR-0009

**Kopia snapshots an empty tree.**
The `/var/mnt/nfs/backups/<service>/` dump contract is documented in
[roles/kopia/README.md](../roles/kopia/README.md) and implemented by no service.
Gitea, OpenLDAP and Postgres each need a dump sidecar. As it stands the backup
system runs, reports success, and protects nothing. — ADR-0009

## Naming

**`keycloak_realm` defaults to `177cpt`.**
[roles/keycloak/defaults/main.yml](../roles/keycloak/defaults/main.yml) carries
a deployment-specific realm name as the project default. It should be `sophon`.

Related: [sophon.drawio.xml](../sophon.drawio.xml) and
[sophon.drawio.svg](../sophon.drawio.svg) contain `177CPT` labels.

## Networking and DNS

**CoreDNS forwards to `9.9.9.9` and `1.1.1.1`.**
[roles/coredns/templates/docker-compose.yml.j2](../roles/coredns/templates/docker-compose.yml.j2)
hardcodes public resolvers. With no egress every non-local lookup stalls until
timeout. `vnet_dns` is already discovered from the Proxmox network
configuration and is the correct forwarder. — ADR-0006

**No Controller DNS preflight.**
Nothing asserts, after the `coredns` role, that the Controller resolves
`<domain>` names to `infravm_ip`. If they resolve to a tunnel hostname instead,
Keycloak configuration talks to the wrong endpoint and fails obscurely. —
ADR-0007

**Bootstrap tunnel remnants.**
`infravm_portainer_url` in [group_vars/all.yml](../group_vars/all.yml) still
defaults to `https://portainer-bootstrap.<domain>`, and the Cloudflare
bootstrap ingress and CNAME tasks remain in
[roles/infravm/tasks/main.yml](../roles/infravm/tasks/main.yml). The Controller
is required to have direct vnet access, so this path should not exist. —
ADR-0002

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
