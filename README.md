# Sophon

Offline-capable deployment of DNS, reverse proxy, directory, SSO and Git services onto one
Fedora CoreOS VM (**InfraVM**), on Proxmox, VMware vCenter or Microsoft Hyper-V. Services run as rootless Podman
containers managed through Portainer; the Ansible runner is a container on InfraVM itself.

## Install

You do two things: run one command on your laptop, then answer prompts on the VM's console.
The laptop needs only **Docker or Podman** (Linux, WSL, macOS or Windows). Under WSL, keep this folder on the Linux
filesystem (`~/sophon`, not `/mnt/c/...`): building the ISOs is several times faster.

**1. On your laptop** (internet needed)

```bash
bash sophon-provision.sh          # Windows: .\sophon-provision.ps1
```

Choose where InfraVM runs (Proxmox, vCenter or Hyper-V) and answer the prompts. It downloads everything, builds the
install media, then creates and starts a blank VM with the media attached.

**2. On the VM's console** (Proxmox/vCenter console, or Hyper-V Manager > Connect)

1. CoreOS installs itself and reboots. Nothing to type.
2. When asked, choose a password for `core`, then log in as `core`.
3. Run `sophon-deploy`. It first asks for the network (the VM starts on a DHCP lease; address, prefix and gateway
   default to it, so Enter keeps them as a static address, and DNS defaults to `1.1.1.1`). Then answer **Y** (the
   default) to "Does this site have internet access?" unless the site is offline, and enter your domain and credentials.
   It deploys CoreDNS, Traefik,
   OpenLDAP, Keycloak and Gitea.

**No internet at the VM's site?** Also follow [Air-gapped sites](#air-gapped-sites).

### Laptop settings

The script builds a tools image the first time (about 560 MB; allow several minutes, because it compiles
`coreos-installer`), then asks for anything it doesn't know. Passwords are
prompted for and never put on a command line. To skip the questions, copy [.env.example](.env.example) to `.env`
and fill it in (git-ignored; the scripts read it automatically).

```bash
cp .env.example .env              # PowerShell: Copy-Item .env.example .env
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
   | Account | Cloudflare Tunnel | Edit | tunnel routes (skip if you don't use tunnels) |

3. **Zone Resources:** Include > Specific zone > your domain. **Account Resources:** Include > your account.
4. **Continue to summary** > **Create Token**, and copy it now (it is shown once). Paste it at the *Cloudflare token*
   prompt, or set `CF_DNS_API_TOKEN` for `certificates issue` (that needs only the two Zone permissions).

### Another hypervisor (for example VMware Workstation)

Run `prepare`, then do the VM part by hand:

1. Copy `artifacts/coreos/sophon-installer.iso` and `artifacts/bootstrap/sophon-deployment.iso` to the hypervisor.
   At an [air-gapped site](#air-gapped-sites) also copy the certificate disk, `artifacts/secrets/sophon-certificates.iso`.
2. Create a blank VM: 64-bit Linux, UEFI or BIOS (Hyper-V: generation 2, Secure Boot off), 8 vCPU, 8 GiB RAM,
   one 40+ GiB disk, one NIC that can reach your network (Workstation: bridged) with DHCP.
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

Portainer is at `https://portainer.<domain>` (also `traefik`, `dns`, `ldap`, `auth`, `git`). `https://<VM address>:9443`
is Portainer's own self-signed bootstrap port (certificate for `localhost`): ignore it.

Point clients at CoreDNS on the VM for your domain only. On Windows, in an elevated PowerShell:

```powershell
Add-DnsClientNrptRule -Namespace '.example.com' -NameServers '<VM address>'; Clear-DnsClientCache
# undo: Get-DnsClientNrptRule | Where-Object Namespace -eq '.example.com' | Remove-DnsClientNrptRule -Force
```

Test with `Resolve-DnsName` (`nslookup` ignores these rules). Don't point a machine's whole DNS at the VM if the VM's
upstream forwards back to it (for example the Hyper-V Default Switch address): lookups loop.

### Air-gapped sites

Run the laptop step while connected (it caches every image). The VM installs offline; a secret certificate disk
replaces Let's Encrypt.

1. **Connected machine:** `bash sophon-provision.sh certificates issue --domain example.com`
   (PowerShell: `.\sophon-provision.ps1 certificates issue ...`). It asks for an ACME email and a Cloudflare token
   (the two Zone permissions above) and writes `artifacts/secrets/sophon-certificates.iso` (label `SOPHON_CERTS`):
   your wildcard certificate, chain and private key, no token or ACME account. It is a secret: delete it after use.
   Run it on the same machine each time.
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
`bash sophon-provision.sh prepare -e prepare_force=true`. New ISOs only affect **new** VMs; don't swap images under
existing PostgreSQL, LDAP or Portainer volumes without migrating ([details](docs/inputs.md#upgrading-from-the-previous-pins)).
