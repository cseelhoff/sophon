# Inputs for each step

Nothing site-specific is baked into the reusable ISOs. Each step asks only for what it needs.
Keep every credential in a password manager: reruns need the same existing values.

## Step 1 - prepare (internet)

No inputs. Needs Docker or Podman, space for the images and ISOs, and internet.
Add `-e prepare_force=true` after `prepare` in the step 1 command from the README to refresh cached downloads, for example:
`docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock sophon-tools prepare -e prepare_force=true`.
A laptop that is later offline
must already have `artifacts/` and the built `sophon-tools` image from a completed run; steps 1-2 then
use only the hypervisor's management network.

## Step 2 - provision

Choose the hypervisor with `SOPHON_HYPERVISOR` (`proxmox` default, `vcenter` or `hyperv`) or in the dashboard.

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

All providers: `SOPHON_INFRAVM_NAME`, `_CORES` (8), `_MEMORY` MiB (8192), `_DISK_GB` (100).
A VM that already exists is only started if this workflow created it; anything else is left alone.

## Step 3 - install (console)

`install.sh` asks for: the blank target disk (`/dev/vda` on Proxmox, `/dev/sda` on VMware and Hyper-V),
a static IPv4 address and prefix, the gateway, an upstream DNS server, and a new password for the
`core` console user. It refuses non-blank disks and requires typing `INSTALL <disk>`.
The blank-disk check reads the whole disk and can take many minutes.
Pick an address outside the DHCP pool; it is the address every service name resolves to.

## Step 4 - configure (console)

`sophon-controller` asks for:

| Input | Notes |
|---|---|
| Domain | A public domain whose DNS is on Cloudflare (needed for Let's Encrypt DNS-01). |
| ACME email | A real address. |
| Cloudflare token | Optional offline. Zone:Read, DNS:Edit (+ Tunnel:Edit if you use tunnels), scoped to one zone. |
| Pre-issued certificates | Optional pasted payload for sites without internet; see [certificates.md](certificates.md). |
| Passwords | Portainer admin (12+ characters), Keycloak admin and database, Portainer OIDC client secret, Gitea admin, LDAP admin and two seed users; Gitea database defaults to the Portainer password. |
| Portainer licence | Business Edition key (a free 5-node starter licence works). |

`sophon-controller --run` does the same non-interactively from exported `SOPHON_*` variables.
`sophon-controller --dashboard` serves the optional progress UI on loopback.

Service names (`traefik`, `portainer`, `dns`, `ldap`, `auth`, `git` under your domain) must
resolve to InfraVM for your clients: point DNS or a split-DNS rule at CoreDNS on InfraVM.

## If something fails

Host setup: `journalctl -u sophon-host-bootstrap`, then `sudo systemctl restart sophon-host-bootstrap`
(do not reinstall). Portainer closes its first-run window after a few minutes idle; the same restart
reopens it. A disconnected guest also needs an accurate clock: certificate and OIDC checks reject
large skew, so check `date -u` and the hardware clock after a cold boot.
