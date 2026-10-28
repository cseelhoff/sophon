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

## Step 3 - install (console)

`install.sh` installs to the only blank disk (`/dev/vda` on Proxmox, `/dev/sda` on VMware and Hyper-V), found
automatically; if there is none or several it asks which. It then asks for the static IPv4 address, prefix length,
gateway and DNS server, each showing the live system's current value in square brackets: press Enter to accept it or
type a different one. Finally it asks for a new password for the `core` console user. It refuses disks with
partitions or filesystem signatures, rechecks that right before writing, and waits 10 seconds (Ctrl-C aborts).
The address and DNS values are saved, so `sophon-controller` (step 4) does not ask for them again. The default address
is usually a DHCP lease: reserve it on your DHCP server or type a static address, because it is the address every
service name resolves to.

## Step 4 - configure (console)

`sophon-controller` first asks **whether the site is air-gapped** (default no) and whether to **use one password for all
service accounts** (default no), then asks for:

| Input | Notes |
|---|---|
| Domain | A public domain whose DNS is on Cloudflare (needed for Let's Encrypt DNS-01 when connected). |
| ACME email | Connected sites only. A real address. |
| Cloudflare token | Connected sites only, required. Zone:Read, DNS:Edit (+ Tunnel:Edit if you use tunnels), scoped to one zone. |
| Cloudflare tunnel name | Connected sites only (default `sophon`). |
| Pre-issued certificates file | Air-gapped sites only, required: the `acme.json` from the secret `SOPHON_CERTS` disk (found automatically; press Enter) or another path under `/workspace`; see [certificates.md](certificates.md). |
| Passwords | Portainer admin (12+ characters), Keycloak admin and database, Portainer OIDC client secret, Gitea admin, LDAP admin and two seed users; Gitea database defaults to the Portainer password. Typing shows `*`. "Same password for all" (12+ characters, confirmed) fills every one of these except the Cloudflare token and licence key. |
| Portainer licence | Business Edition key (a free 5-node starter licence works). |

Required values are asked again until given. Settings already exported as `SOPHON_*` variables are used without asking
(passwords are skipped silently). `sophon-controller --run` does the same non-interactively.

Service names (`traefik`, `portainer`, `dns`, `ldap`, `auth`, `git` under your domain) must
resolve to InfraVM for your clients: point DNS or a split-DNS rule at CoreDNS on InfraVM.

## If something fails

Host setup: `journalctl -u sophon-host-bootstrap`, then `sudo systemctl restart sophon-host-bootstrap`
(do not reinstall). Portainer closes its first-run window after a few minutes idle; the same restart
reopens it. A disconnected guest also needs an accurate clock: certificate and OIDC checks reject
large skew, so check `date -u` and the hardware clock after a cold boot.
