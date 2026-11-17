# Inputs for each step

Nothing site-specific is baked into the reusable ISOs. Each step asks only for what it needs.
Keep every credential in a password manager: reruns need the same existing values.

## On the laptop: prepare (internet)

No inputs. Needs Docker or Podman, space for the images and ISOs, and internet.
Pass `-e prepare_force=true` after `prepare` to refresh cached downloads and force an ISO rebuild, for example:
`bash sophon-provision.sh prepare -e prepare_force=true` (or `.\sophon-provision.ps1 prepare -e prepare_force=true`;
the raw `docker run` form is in [manual-commands.md](manual-commands.md)).
A laptop that is later offline
must already have `artifacts/` and the built `sophon-tools` image from a completed run; steps 1-2 then
use only the hypervisor's management network.

## On the laptop: provision

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

The Proxmox API certificate is not verified (it is usually self-signed). The VM uses the default SeaBIOS firmware,
one virtio disk and NIC, the installer ISO on `ide2`, the deployment ISO on `ide3` and a serial socket; the console in
the web UI (noVNC) shows step 3 and the `core` password prompt.

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

## Automatic install (nothing to type)

The installer ISO (the stock live ISO plus a small baked-in Ignition config, built by step 1) runs `install.sh --auto`
as soon as the deployment ISO is attached. It installs to the only blank disk (`/dev/vda` on Proxmox, `/dev/sda` on
VMware and Hyper-V). It refuses disks with partitions or filesystem signatures, so booting the ISO again after
installation never reinstalls; it rechecks that right before writing and waits 10 seconds (Ctrl-C aborts). With no
single blank disk, `--auto` stops with a message; run `sudo bash <deployment-iso-mount>/install.sh` to be asked which
disk to use. No network settings or password are baked in.

On the first boot of the installed system `sophon-set-password.service` asks for the `core` console password on tty1,
before the login prompt (the account stays locked until then). The VM starts on DHCP.

## On the VM console: sophon-deploy

`sophon-deploy` first asks for the **network**, unless one is already saved: the static IPv4 address, prefix
length, gateway and DNS server. Address, prefix and gateway show the live DHCP value in square brackets (Enter accepts
it); DNS defaults to `1.1.1.1` and the DHCP-provided server is printed above the prompt for reference. It applies the
address with NetworkManager and saves the address and DNS in `site.json`, so the console wizard does not ask again.
The default address is usually a DHCP lease: reserve it on your DHCP server or type a static address, because it is the
address every service name resolves to. The DNS server is CoreDNS's upstream for every name outside your domain, so give
it a real resolver (router or public). Do not use a machine that forwards its own DNS to this VM: the Hyper-V Default
Switch address (the PC's ICS proxy) does that as soon as you point the PC at the VM, and lookups loop.
`sophon-deploy --network` asks again later.

Then it asks **whether the site has internet access** (default yes) and whether to **use one password for all
service accounts** (default no), then asks for:

| Input | Notes |
|---|---|
| Domain | A public domain whose DNS is on Cloudflare (needed for Let's Encrypt DNS-01 when connected). |
| ACME email | Connected sites only. A real address. |
| Cloudflare token | Connected sites only, required. A custom token with three permissions: Zone / Zone / Read, Zone / DNS / Edit and Account / Cloudflare Tunnel / Edit (tunnel permission only if you use tunnels), scoped to your zone and account. Steps: [README](../README.md#cloudflare-api-token). |
| Cloudflare tunnel name | Connected sites only (default `sophon`). `sophon-deploy` uses the tunnel with this name (Cloudflare Zero Trust > Networks > Tunnels) or creates it, then publishes the service names on it and runs its `cloudflared` connector next to Traefik, so the services are reachable from the internet. Any other tunnel in the account is left alone, so enter the name of an existing tunnel to reuse it. If the lookup or creation fails, nothing is published and no connector starts. |
| Pre-issued certificates file | Air-gapped sites only, required: the `acme.json` from the secret `SOPHON_CERTS` disk (found automatically; press Enter) or another path under `/workspace`; see [certificates.md](certificates.md). |
| Passwords | Arcane admin (12+ characters), Keycloak admin and database, Arcane OIDC client secret, Gitea admin, LDAP admin and two seed users, and Kopia UI; Gitea database defaults to the Arcane password. Typing shows `*`. "Same password for all" (12+ characters, confirmed) excludes the Cloudflare token, existing Kopia repository password and SSH material. Configure generates and reuses Arcane's local SQLite encryption key under `credentials/arcane/` automatically. |
| Kopia | External SFTP host/user/path, repository password and backup policy; Configure generates the client key and confirms host fingerprints. |

Required values are asked again until given. Settings already exported as `SOPHON_*` variables are used without asking
(passwords are skipped silently). `sophon-deploy --run` does the same non-interactively.

Service names (`traefik`, `arcane`, `dns`, `kopia`, `ldap`, `auth`, `git` under your domain) must
resolve to InfraVM for your clients: point DNS or a split-DNS rule at CoreDNS on InfraVM.

### Kopia backup inputs

Kopia deploys last, after core source volumes exist. Its backup operation needs
no Arcane/Gitea token, stack definitions or environment capture. The collector
uses a restricted engine proxy and explicitly mounted read-only source volumes.
Keep stack definitions in IaC and supplied secrets outside Arcane.

These are Configure inputs on InfraVM, not laptop `.env` settings:

| Variable | Meaning |
|---|---|
| `SOPHON_KOPIA_PASSWORD` | Required repository encryption password, new or existing; never replaced by "same password for all". Retain outside the repository. |
| `SOPHON_KOPIA_SERVER_USERNAME`, `_SERVER_PASSWORD` | Independent UI login; username defaults to `admin`. |
| `SOPHON_KOPIA_SFTP_HOST`, `_SFTP_PORT` | Required reachable SFTP host; port defaults to `22`. |
| `SOPHON_KOPIA_SFTP_USERNAME`, `_SFTP_PATH` | Required account and absolute repository path, without `..` components. |
| `SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64` | Optional single-line base64 unencrypted private-key override. Otherwise Configure generates/reuses project `credentials/kopia/id_ed25519`. Encoding is not encryption. |
| `SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64` | Optional single-line base64 of independently verified `known_hosts` for unattended pairing. Include all offered keys, with `[host]:port` for non-22 ports. Overrides cannot replace saved trust. |
| `SOPHON_KOPIA_CREATE_REPOSITORY` | Optional unattended creation confirmation (`true`); default `false`. Used only for an exact missing repository on an unused path, never an existing repository or other error. No reset/rerun step is needed. |
| `SOPHON_KOPIA_CLIENT_USERNAME`, `_CLIENT_HOSTNAME` | Stable snapshot source identity, default `backup@sophon`. Preserve it on reruns/recovery. |
| `SOPHON_KOPIA_EXTRA_VOLUMES` | Site workload volume names as a JSON array, default `[]`. Core roles declare their own list. These become read-only mounts, not host paths. Retain the list in site IaC and redeploy after changes. |
| `SOPHON_KOPIA_BACKUPS_ENABLED` | `false` until a first full backup is verified. Set `true` to enable scheduled backups; keep the existing value on reruns. |
| `SOPHON_KOPIA_SNAPSHOT_INTERVAL` | Default `6h`; positive integer with `s`, `m`, `h` or `d` suffix. The container supervisor schedules collector runs, not raw filesystem snapshots. |
| `SOPHON_KOPIA_COARSE_ENABLED` | Optional last-resort weekly live Btrfs subvolume snapshot layer, default `false`, independent of the primary schedule. |
| `SOPHON_KOPIA_COARSE_VOLUMES_DIR` | Host rootless Podman Btrfs `volumes` subvolume (default `/var/home/core/.local/share/containers/storage/volumes`), mounted read-only. |
| `SOPHON_KOPIA_COARSE_WORK_DIR` | Disjoint host Btrfs working directory outside the volumes subvolume (default `/var/home/core/sophon-coarse`). Holds temporary read-only snapshot staging, config/cache/logs. |

Keep supplying the same destination, password, source identity and policy
when reconfiguring an existing instance. The role reuses the `kopia` stack and
its v3 config/cache/log/staging volumes; preparing new media alone changes no
installed VM. An unreachable repository or invalid key/password fails startup.
There is no collector API key to supply, persist or rotate. Revoke a retired
legacy collector key after finishing legacy recovery and quiescing its schedule.

Before Configure, provide a confined SFTP account and an existing writable
directory on your backup server. Configure prints a ready-to-paste public
`authorized_keys` line, scans the server and asks you to independently verify
every SHA256 fingerprint. Install the public key on the server and continue.
The key/pins stay in project `credentials/kopia`, never in the backup or ISO.
After a rebuild, authorize a new generated client key; retain the repository
password, not a recovery copy of a replaceable SSH key.

Preflight checks reachability, authentication, write/read/delete and available
space before any stack deployment. Servers without the statvfs extension report
space as unavailable; authentication, permission or other query failures remain
fatal. Reported free space is not a guarantee that future backups fit.

Only Kopia's exact missing-repository result on an empty path (or its own verified
`.shards` layout marker) offers creation. Wrong passwords, changed host keys,
unrelated files and every other error fail closed. `sophon-deploy --run` needs
saved trust or explicit verified pin overrides, and
`SOPHON_KOPIA_CREATE_REPOSITORY=true` to confirm first creation. Existing
repositories never create again, even if that confirmation remains exported.
The old `SOPHON_KOPIA_INIT_REPOSITORY` flag is not supported.

Coarse snapshots use a separate Kopia source and keep four snapshots. Enabling
takes an atomic read-only Btrfs subvolume snapshot immediately with zero container
downtime, then schedules attempts every seven days. Non-Btrfs or non-subvolume
sources fail explicitly; there is no plain-copy fallback. See the
[last-resort procedure](../roles/kopia/README.md#optional-weekly-coarse-layer).

Changed host keys are a hard failure, including when a new pin is supplied.
After independently checking an intentional server rotation, remove only that
endpoint's hash-named `.known_hosts` file under `credentials/kopia`, then pair
again. Do not delete the whole directory or blindly accept a scan.

The [operator guide](../roles/kopia/README.md) defines v3 labels, console backup
commands, partial-run reports and interruption recovery. No v1/v2 labels or
old manifests are interpreted. [Migrate outside workloads](kopia-v3-migration.md)
before expecting them to be protected. Configure never restores state.

### Console recovery inputs

`sophon-restore plan --group GROUP --snapshot latest` uses the existing `kopia`
container's connection settings, or the same `SOPHON_KOPIA_*` destination,
password/key/pins and stable source identity overrides above when it is absent.
It never uses Arcane or Gitea. Supply `SOPHON_RESTORE_SOURCE_VOLUME` when the
backup container is absent (normally `kopia_source-v3`, retained to coordinate
backup/recovery), and optionally `SOPHON_KOPIA_RESTORE_IMAGE` for an already loaded
custom recovery image. `--state-volume NAME` selects a private labelled staging
volume; default `sophon-restore-v3`.

Apply requires the planned group, exact printed snapshot ID and `--approve`.
The fresh deployment must use matching image IDs and current native import
declarations. Failed groups are not restored from an older snapshot implicitly.
Keep decrypted staging private and run `cleanup --approve` after success or
deliberate abandonment. See the [recovery procedure](../roles/kopia/README.md#explicit-group-restoration).

## Upgrading from the previous pins

The 2026-10-28 pins target **fresh installations**. Existing VMs are not upgraded just by preparing new ISOs;
provision still leaves their installed system alone. Back up application data and verify restoration before
replacing any stack or image.

* **Arcane replaces Portainer (clean break).** Deployment now writes declarative Compose stacks to
  `/var/lib/sophon/stacks/` and reconciles them directly over the rootless Podman socket. Arcane (`v3.0.1`)
  provides the open-source container and Compose GUI at `https://arcane.<domain>` with native Keycloak OIDC
  RBAC (`arcane-admins` -> `role_admin`). Replace `SOPHON_PORTAINER_ADMIN_PASSWORD` and
  `SOPHON_KEYCLOAK_PORTAINER_CLIENT_SECRET` with `SOPHON_ARCANE_ADMIN_PASSWORD` and
  `SOPHON_KEYCLOAK_ARCANE_CLIENT_SECRET`; `SOPHON_PORTAINER_LICENSE_KEY` and `SOPHON_PORTAINER_SETUP_TOKEN`
  are retired.
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
(do not reinstall). A disconnected guest also needs an accurate clock: certificate and OIDC checks reject
large skew, so check `date -u` and the hardware clock after a cold boot.
