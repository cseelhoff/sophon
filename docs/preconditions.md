# Preconditions

What must be true before `ansible-playbook site.yml` will succeed. Most of
these are not checked, and most produce a confusing failure several roles later
rather than an immediate one.

## Controller

The machine you run Ansible from.

| Requirement | Why |
|---|---|
| `nix develop` shell entered | Provides Ansible, `skopeo`, `butane`, `libguestfs`, `fuse-nfs` at the versions the playbooks expect |
| Galaxy collections installed | `ansible-galaxy install -r requirements.yml` |
| An SSH public key at `~/.ssh/id_rsa.pub` | Baked into InfraVM's Ignition as `infravm_ssh_public_key`; without it you have no console-free recovery path |
| Routed L3 access to the vnet | The Controller talks directly to the NFS export and to Portainer on `<infravm_ip>:9443`. There is no bootstrap tunnel — see [ADR-0002](adr/0002-controller-requires-direct-vnet-access.md) |
| DNS resolution for `<domain>` pointing at CoreDNS on `infravm_ip` | Keycloak configuration and Traefik checks run against `https://auth.<domain>` etc. from the Controller — see [ADR-0007](adr/0007-controller-resolves-service-names-through-coredns.md) |

The DNS requirement is circular on a first run: CoreDNS does not exist until
the `coredns` role has run. That role therefore ends with a preflight that
resolves `traefik.<domain>` through the Controller's own resolver and asserts
the answer is `infravm_ip`. On a first run it stops there with an actionable
message; point the resolver at `infravm_ip` (or add the service names to
`/etc/hosts`) and re-run. Deploy is idempotent, so the second run picks up
where the first stopped.

Sophon never edits the Controller's network configuration. Checking is the
role's job; changing it is the operator's.

## Proxmox host

| Requirement | Notes |
|---|---|
| API reachable over HTTPS | `proxmox_host`, `proxmox_port` (default 8006) |
| Root credentials | `proxmox_password` — used for both the API and shell-on-node execution |
| Correct node name | `proxmox_node` defaults to `proxmox`; on most installs it is not that |
| A storage for ISOs and one for VM disks | `proxmox_iso_storage_id` (default `local`), `proxmox_disk_storage_id` (default `local-lvm`) |
| A bridge, and a VLAN if you use one | `vnet_interface` (default `vmbr0`), `vnet_vlan` |
| QEMU Guest Agent usable | The NFS VM is configured entirely through QGA before it has a network identity |

`proxmox_validate_certs` defaults to `false`. If your Proxmox has a trusted
certificate, set it to `true`.

## Addresses

Two addresses are inputs, not discoveries. Choose them from a part of the vnet
that your DHCP server will not hand out
([ADR-0011](adr/0011-addresses-are-operator-supplied.md)).

| Variable | What it is |
|---|---|
| `nfs_ip` | Static address for `sophon-nfs` |
| `infravm_ip` | Static address for `sophon-infravm`; also the address CoreDNS serves on and the address every service FQDN resolves to |

Gateway, prefix length and upstream DNS are derived from the Proxmox network
configuration and do not normally need supplying.

## Domain and certificates

This is the precondition that catches people out.

`domain_name` must be a **publicly registered domain whose DNS is managed by
Cloudflare**. Not `homelab.local`, not `sophon.internal`, not a domain parked
at another registrar's nameservers.

Traefik obtains certificates from Let's Encrypt using the DNS-01 challenge,
which requires writing a TXT record into the real public zone. A private TLD
cannot be validated, and without valid certificates the LDAPS → Keycloak → OIDC
chain does not come up.

You therefore also need:

| Input | Requirement |
|---|---|
| `traefik_acme_email` | A real address; Let's Encrypt sends expiry warnings there |
| `coredns_cf_token` | Cloudflare API token with **Zone → DNS → Edit** on the zone, and tunnel configuration rights if you use tunnels |
| `coredns_cf_zone_id` | The zone's ID |
| `coredns_cf_account_id` | The account's ID |
| `coredns_cf_tunnel_id` | Only if using Cloudflare tunnels |

Scope the token to the single zone. It can rewrite your public DNS.

Accurate time on both the Proxmox host and InfraVM also matters: ACME rejects
requests with significant clock skew, and Fedora CoreOS with no egress has no
NTP source.

## Portainer licence

`portainer_license_key` is required. The deployment runs Portainer **Business
Edition** (`docker.io/portainer/portainer-ee`), and the Keycloak integration
uses `OAuthAutoMapTeamMemberships` and `AdminGroupClaimsRegexList`, which
Community Edition does not implement — it accepts the settings and silently
ignores them, leaving every SSO user unprivileged.

Portainer's free Business Edition licence covers a small number of nodes and is
sufficient. See [ADR-0010](adr/0010-portainer-is-the-deployment-substrate.md).

## Prestage

`artifacts/` must be populated before any provisioning happens. `site.yml` does
this for you by running the `prestage` role first, so a Controller with internet
access needs nothing extra.

If the Controller is disconnected, `ansible-playbook prestage.yml` must have
been run successfully on a machine with internet access and `artifacts/` copied
across. Deploy itself fetches nothing
([ADR-0001](adr/0001-air-gapped-a-supported-deployment-mode.md)). See
[prestage.md](../prestage.md) for the runbook.

## Summary: a complete invocation

```bash
ansible-playbook site.yml \
  -e proxmox_host=10.0.60.1 \
  -e proxmox_port=8006 \
  -e proxmox_node=pve-01 \
  -e proxmox_password=<proxmox root password> \
  -e proxmox_iso_storage_id=local \
  -e domain_name=example.com \
  -e nfs_ip=10.0.60.10 \
  -e infravm_ip=10.0.60.11 \
  -e vnet_interface=vmbr1 \
  -e vnet_vlan=60 \
  -e traefik_acme_email=admin@example.com \
  -e coredns_cf_token=<cloudflare api token> \
  -e coredns_cf_zone_id=<zone id> \
  -e coredns_cf_account_id=<account id> \
  -e portainer_license_key=<licence key>
```

Anything not supplied that Sophon can generate — service passwords in
particular — is generated and written under `artifacts/`, then reused on
subsequent runs. Values passed with `-e` always win and are never written to
disk ([ADR-0008](adr/0008-generated-state-persists-under-artifacts.md)).

Do not paste these values into a tracked file. Use a shell history-ignored
invocation, an environment file outside the repo, or a vault.
