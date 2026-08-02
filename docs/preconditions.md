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
| `coredns_cf_token` | Cloudflare API token with **Zone → Zone → Read**, **Zone → DNS → Edit**, and **Account → Cloudflare Tunnel → Edit** if you use tunnels |
| `coredns_cf_tunnel_id` | Only if using Cloudflare tunnels |

Scope the token to the single zone. It can rewrite your public DNS.

The zone ID and account ID are **not** inputs. The `coredns` role reads them
back from the API using the token, so a zone that gets recreated cannot leave a
stale ID behind. Setting `coredns_cf_zone_id` explicitly still works and is
checked against the live zone.

`Zone → Read` is easy to leave off and the failure is confusing: the token
works everywhere else but lists no zones, so lego cannot resolve the zone ID and
reports `zone could not be found`. A token scoped to a zone that was later
deleted — what a lapsed and re-registered domain leaves behind — fails the same
way, because the replacement zone has a different ID. The `coredns` role
preflights both cases and prints the remediation.

Do not validate the token with `GET /user/tokens/verify`. Account-owned tokens
(the `cfat_` format) are rejected there with `Invalid API Token` while working
normally against every other endpoint.

`Zone → DNS → Edit` is checked separately, by writing and deleting a throwaway
`_sophon-preflight` TXT record. Read access is not enough and the difference is
expensive: without write access every DNS-01 challenge fails with
`Authentication error (10000)`, and five failures for one hostname earns an
hour-long Let's Encrypt lockout for that name.

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

Every site setting is read from the environment, so describe the site once:

```bash
export SOPHON_PROXMOX_HOST=10.0.60.1
export SOPHON_PROXMOX_PORT=8006
export SOPHON_PROXMOX_NODE=pve-01
export SOPHON_PROXMOX_PASSWORD='<proxmox root password>'
export SOPHON_PROXMOX_ISO_STORAGE=local
export SOPHON_DOMAIN=example.com
export SOPHON_NFS_IP=10.0.60.10
export SOPHON_INFRAVM_IP=10.0.60.11
export SOPHON_VNET_INTERFACE=vmbr1
export SOPHON_VNET_VLAN=60
export SOPHON_VNET_GATEWAY=10.0.60.1
export SOPHON_VNET_CIDR=24
export SOPHON_ACME_EMAIL=admin@example.com
export SOPHON_CF_TOKEN='<cloudflare api token>'
export SOPHON_PORTAINER_LICENSE_KEY='<licence key>'
```

Then deploy, and redeploy, with:

```bash
ansible-playbook site.yml
```

The optional extras are `SOPHON_PORTAINER_ADMIN_PASSWORD`,
`SOPHON_KEYCLOAK_ADMIN_PASSWORD` and `SOPHON_KEYCLOAK_REALM`; leave them unset
to get generated passwords and the default realm.

Splitting the run this way is not just ergonomics. `site.yml` has no memory of
which subnet a deployment lives on, so a single vnet flag dropped from a long
`-e` line silently retargets the whole deploy at a different network. Exporting
the set once removes that failure mode from every subsequent run.

`-e` still takes precedence over the environment for one-off overrides.

Anything not supplied that Sophon can generate — service passwords in
particular — is generated and written under `artifacts/`, then reused on
subsequent runs. Values passed with `-e` always win and are never written to
disk ([ADR-0008](adr/0008-generated-state-persists-under-artifacts.md)).

Do not paste these values into a tracked file. Use a shell history-ignored
invocation, an environment file outside the repo, or a vault.
