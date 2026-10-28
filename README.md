# Sophon

Offline-capable deployment of DNS, reverse proxy, directory, SSO and Git services onto one
Fedora CoreOS VM (**InfraVM**), on Proxmox, VMware vCenter or Microsoft Hyper-V. Services run as rootless Podman
containers managed through Portainer; the Ansible runner is a container on InfraVM itself.

## The four steps

The only thing the laptop needs is **Docker or Podman** (on Windows, either one, or WSL with one of them). Everything
else (Ansible, xorriso, govc, Traefik) lives in one container image that the scripts build for you.

| Step | Where | What happens |
|---|---|---|
| 1. Prepare | connected laptop | Download and cache the stock CoreOS live ISO and container images; build the runner image and `artifacts/bootstrap/sophon-deployment.iso`. |
| 2. Provision | laptop with hypervisor access | Runs step 1 if needed, then creates a blank InfraVM with both ISOs attached and starts it. |
| 3. Install | InfraVM console | Installs CoreOS offline to the only blank disk. Asks for the static IP, prefix, gateway and DNS (current values are the defaults; Enter accepts) and a new `core` console password. Then reboots. |
| 4. Configure | InfraVM console | Asks for the domain and credentials, then deploys CoreDNS, Traefik, OpenLDAP, Keycloak and Gitea. |

### Steps 1 and 2: one command

From the project folder:

```bash
bash sophon-provision.sh          # Linux, macOS, WSL
```
```powershell
.\sophon-provision.ps1            # Windows
```

The script finds Docker or Podman and the right socket, builds the tools image the first time (internet, about 560 MB),
then asks **where InfraVM should run** (Proxmox, vCenter, Hyper-V, or *only download and build the installation media
and exit*) and for any setting you haven't already given it. It runs step 1 (internet) and then creates and starts the
VM. Passwords are prompted for and never put on a command line.

**Settings:** to avoid the questions, copy [.env.example](.env.example) to `.env` and fill in what you know. It lists
every option for steps 1 and 2 with a comment on each; the scripts read `.env` automatically (it is git-ignored).
Anything left blank is asked for, and a variable already set in your shell overrides the file. The step 4 settings are
entered on the InfraVM console instead (see [docs/inputs.md](docs/inputs.md)).

```powershell
Copy-Item .env.example .env     # bash: cp .env.example .env
# edit .env, for example SOPHON_HYPERVISOR=hyperv to deploy to Hyper-V on this PC without the menu
```

What each hypervisor needs:

| Hypervisor | Required settings |
|---|---|
| Proxmox | `SOPHON_PROXMOX_HOST`, `SOPHON_PROXMOX_NODE`, password |
| vCenter | `SOPHON_VCENTER_URL`, `_USERNAME`, `_DATACENTER`, `_CLUSTER`, `_DATASTORE`, `_NETWORK`, password |
| Hyper-V on **this** Windows PC (PowerShell script only) | nothing up front: runs as you, one UAC prompt, asks which virtual switch; no password, WinRM or firewall changes |
| Hyper-V on another host | `SOPHON_HYPERV_HOST`, `_USERNAME`, `_SWITCH`, password; WinRM must be enabled on the host (see [docs/manual-commands.md](docs/manual-commands.md)) |

Other commands take the same form: `prepare` (step 1 only), `certificates ...`, `test`. Then open the VM's console (Hyper-V Manager > Connect, or your
hypervisor's console) and continue at step 3. Create the Hyper-V virtual switch beforehand if you have none; InfraVM
connects to it and needs a static-capable network. The raw `docker run` equivalents, the socket paths for each engine
and the Hyper-V details are in [docs/manual-commands.md](docs/manual-commands.md).

### Another hypervisor (for example VMware Workstation)

Proxmox, vCenter and Hyper-V are automated. Elsewhere, do step 2 by hand; steps 3 and 4 are unchanged.

1. Run `prepare` and copy `artifacts/coreos/fedora-coreos-live.iso` and
   `artifacts/bootstrap/sophon-deployment.iso` to the hypervisor.
2. Create a blank VM: 64-bit Linux, UEFI (Hyper-V: generation 2, Secure Boot off), 8 vCPU, 8 GiB RAM,
   one 40+ GiB (default 100) disk, one NIC that can reach your network (Workstation: bridged).
3. Attach **both** ISOs as two CD/DVD drives, connected at power-on, with the disk first in the boot order.
4. Power on and continue with steps 3 and 4. If it boots the live ISO again after install,
   move the disk above the CDs or eject the ISOs.

### Step 3: install, on the VM console (the stock live ISO)

```bash
sudo mkdir -p /mnt/sophon && sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

After the reboot, log in as `core`, then step 4:

```bash
sophon-controller
```

Both ISOs can then be detached. **No internet at the site?** Read [Step 4 at a site with no internet](#step-4-at-a-site-with-no-internet-air-gapped) first.

### Step 4 at a site with no internet (air-gapped)

Steps 1 to 3 are the same; only step 4 and a recurring renewal differ. Read this before you start:

| Step | Air-gapped difference |
|---|---|
| Setup and 1 | Do these on a **connected** laptop. Step 1 caches every image the services need, and the deployment never pulls anything later. |
| Before 4 | On a connected machine, make the secret certificate ISO (below). Carry it to the site and attach it to the InfraVM. |
| 2 | None, if the laptop can reach the hypervisor's management network. The laptop itself may be offline once steps 1 and the tools image exist. |
| 3 | None. It was always offline. |
| 4 | Attach the certificate ISO, run `sophon-controller` and answer **y** to "air-gapped". It finds the disk by itself and offers its file as the default at the *Pre-issued certificates file* prompt (press Enter). The file is **required**; step 4 stops if it does not hold a trusted certificate for the domain and `*.<domain>`. |

Without a Cloudflare token, Cloudflare tunnel publishing is also unavailable. Give InfraVM an accurate clock, and
point your clients' DNS at CoreDNS on InfraVM (see [docs/inputs.md](docs/inputs.md)).

**Renewals (recurring, every 30 days or so).** Certificates last 90 days and nothing on an air-gapped InfraVM can
renew them, so carry in a fresh certificate disk on a schedule. Run the generator on the connected machine about once
a month: it reuses certificates that still have more than 30 days left and issues new ones only after that, so an
early run just writes the same disk again. Import it before the day-90 expiry; a monthly cadence always lands in the
window between day 60 and day 90.

1. **On the connected machine**, for the first time and every renewal:
   ```bash
   bash sophon-provision.sh certificates issue --domain example.com
   ```
   (PowerShell: `.\sophon-provision.ps1 certificates issue ...`.) It prompts for an ACME email and a Cloudflare token
   (or reads `CF_DNS_API_TOKEN` from your environment) and writes `artifacts/secrets/sophon-certificates.iso`
   (volume label `SOPHON_CERTS`): one `acme.json` with your wildcard certificate for the domain and `*.<domain>`, its
   full chain and its private key, never the token or the ACME account. The ISO holds a private key: carry it to the
   site, and delete it afterwards. Run the command on the same machine each time (it keeps the ACME account in the
   `sophon-state` volume).
2. **Attach it to the InfraVM.** On this PC's Hyper-V: `.\sophon-provision.ps1 attach-certificates` (a new VM gets it
   automatically if the ISO already exists). Elsewhere, attach the ISO as a CD/DVD in your hypervisor.
3. **On the InfraVM console**, run `sophon-controller` the first time. For a renewal run:
   ```bash
   sophon-controller --refresh-certificates
   ```
   Answer **y** to "air-gapped" and press Enter at the certificate prompt, and re-enter the existing service credentials
   (a refresh currently reruns the full service deployment). If the certificates have already expired, the same
   steps recover the site. Afterwards eject the disk (`.\sophon-provision.ps1 detach-certificates` on local Hyper-V).

See [docs/certificates.md](docs/certificates.md) for validation, backups and rate limits.

## Layout

```text
sophon-provision.sh  sophon-provision.ps1          steps 1-2 in one command (detect engine, run the tools container)
.env.example                                       every step 1-2 setting with comments; copy to .env
2-provision-hyperv-local.ps1                       step 2 for Hyper-V on this PC, as the signed-in user
Containerfile                                      the tools image (default) and the InfraVM runner image
1-prepare.yml  2-provision.yml  4-configure.yml   the playbooks, numbered by step
ansible.cfg  inventory.yml                         Ansible setup
group_vars/all.yml                                 every SOPHON_* setting, in one place

roles/
  prepare/          step 1   cache downloads, build runner + deployment ISO
  proxmox/ vcenter/ hyperv/ step 2   authenticate and create the blank VM
  portainer/        step 4   first-run Portainer setup and licence
  portainer_stack/  step 4   deploy one compose stack through Portainer's API
  coredns/ traefik/ openldap/ keycloak/ gitea/   step 4   the services
library/vcenter_vm.py                              vCenter module (wraps govc)
library/hyperv_vm.py                               Hyper-V module (PowerShell over WinRM, pypsrp)

guest/              files that run inside InfraVM
  install.sh          step 3, from the live ISO
  host-bootstrap.sh   first boot: load images, start Portainer and the runner
  sophon-controller   step 4 entry point (runs src/console.py in the runner)

src/                Python helpers
  cli.py              the container's entry point: prepare, provision, certificates, test
  bundle.py           builds the deployment ISO
  certificates.py     issue / validate / import the ACME certificate store
  console.py          step 4 terminal wizard
  deployment.py       setting validation and the playbook launcher
docs/               inputs, manual commands, architecture, certificates, decisions
tests/              unit and fixture tests
```

## Tests

```bash
bash sophon-provision.sh test   # unit, fixture and step-boundary tests
```

The 4-step flow has been verified end to end on Proxmox and vCenter; Hyper-V is newer and not yet verified on a real host.
