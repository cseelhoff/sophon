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
3. Run `sophon-deploy`. Confirm the network, then enter your domain and credentials. It deploys CoreDNS, Traefik,
   OpenLDAP, Keycloak and Gitea.

**No internet at the VM's site?** Also follow [Air-gapped sites](#air-gapped-sites).

### Laptop settings

The script builds a tools image the first time (about 560 MB), then asks for anything it doesn't know. Passwords are
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

If the installer cannot find exactly one blank disk or the deployment ISO, it says so on the console and stops. Run it
yourself (this also lets you choose the disk):
`sudo mkdir -p /mnt/sophon && sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon && sudo bash /mnt/sophon/install.sh`.

`sophon-deploy --network` asks for the network again later. Both ISOs can be detached once `sophon-deploy` finishes.

### Air-gapped sites

Do part 1 on a **connected** laptop: it caches every image, and nothing is pulled later. The laptop can then be offline
if it can still reach the hypervisor. The VM installs offline; a secret certificate disk replaces Let's Encrypt. Add
these to the install above:

1. **On the connected machine**, make the certificate disk (first time and every renewal):
   ```bash
   bash sophon-provision.sh certificates issue --domain example.com
   ```
   (PowerShell: `.\sophon-provision.ps1 certificates issue ...`.) It prompts for an ACME email and a Cloudflare token
   (or reads `CF_DNS_API_TOKEN` from your environment) and writes `artifacts/secrets/sophon-certificates.iso`
   (volume label `SOPHON_CERTS`): one `acme.json` with your wildcard certificate for the domain and `*.<domain>`, its
   full chain and its private key, never the token or the ACME account. It holds a private key: carry it to the
   site and delete it afterwards. Run the command on the same machine each time (it keeps the ACME account in the
   `sophon-state` volume).
2. **Attach it to the VM** as a CD/DVD (on this PC's Hyper-V: `.\sophon-provision.ps1 attach-certificates`; a new VM
   gets it automatically if the ISO already exists).
3. **On the console**, run `sophon-deploy` and answer **y** to "air-gapped". It finds the disk and offers its file at the
   *Pre-issued certificates file* prompt (press Enter). The file is **required**; `sophon-deploy` stops if it does not
   hold a trusted certificate for the domain and `*.<domain>`.

Without a Cloudflare token, Cloudflare tunnel publishing is also unavailable. Give InfraVM an accurate clock, and
point your clients' DNS at CoreDNS on InfraVM (see [docs/inputs.md](docs/inputs.md)).

**Renewals (every 30 days or so).** Certificates last 90 days and nothing on an air-gapped InfraVM can renew them.
Repeat step 1 monthly: it reuses certificates with more than 30 days left, so an early run just writes the same disk
again, and a monthly cadence always lands between day 60 and day 90. Attach the new disk and run on the console:

```bash
sophon-deploy --refresh-certificates
```

Answer **y** to "air-gapped", press Enter at the certificate prompt, and re-enter the existing service credentials
(a refresh currently reruns the full service deployment). If the certificates have already expired, the same steps
recover the site. Afterwards eject the disk (`.\sophon-provision.ps1 detach-certificates` on local Hyper-V).

See [docs/certificates.md](docs/certificates.md) for validation, backups and rate limits.

**Rebuild the tools image before preparing new media:** `docker build -t sophon-tools -f Containerfile .`
(use `podman` if that is your engine). The launchers reuse an existing tools image.
Then run `prepare -e prepare_force=true`. **Do not apply these database/LDAP changes to existing volumes without
migration:** see [the upgrade boundary](docs/inputs.md#upgrading-from-the-previous-pins).
