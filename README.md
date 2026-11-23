# Sophon

Offline-capable deployment of DNS, reverse proxy, encrypted backups, directory, SSO, Git, artifact/container registry,
and container-management services onto one Fedora CoreOS VM (**InfraVM**), on Proxmox, VMware vCenter or Microsoft
Hyper-V. Services run as declarative rootless Podman Compose stacks with Arcane as the OIDC-authenticated management GUI
and Sonatype Nexus Repository as the unified hosted + caching proxy artifact/container registry; the Ansible runner is
a container on InfraVM itself. See the [Nexus & Pure GitOps Architecture Plan](docs/nexus-and-gitops-plan.md).

## Install

For a complete visual walkthrough with screenshots covering clean SFTP backup server setup, first-boot console configuration, Day-1 verification in Arcane, live Btrfs snapshots, and full disaster recovery, see the **[Installation, Operations & Disaster Recovery Guide](docs/operator-guide.md)**.

You can deploy Sophon **either interactively on the VM console** or **completely unattended** using optional mode-`0600`
secrets ISOs (`sophon-secrets.iso` and `sophon-sftp-secrets.iso`) across **Proxmox, VMware vCenter, and Microsoft Hyper-V**.
The laptop needs only **Docker or Podman** (Linux, WSL, macOS or Windows). Under WSL, keep this folder on the Linux
filesystem (`~/sophon`, not `/mnt/c/...`): building the ISOs is several times faster.

### Option A: Zero-prompt unattended install & unified auto-restore (Proxmox, vCenter, Hyper-V)

Run two commands (optionally copying [`.env.example`](.env.example) to `.env` first if you want to pre-fill hypervisor, domain, or IP settings—otherwise `sophon-build-isos.sh` prompts interactively and offers to save your decisions to a mode-`0600` `.env` file at the end):

```sh
# 1. Build all ISOs (prompts for any unset values, writes mode-0600 secrets ISOs + offline OS/app ISOs, and offers to save .env)
sh sophon-build-isos.sh           # Windows: .\sophon-build-isos.ps1

# 2. Provision Sophon (deploys SFTP Backup Server VM when sophon-sftp-secrets.iso exists, then production InfraVM)
sh sophon-provision.sh            # Windows: .\sophon-provision.ps1

# Optional: deploy or refresh the isolated dev / restore-drill VM (`SOPHON_DEV_INFRAVM_NAME` on `SOPHON_DEV_NETWORK_CIDR`)
sh sophon-provision-dev.sh        # Windows: .\sophon-provision-dev.ps1

# Optional: hot-mount renewed sophon-secrets.iso onto InfraVM for monthly air-gapped TLS certificate updates
sh sophon-mount-secrets.sh        # Windows: .\sophon-mount-secrets.ps1

# Optional: stop and delete a Sophon VM (`SOPHON_INFRAVM_NAME` by default, or `--vm-name sophon-dev-infravm`)
sh sophon-destroy-vm.sh           # Windows: .\sophon-destroy-vm.ps1
```

One `sophon-secrets.iso` handles **both Day-1 builds and automated restores** safely:
- **Empty SFTP repository → Day-1 build:** InfraVM ingests `sophon-seed.json` into RAM (`tmpfs`), ejects the CD-ROM, sets the `core` password and static IPv4, initializes the encrypted Kopia repository, deploys all stacks, runs the first verified backup, and enables the `6h` schedule.
- **Existing snapshots found → Safe Standby Auto-Restore:** InfraVM automatically postpones Cloudflare tunnel creation/DNS repointing, connects Kopia in `--readonly` mode with scheduled backups disabled, deploys the stacks, runs `sophon-restore --snapshot latest --yes`, verifies all containers and SNI TLS endpoints locally inside the guest, and reports `STANDBY RESTORE COMPLETE` on the VM login banner (`/etc/issue`).
- **Optional production promotion (`https://<domain>` or `sophon-activate`):** Standby/dev restores run on `SOPHON_DEV_NETWORK_CIDR` (default VM `sophon-dev-infravm`) and never touch live DNS or write to the backup repository. Only when replacing a lost production server, first shut down/delete the old production VM (`sh sophon-destroy-vm.sh`) and then open **`https://<domain>`** (Keycloak SSO, `arcane-admins`) to click **Promote to Active Production**—the button remains disabled/grayed out whenever `SOPHON_NETWORK_CIDR` is still active on the network—or run `sophon-activate` as `core`. Promotion switches the static IP and self-pointing DNS from `SOPHON_DEV_NETWORK_CIDR` to `SOPHON_NETWORK_CIDR`, promotes SFTP to `sophon-backup`, and enables Cloudflare tunnel publishing + scheduled Kopia backups.

### Option B: Interactive console install (no secrets ISO needed)

**1. On your laptop** (internet needed)

```sh
sh sophon-build-isos.sh --os-only # Windows: .\sophon-build-isos.ps1 --os-only
sh sophon-provision.sh            # Windows: .\sophon-provision.ps1
```

Choose where InfraVM runs (Proxmox, vCenter or Hyper-V) and answer the prompts. It downloads everything, builds the
install media, then creates and starts a blank VM with the media attached.

**2. On the VM's console** (Proxmox/vCenter console, or Hyper-V Manager > Connect)

1. CoreOS installs itself and reboots. Nothing to type.
2. When asked, choose a password for `core`, then log in as `core`.
3. Run `sophon-deploy`. It first asks for the network (the VM starts on a DHCP lease; address, prefix and gateway
   default to it, so Enter keeps them as a static address, and DNS defaults to `1.1.1.1`). Then answer **Y** (the
   default) to "Does this site have internet access?" unless the site is offline, and enter your domain and credentials.
   It deploys CoreDNS, Traefik, OpenLDAP, Keycloak, Gitea, Nexus, Arcane and Kopia.

**No internet at the VM's site?** Also follow [Air-gapped sites](#air-gapped-sites).

### Laptop settings

The script builds a tools image the first time (about 560 MB; allow several minutes, because it compiles
`coreos-installer`), then asks for anything it doesn't know. Passwords are
prompted for and never put on a command line. To skip the questions, copy [.env.example](.env.example) to `.env`
and fill it in (git-ignored; the scripts read it automatically).

```sh
install -m 600 .env.example .env  # PowerShell: Copy-Item .env.example .env
```

| Hypervisor | Required settings |
|---|---|
| Proxmox | `SOPHON_PROXMOX_HOST`, `SOPHON_PROXMOX_NODE`, password |
| vCenter | `SOPHON_VCENTER_URL`, `_USERNAME`, `_DATACENTER`, `_CLUSTER`, `_DATASTORE`, `_NETWORK`, password |
| Hyper-V on **this** Windows PC (PowerShell script only) | nothing up front: runs as you, one UAC prompt, asks which virtual switch |
| Hyper-V on another host | `SOPHON_HYPERV_HOST`, `_USERNAME`, `_SWITCH`, password; WinRM enabled on the host (see [docs/manual-commands.md](docs/manual-commands.md)) |

### Cloudflare API token

Needed at `sophon-deploy` (connected sites) for Let's Encrypt DNS-01 and tunnel publishing. Your domain's DNS must be on Cloudflare.

1. Go to <https://dash.cloudflare.com/profile/api-tokens> > **Create Token** > **Create Custom Token**.
2. Add these three permissions:

   | Resource | Scope | Permission | Used for |
   |---|---|---|---|
   | Zone | Zone | Read | find the zone (Let's Encrypt) |
   | Zone | DNS | Edit | DNS-01 challenge records and tunnel CNAMEs |
   | Account | Cloudflare Tunnel | Edit | tunnel routes (skip when `SOPHON_CF_TUNNEL_ENABLED=false`) |

3. **Zone Resources:** Include > Specific zone > your domain. **Account Resources:** Include > your account.
4. **Continue to summary** > **Create Token**, and copy it now (it is shown once). Paste it at the *Cloudflare token*
   prompt, or set `SOPHON_CF_TOKEN` in `.env` (air-gapped `certificates issue` needs only the two Zone permissions).
   For LAN-only, dev, or standby VMs that still issue Let's Encrypt DNS-01 certificates without creating a Cloudflare
   tunnel or repointing public DNS CNAMEs, answer `false` at `SOPHON_CF_TUNNEL_ENABLED`.

### Another hypervisor (for example VMware Workstation)

Run `sh sophon-build-isos.sh --os-only`, then do the VM part by hand:

1. Copy `artifacts/coreos/sophon-coreos.iso` and `artifacts/bootstrap/sophon-apps.iso` to the hypervisor.
   At an [air-gapped site](#air-gapped-sites) (or when using unattended seeding) also copy `artifacts/secrets/sophon-secrets.iso`.
2. Create a blank VM: 64-bit Linux, UEFI or BIOS (Hyper-V: generation 2, Secure Boot off), 8 vCPU, 12 GiB RAM,
   one 160+ GiB disk, one NIC that can reach your network (Workstation: bridged) with DHCP.
3. Attach **both** ISOs as two CD/DVD drives, connected at power-on, disk first in the boot order.
   The certificate disk can be attached now as a third drive, or any time before you run `sophon-deploy`.
4. Power on and continue with part 2 above. If the VM boots the installer again after install, nothing is reinstalled
   (the disk is no longer blank); move the disk above the CDs or eject the ISOs.

### If the install stops

If the installer can't find exactly one blank disk or the deployment ISO, it says why and stops. Run it by hand (this
also lets you pick the disk):
`sudo mkdir -p /mnt/sophon && sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon && sudo bash /mnt/sophon/install.sh`

Later, `sophon-deploy --network` asks for the network again. Detach both ISOs once `sophon-deploy` finishes.

### Using the services

The Appliance Status & Cutover UI is at `https://<domain>` (`https://status.<domain>`, LAN-only and excluded from Cloudflare Tunnel publishing), and Arcane is at `https://arcane.<domain>` (also `traefik`, `dns`, `kopia`, `ldap`, `auth`, `git`, `nexus`, `registry`, `registry-push`).
Sign in to Arcane, Gitea, and Keycloak SSO (`john.doe.admin` or any member of `arcane-admins` receives `role_admin`),
and sign in to Nexus (`https://nexus.<domain>`) with OpenLDAP credentials (`nexus-admins` -> administrator,
`nexus-developers` -> publish/read across hosted, proxy, and group repositories). Pull container images through
`https://registry.<domain>` (`docker-group`) and push internal CI images to `https://registry-push.<domain>` (`docker-hosted`).

Point clients at CoreDNS on the VM for your domain only:

- **Windows (and WSL):** In an elevated PowerShell (WSL2 tunnels DNS through the Windows DNS Client, so this configures both Windows and WSL at once):

  ```powershell
  Add-DnsClientNrptRule -Namespace '.example.com','example.com' -NameServers '<VM address>'; Clear-DnsClientCache
  # undo: Get-DnsClientNrptRule | Where-Object Namespace -eq '.example.com' | Remove-DnsClientNrptRule -Force; Clear-DnsClientCache
  ```

  Test with `Resolve-DnsName traefik.example.com` (`nslookup` on Windows ignores NRPT rules; inside WSL use `getent ahosts traefik.example.com`).

- **Linux (`systemd-resolved` 258+):** Add a domain-scoped DNS delegate (use `/etc/systemd/dns-delegate.d` instead of `/run/...` to persist across reboots; on non-WSL NixOS, enable `services.resolved.enable = true;` first):

  ```sh
  sudo mkdir -p /run/systemd/dns-delegate.d
  printf '[Delegate]\nDNS=<VM address>\nDomains=~example.com\n' | sudo tee /run/systemd/dns-delegate.d/sophon.dns-delegate >/dev/null
  sudo systemctl reload systemd-resolved
  # undo: sudo rm -f /run/systemd/dns-delegate.d/sophon.dns-delegate && sudo systemctl reload systemd-resolved
  ```

  Test with `resolvectl query traefik.example.com`.

Don't point a machine's whole DNS at the VM if the VM's upstream forwards back to it (for example the Hyper-V Default Switch address): lookups loop.

### Backups

Kopia is built during Prepare, carried offline and deployed after the core
stacks are running. Its [native action collector](roles/kopia) operates without
Arcane, Gitea or a management API key: Kopia's built-in scheduler (`--snapshot-interval`)
and native before/after snapshot actions (`/app/backup.py`) clone declared named
volumes or paths (including Gitea repositories and file-backed **Git LFS** objects
under `/data/git/lfs`) via Btrfs CoW reflinks from `/volumes:ro`, apply `.kopiaignore`
exclusions, and stream hot logical database dumps (`pg_dumpall`, `pg_dump -Fc -Z0`,
`slapcat`) through a locked-down `dump-proxy` with **zero container stops**.
Snapshots contain real files, uncompressed deduplicatable dumps and a versioned
manifest—never images, caches, container environments or a system copy.

Supply the external SFTP host/user/path, repository password and Kopia UI
credentials at the console. If you do not already have an SFTP server or NAS,
Prepare also builds `artifacts/backup/sophon-sftp-server.iso`, an
unattended Alpine Linux SFTP backup appliance (`sophon-backup-setup` and
`sophon-authorize-key`); see [Operator Guide](docs/operator-guide.md) and
[Manual Backup Server Setup](docs/manual-backup-server.md).
Configure generates a client key, shows the public
line to authorize, confirms host fingerprints and runs preflight before stacks;
see [backup inputs](docs/inputs.md#kopia-backup-inputs). Only a confirmed missing
repository on an unused path may be created; all other errors fail closed.
Keep repository decryption credentials
outside the encrypted repository. `SOPHON_KOPIA_BACKUPS_ENABLED` defaults to
`true`: Configure configures Kopia's native snapshot policy and actions, triggers
and verifies the first snapshot, and leaves the six-hour schedule active in one
step (while offering to pause scheduled backups when a newly installed VM
reconnects to an existing repository before `sophon-restore`).

Core services and developer workloads deployed via Gitea Actions opt in using
concise Traefik-style `sophon.backup.*` container labels—no Sophon Ansible edits
or volume mount lists required (see [self-service labels](roles/kopia/README.md#self-service-container-labels)).
Undeclared volumes are reported, not automatically copied. Trigger an immediate
snapshot from the Kopia Web UI (**Snapshot Now** on `/sophon-state`) or from the
console:

```bash
podman exec kopia kopia snapshot create /sophon-state
podman exec kopia cat /source/last-run.json
```

Arcane is reconstructible: nothing may exist only in its UI. Rebuild means
redeploy from code and restore state via `sophon-restore`—either automatically
in **Safe Standby Mode** via `sh sophon-provision-dev.sh` (promoting
to live production and switching from `SOPHON_DEV_NETWORK_CIDR` to
`SOPHON_NETWORK_CIDR` only if you explicitly click **Promote to Active Production**
at `https://<domain>` after decommissioning the old prod VM with `sh sophon-destroy-vm.sh`,
or run `sophon-activate`), or
interactively via `sophon-deploy --prepare-restore` followed by `sophon-restore`
(see [Manual Installation & Recovery](docs/manual-installation.md)). Across
**Proxmox, VMware vCenter, and Microsoft Hyper-V**, `sh sophon-provision-dev.sh`
(PowerShell: `.\sophon-provision-dev.ps1`, plus optional weekly
`systemd --user` timer units in [contrib/systemd/](contrib/systemd)) always
deletes any existing `SOPHON_DEV_INFRAVM_NAME` (`sophon-dev-infravm`) first and
boots a clean standby VM on `SOPHON_DEV_NETWORK_CIDR` with the confined
read-only `sophon-restore` SFTP key—no `serial0` socket and no workstation-to-VM
network path required. Treat `sophon-secrets.iso` and `sophon-sftp-secrets.iso`
like passwords: delete or detach them once ingested. See the
[Operations Guide](docs/operator-guide.md) and the
[Kopia role reference](roles/kopia/README.md).

### Air-gapped sites

Run the laptop step while connected (it caches every image). The VM installs offline; `sophon-secrets.iso` carries your
pre-issued wildcard certificate in place of live Let's Encrypt issuance.

1. **Connected machine:** `sh sophon-build-isos.sh certificates issue --domain example.com`
   (PowerShell: `.\sophon-build-isos.ps1 certificates issue ...`). It asks for an ACME email and a Cloudflare token
   (the two Zone permissions above) and writes `artifacts/secrets/sophon-secrets.iso` (label `SOPHON_CERTS`),
   preserving any existing `/sophon-seed.json` while storing your wildcard certificate, chain and private key (no token
   or ACME account). It is a secret: delete it after use. Run it on the same machine each time.
2. **Attach it to the VM** as a CD/DVD (local Hyper-V: `.\sophon-provision.ps1 attach-certificates`; a new VM gets it
   automatically if the ISO exists).
3. **On the console:** run `sophon-deploy`, answer **n** to "Does this site have internet access?", and press Enter at the certificate prompt.

Without a token there is no tunnel publishing. Keep the VM's clock accurate.

**Renewals (monthly).** The certificate lasts 90 days and an air-gapped VM can't renew it. Repeat steps 1-2, then run
`sophon-deploy --refresh-certificates` (answer **n** to the internet question, press Enter, re-enter your existing credentials; it reruns the
full deployment). Eject the disk afterwards (`.\sophon-provision.ps1 detach-certificates`). Details:
[docs/certificates.md](docs/certificates.md).

### Upgrading

Rebuild the tools image (`docker build -t sophon-tools -f Containerfile .`, or `podman`), then run
`sh sophon-build-isos.sh --os-only --force`. New ISOs only affect **new** VMs; don't swap images under
existing PostgreSQL or LDAP volumes without migrating ([details](docs/inputs.md#upgrading-from-the-previous-pins)).
