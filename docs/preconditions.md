# Preconditions

What must be true for the two-phase deployment. Bootstrap and service deployment
have different access and credential requirements.

## Bootstrap laptop

Enter `nix develop`, then start `python3 deployment.py --phase bootstrap --serve`.
Connected preparation requires internet, sufficient space for both ISOs,
container archives, the runner image and the ISO, and working image-building
tools. Provisioning afterward uses only the selected hypervisor's management HTTPS.

Supply Proxmox management access, node, ISO/disk storage, bridge/VLAN and VM size.
Alternatively, select vCenter and supply its HTTPS URL, account, datacenter,
cluster, shared datastore, and an existing port group. Its VLAN is configured
on the port group, not through Proxmox's bridge/VLAN inputs.
No guest IP, console password, SSH key or domain is needed on the laptop.
Fresh deployments are supported. Conflicting unmarked InfraVMs are preserved
and rejected; compatible marked VMs are started without rewriting their disks.

## InfraVM console

Use the hypervisor console to run the installer from the Sophon ISO in stock
CoreOS live media. Choose a fresh blank disk and supply static networking and a
local console password for `core`. Neither SSH nor network access from the
laptop to the guest is required. After reboot log in as `core` and run
`sophon-controller`; the local runner needs no hypervisor access.

| Requirement | Why |
|---|---|
| Offline installation completed | CoreOS and the project, collections, archives and runner are on persistent disk; both ISOs may be detached |
| Portainer reachable on `<infravm_ip>:9443` | Phase 2 uses Portainer, not a hypervisor API |
| Pre-issued certificates imported when there is no egress | Run `python3 certificates.py issue` on the connected machine; paste its base64 payload in Step 4. Private certificate ISO transport remains an alternative. Keep the Cloudflare token on the connected machine |
| Service names resolve to `infravm_ip` in the runner | Container-only host entries supply this before CoreDNS starts; Keycloak and Traefik checks use service FQDNs — see [ADR-0007](adr/0007-controller-resolves-service-names-through-coredns.md) |

Phase 2 asks for the domain; bootstrap and dashboard startup do not require it.
Before each service run, the playbook updates container-only host entries for the domain apex and
`traefik`, `auth`, `ldap`, `git`, `portainer`, and `dns` service names,
pointing them at InfraVM. This avoids a first-run DNS dependency before CoreDNS
starts, without changing the host's resolver. Other clients still
need appropriate DNS. The CoreDNS role checks the runner's service-name
resolution and reports a mismatch rather than silently using a public address.

The live-console installer configures the static address and upstream DNS.
External service-name resolution and operator access remain operator-managed.
No Tailscale subnet advertisement or other connectivity method is installed.

## Proxmox host

| Requirement | Notes |
|---|---|
| API reachable over HTTPS | `proxmox_host`, `proxmox_port` (default 8006) |
| Root credentials | `proxmox_password` — API authentication and ISO uploads, Phase 1 only |
| Correct node name | `proxmox_node` defaults to `proxmox`; on most installs it is not that |
| A storage for ISOs and one for VM disks | `proxmox_iso_storage_id` (default `local`), `proxmox_disk_storage_id` (default `local-lvm`) |
| A bridge, and a VLAN if you use one | `vnet_interface` (default `vmbr0`), `vnet_vlan` |

No QEMU Guest Agent, hypervisor shell execution, fw_cfg delivery or guest SSH
access is needed. Phase 2 is local to InfraVM.

InfraVM has both the normal graphical console and a serial socket. The installer
configures both consoles on the installed system. The unchanged live ISO may
require starting `sudo systemctl start serial-getty@ttyS0.service` from its
graphical console before serial-based test automation can log in.

`proxmox_validate_certs` defaults to `false`. If your Proxmox has a trusted
certificate, set it to `true`.

## VMware vCenter

Select `SOPHON_HYPERVISOR=vcenter`; Proxmox remains the default.
`nix develop` supplies VMware's `govc` CLI.

| Requirement | Notes |
|---|---|
| vCenter reachable by HTTPS | `SOPHON_VCENTER_URL`; direct standalone ESXi endpoints are rejected |
| Account with VM, network and datastore permissions | `SOPHON_VCENTER_USERNAME`, `SOPHON_VCENTER_PASSWORD`; Phase 1 only |
| Trusted vCenter certificate chain | System trust or a separately verified `SOPHON_VCENTER_CA_FILE` PEM; verification is never disabled |
| Existing datacenter and cluster | `SOPHON_VCENTER_DATACENTER`, `SOPHON_VCENTER_CLUSTER` |
| Datastore accessible to cluster hosts | `SOPHON_VCENTER_DATASTORE`; holds content-addressed ISOs and a fresh thin disk |
| Existing standard or distributed port group | `SOPHON_VCENTER_NETWORK`; select a port group already configured for the guest VLAN |
| Optional existing VM inventory folder | `SOPHON_VCENTER_FOLDER`; empty uses the datacenter's VM root |

The bootstrap laptop must also reach the datastore upload endpoints selected
by vCenter. No VMware guest tools or guest networking are required for the
normal live-console installer. It supports whole VMware disks such as
`/dev/sda`, not only Proxmox's `/dev/vda`.

## Addresses

The InfraVM address is an input at the live console, not a discovery. Choose
it from a part of the vnet that your DHCP server will not hand out
([ADR-0011](adr/0011-addresses-are-operator-supplied.md)).

| Variable | What it is |
|---|---|
| `infravm_ip` | Static address for `sophon-infravm`; also the address CoreDNS serves on and the address every service FQDN resolves to |

Gateway, prefix length and upstream DNS are explicit live-console inputs.
Installed network defaults supply `SOPHON_INFRAVM_IP` and `SOPHON_VNET_DNS`
to the service runner. NFS addressing and backup credentials are not required.

## Domain and certificates

This is the precondition that catches people out.

`domain_name` must be a **publicly registered domain whose DNS is managed by
Cloudflare**. Not `homelab.local`, not `sophon.internal`, not a domain parked
at another registrar's nameservers.

Supply it in the console wizard, optional dashboard, or as `SOPHON_DOMAIN`
in the runner's environment. It is not bundled into either ISO.
Portainer's hostname routing is configured when Traefik is deployed in Phase 2;
its initial API access uses the InfraVM IP and port 9443.

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

Accurate UTC on InfraVM matters: certificate, LDAPS, OIDC, and ACME checks
reject excessive clock skew. A disconnected guest needs a trustworthy local
time source or an accurately set hardware clock. Check again after a cold
boot: hypervisor/guest-tool synchronization can reintroduce an incorrect host
time. Do not bypass certificate verification to work around a clock error.

## Portainer licence

`portainer_license_key` is required. The deployment runs Portainer **Business
Edition** (`docker.io/portainer/portainer-ee`), and the Keycloak integration
uses `OAuthAutoMapTeamMemberships` and `AdminGroupClaimsRegexList`, which
Community Edition does not implement — it accepts the settings and silently
ignores them, leaving every SSO user unprivileged.

Portainer's free Business Edition licence covers a small number of nodes and is
sufficient. See [ADR-0010](adr/0010-portainer-is-the-deployment-substrate.md).

## Prestage

`artifacts/` must be populated before provisioning. `bootstrap.yml` runs
connected preparation automatically, including the offline runner, collections,
the full CoreOS live ISO, static installer helper, and Sophon deployment ISO.

If the bootstrap laptop is disconnected, `ansible-playbook prestage.yml` must
have completed beforehand. The media are reusable across sites. Phase 2 uses
the writable project installed at `/var/lib/sophon/project`, not laptop files.
Provisioning itself fetches nothing
([ADR-0001](adr/0001-air-gapped-a-supported-deployment-mode.md)). See
[prestage.md](../prestage.md) for the runbook.

## Credentials

Supply management credentials only on the bootstrap laptop, and the independent
`core` console password only at installation. Enter service credentials on
InfraVM; they are not placed on
the deployment ISO. `site.yml` validates service credentials before deployment.
Empty or whitespace-only values fail with the environment-variable name;
the Portainer admin password must contain at least 12 characters.

| Required environment variable | Ansible variable |
|---|---|
| `SOPHON_PROXMOX_PASSWORD` (Proxmox Phase 1 only) | `proxmox_password` |
| `SOPHON_VCENTER_PASSWORD` (vCenter Phase 1 only) | `vcenter_password` |
| `SOPHON_PORTAINER_ADMIN_PASSWORD` | `portainer_admin_password` |
| `SOPHON_KEYCLOAK_ADMIN_PASSWORD` | `keycloak_admin_password` |
| `SOPHON_KEYCLOAK_DB_PASSWORD` | `keycloak_db_password` |
| `SOPHON_KEYCLOAK_PORTAINER_CLIENT_SECRET` | `keycloak_portainer_client_secret` |
| `SOPHON_GITEA_ADMIN_PASSWORD` | `gitea_admin_password` |
| `SOPHON_OPENLDAP_ADMIN_PASSWORD` | `openldap_admin_password` |
| `SOPHON_OPENLDAP_EXAMPLE_USER_PASSWORD` | `openldap_example_user_password` |
| `SOPHON_OPENLDAP_EXAMPLE_ADMIN_PASSWORD` | `openldap_example_admin_password` |

The optional `SOPHON_GITEA_DB_PASSWORD` overrides its Ansible variable.
When unset or empty, it reuses the supplied Portainer admin password.
Set it explicitly if the deployed
values differ. LDAP config and bind credentials retain their existing reuse of
the LDAP admin password unless overridden with Ansible variables.

Keep the durable copy in your password manager. Environment variables transport
credentials; they do not survive the shell or container being replaced. Prefer
your password manager's environment injection, or a hidden shell prompt, rather
than putting literal passwords into shell history. For example, in Bash:

```bash
read -rsp 'Existing Gitea admin password: ' SOPHON_GITEA_ADMIN_PASSWORD; printf '\n'
export SOPHON_GITEA_ADMIN_PASSWORD
```

Repeat for each required credential, or use the console wizard/dashboard. Dashboard
values are not saved between browser reloads. Passwords are no longer generated
or saved as reusable controller secrets; Ansible Vault is not required.
Deployed service configuration and logs may still contain credentials.

### Existing deployments

Before rerunning, move the **existing values** from the old
`artifacts/secrets/<ansible-variable>` files into your password manager and
supply them through the matching environment variables above. Read these files
privately as their owner; do not paste them into chat, commits, or shared logs.
Use any existing explicit overrides instead of stale saved values.

The old files are left untouched but are no longer read. Nothing is automatically
rotated or migrated. In particular, preserve the Keycloak database password,
Portainer OIDC client secret, Gitea database password, and Kopia repository
password. If Gitea/Kopia previously reused the Portainer password, keep that same
value or supply their previous values as explicit overrides. Changing an input
is not a supported password-rotation procedure for an existing service.

Once all values are safely retained and verified, the old password files can be
retired separately. Do not delete all of `artifacts/`: it still contains needed
certificates and other private deployment state.

## Summary: the two entry points

On the bootstrap laptop:

```bash
nix develop
python3 deployment.py --phase bootstrap --serve
```

Use Configure/Run for Phase 1, then the hypervisor live console:

```bash
sudo mkdir -p /mnt/sophon
sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

After installation/reboot, log in as `core` and run `sophon-controller`.
The wizard requests service settings and confirms before running Ansible.
For exported service settings, use `sophon-controller --run`; the optional
web interface is `sophon-controller --dashboard`. Do not run these concurrently.
`SOPHON_KEYCLOAK_REALM` defaults to `sophon`; required passwords have no
generated fallback. See [the quickstart](../README.md#quickstart) for details.

Do not paste credentials into a tracked file. Retain them in your password
manager and supply them to the environment of the invocation.
