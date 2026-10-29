# Inputs for each step

Nothing site-specific is baked into the reusable ISOs. Each step asks only for what it needs.
Keep every credential in a password manager: reruns need the same existing values.

## Step 1 - prepare (internet)

No inputs. Needs Docker or Podman, space for the images and ISOs, and internet.
Pass `-e prepare_force=true` after `prepare` to refresh cached downloads and force an ISO rebuild, for example:
`bash sophon-provision.sh prepare -e prepare_force=true` (or `.\sophon-provision.ps1 prepare -e prepare_force=true`;
the raw `docker run` form is in [manual-commands.md](manual-commands.md)).
A laptop that is later offline
must already have `artifacts/` and the built `sophon-tools` image from a completed run; steps 1-2 then
use only the hypervisor's management network.

## Step 2 - provision

Choose the hypervisor with `SOPHON_HYPERVISOR` (`proxmox` default, `vcenter` or `hyperv`).
Every step 1-2 variable below is also listed, with a comment, in [`.env.example`](../.env.example); copy it to `.env` and the
`sophon-provision` scripts read it.

| Proxmox | Meaning |
|---|---|
| `SOPHON_PROXMOX_HOST`, `_PORT` | API host (port 8006) |
| `SOPHON_PROXMOX_PASSWORD` | the `root@pam` password (this step only) |
| `SOPHON_PROXMOX_NODE` | the real node name, usually not `proxmox` |
| `SOPHON_PROXMOX_ISO_STORAGE`, `_DISK_STORAGE` | `local`, `local-lvm` |
| `SOPHON_VNET_INTERFACE`, `SOPHON_VNET_VLAN` | bridge (`vmbr0`) and optional VLAN tag |

| vCenter | Meaning |
|---|---|
| `SOPHON_VCENTER_URL`, `_USERNAME`, `_PASSWORD` | HTTPS URL (not a standalone ESXi) and an account that can create VMs and upload to the datastore |
| `SOPHON_VCENTER_CA_FILE` | optional independently verified CA PEM; TLS verification is never disabled |
| `SOPHON_VCENTER_DATACENTER`, `_CLUSTER`, `_DATASTORE` | existing inventory names; the datastore must be shared by the cluster |
| `SOPHON_VCENTER_NETWORK` | existing port group already carrying the guest VLAN |
| `SOPHON_VCENTER_FOLDER` | optional existing folder |

| Hyper-V | Meaning |
|---|---|
| `SOPHON_HYPERV_HOST`, `_PORT` | host reachable over WinRM; port 5985 (HTTP, NTLM message encryption) or 5986 (HTTPS) |
| `SOPHON_HYPERV_USERNAME`, `_PASSWORD` | an account in the host's Administrators group (`Administrator` by default); needs `Enable-PSRemoting` on the host once |
| `SOPHON_HYPERV_CA_FILE` | optional CA PEM that verifies the HTTPS listener (port 5986); TLS verification is never disabled |
| `SOPHON_HYPERV_SWITCH` | existing virtual switch already carrying the guest VLAN |
| `SOPHON_HYPERV_PATH` | optional folder on the host for the VM, disk and ISOs (default: the host's VM path) |
| `2-provision-hyperv-local.ps1` | same VM on the local machine as the signed-in user (no host or credentials); takes `-Switch` / `SOPHON_HYPERV_SWITCH`, `-Path` / `SOPHON_HYPERV_PATH` and the `SOPHON_INFRAVM_*` sizes |

All providers: `SOPHON_INFRAVM_NAME`, `_CORES` (8), `_MEMORY` MiB (8192), `_DISK_GB` (100).
A VM that already exists is only started if this workflow created it; anything else is left alone.

## Step 3 - install (automatic)

The installer ISO (the stock live ISO plus a small baked-in Ignition config, built by step 1) runs `install.sh --auto`
as soon as the deployment ISO is attached. It installs to the only blank disk (`/dev/vda` on Proxmox, `/dev/sda` on
VMware and Hyper-V). It refuses disks with partitions or filesystem signatures, so booting the ISO again after
installation never reinstalls; it rechecks that right before writing and waits 10 seconds (Ctrl-C aborts). With no
single blank disk, `--auto` stops with a message; run `sudo bash <deployment-iso-mount>/install.sh` to be asked which
disk to use. No network settings or password are baked in.

On the first boot of the installed system `sophon-set-password.service` asks for the `core` console password on tty1,
before the login prompt (the account stays locked until then). The VM starts on DHCP.

## Step 4 - configure (console)

`sophon-deploy` first asks for the **network**, unless one is already saved: the static IPv4 address, prefix
length, gateway and DNS server. Address, prefix and gateway show the live DHCP value in square brackets (Enter accepts
it); DNS defaults to `1.1.1.1` and the DHCP-provided server is printed above the prompt for reference. It applies the
address with NetworkManager and saves the address and DNS in `site.json`, so the console wizard does not ask again.
The default address is usually a DHCP lease: reserve it on your DHCP server or type a static address, because it is the
address every service name resolves to. The DNS server is CoreDNS's upstream for every name outside your domain, so give
it a real resolver (router or public). Do not use a machine that forwards its own DNS to this VM: the Hyper-V Default
Switch address (the PC's ICS proxy) does that as soon as you point the PC at the VM, and lookups loop.
`sophon-deploy --network` asks again later.

Then it asks **whether the site is air-gapped** (default no) and whether to **use one password for all
service accounts** (default no), then asks for:

| Input | Notes |
|---|---|
| Domain | A public domain whose DNS is on Cloudflare (needed for Let's Encrypt DNS-01 when connected). |
| ACME email | Connected sites only. A real address. |
| Cloudflare token | Connected sites only, required. Zone:Read, DNS:Edit (+ Tunnel:Edit if you use tunnels), scoped to one zone. |
| Cloudflare tunnel name | Connected sites only (default `sophon`). |
| Pre-issued certificates file | Air-gapped sites only, required: the `acme.json` from the secret `SOPHON_CERTS` disk (found automatically; press Enter) or another path under `/workspace`; see [certificates.md](certificates.md). |
| Passwords | Portainer admin (12+ characters), Keycloak admin and database, Portainer OIDC client secret, Gitea admin, LDAP admin and two seed users; Gitea database defaults to the Portainer password. Typing shows `*`. "Same password for all" (12+ characters, confirmed) fills every one of these except the Cloudflare token and licence key. |
| Portainer licence | Business Edition key (the current free program covers three nodes). |

Required values are asked again until given. Settings already exported as `SOPHON_*` variables are used without asking
(passwords are skipped silently). `sophon-deploy --run` does the same non-interactively.

Service names (`traefik`, `portainer`, `dns`, `ldap`, `auth`, `git` under your domain) must
resolve to InfraVM for your clients: point DNS or a split-DNS rule at CoreDNS on InfraVM.

## Upgrading from the previous pins

The 2026-10-28 pins target **fresh installations**. Existing VMs are not upgraded just by preparing new ISOs;
provision still leaves their installed system alone. Back up application data and verify restoration before
replacing any stack or image.

* **Portainer 3.0.0 is STS, not LTS.** Its image was published October 28, ahead of the October 29 release-note
  date. Docker/Podman environments remain available, but 3.x's new features focus on Kubernetes. Back up the
  Portainer data volume before trying it; do not assume an upgraded database can be opened by 2.x.
  New instances require a one-time setup token. `sophon-deploy` reads it from the local Portainer logs and
  passes it only to step 4, by environment variable, without printing or saving it. The runner sends it as
  `X-Setup-Token` when creating the first administrator; initialized instances do not use it.
* **PostgreSQL 16 -> 18 requires migration.** Both Gitea and Keycloak now use `18.6-alpine3.24`. The supported
  PostgreSQL 18 volume mount is `/var/lib/postgresql`, not `/var/lib/postgresql/data`. Dump each old database
  with the old server, restore into a fresh PostgreSQL 18 volume, then verify the applications. Never point 18
  at a 16 data directory or delete a volume to make an upgrade start.
* **LDAP changes image families.** Bitnami's current image requires a commercial subscription; its public legacy
  image is frozen. The maintained `vegardit/openldap:2.6.10` image uses separate `openldap_database` and
  `openldap_config` volumes. The old `openldap_data` volume is mounted read-only and startup explicitly refuses
  to proceed if it contains data, rather than silently starting an empty directory.
  Export the old LDAP entries with its `slapcat`, retain an offline copy, initialize and validate a separate
  new stack, and import the directory data (not Bitnami's vendor-specific `cn=config`). Reconcile seed-entry
  collisions and verify passwords, POSIX attributes, `memberOf`, policy and Keycloak binds before cutover.
  Keep the original volume until restoration and the new deployment are verified.
* **Gitea 1.22 -> 28.2.0 and Keycloak 26.5 -> 26.8.1** may migrate their application schemas. Read their
  upstream upgrade notes and retain backups of both application and database volumes.

For a fresh deployment, first rebuild `sophon-tools` from the updated [Containerfile](../Containerfile), then
run step 1 with `-e prepare_force=true`. CoreDNS archives, CoreOS metadata and BusyBox cache paths are versioned
so the previous cached artifacts cannot satisfy these new pins.

## If something fails

Host setup: `journalctl -u sophon-host-bootstrap`, then `sudo systemctl restart sophon-host-bootstrap`
(do not reinstall). Portainer closes its first-run window after a few minutes idle; the same restart
reopens it. A disconnected guest also needs an accurate clock: certificate and OIDC checks reject
large skew, so check `date -u` and the hardware clock after a cold boot.
