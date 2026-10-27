# Sophon

Offline-capable deployment of DNS, reverse proxy, directory, SSO and Git services onto one
Fedora CoreOS VM (**InfraVM**), on Proxmox or VMware vCenter. Services run as rootless Podman
containers managed through Portainer; the Ansible runner is a container on InfraVM itself.

## The four steps

The only thing the laptop needs is **Docker or Podman**. `./sophon` runs every laptop-side tool
(Ansible, xorriso, govc, Traefik) from one container image that it builds on first use.

| Step | Where | Command | What happens |
|---|---|---|---|
| 1. Prepare | connected laptop | `./sophon prepare` | Download and cache the stock CoreOS live ISO and container images; build the runner image and `artifacts/bootstrap/sophon-deployment.iso`. |
| 2. Provision | laptop with hypervisor access | `./sophon provision` | Runs step 1 if needed, then creates a blank InfraVM with both ISOs attached and starts it. |
| 3. Install | InfraVM console | `sudo bash /mnt/sophon/install.sh` | Installs CoreOS to the blank disk offline with a static IP and a `core` console password, then reboots. |
| 4. Configure | InfraVM console | `sophon-controller` | Asks for the domain and credentials, then deploys CoreDNS, Traefik, OpenLDAP, Keycloak and Gitea. |

Step 2 is also available as a dashboard: `./sophon dashboard`, then open http://127.0.0.1:41327/.
Details, inputs and passwords for each step are in [docs/inputs.md](docs/inputs.md).

```bash
./sophon prepare                 # step 1 (connected)
export SOPHON_PROXMOX_HOST=pve.example SOPHON_PROXMOX_NODE=pve
read -rsp 'Proxmox password: ' SOPHON_PROXMOX_PASSWORD; export SOPHON_PROXMOX_PASSWORD
./sophon provision               # step 2 (vCenter: SOPHON_HYPERVISOR=vcenter, see docs/inputs.md)
```

Podman users: enable its API socket once with `systemctl --user enable --now podman.socket`
(`./sophon` uses it to build and save images). `./sophon` uses host networking, which works on
Linux and WSL; Docker Desktop on macOS/Windows is not supported yet.

In the VM's console (the stock live ISO), step 3:

```bash
sudo mkdir -p /mnt/sophon && sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

After the reboot, log in as `core` and run `sophon-controller` (step 4). Both ISOs can then be detached.

### Another hypervisor (Hyper-V, VMware Workstation, ...)

Only Proxmox and vCenter are automated. Elsewhere, do step 2 by hand; steps 3 and 4 are unchanged.

1. Run `./sophon prepare` and copy `artifacts/coreos/fedora-coreos-live.iso` and
   `artifacts/bootstrap/sophon-deployment.iso` to the hypervisor.
2. Create a blank VM: 64-bit Linux, UEFI (Hyper-V: generation 2, Secure Boot off), 8 vCPU, 8 GiB RAM,
   one 40+ GiB (default 100) disk, one NIC that can reach your network (Workstation: bridged).
3. Attach **both** ISOs as two CD/DVD drives, connected at power-on, with the disk first in the boot order.
4. Power on and continue with steps 3 and 4 above. If it boots the live ISO again after install,
   move the disk above the CDs or eject the ISOs.

### Certificates without internet

Run once a month (certificates last 90 days); the first run is part of step 4.

1. **On a connected machine**, any time before step 4 and again for each renewal:
   `umask 077; ./sophon certificates issue --domain example.com > payload.txt`.
   It prompts for an ACME email and a Cloudflare token (or reads `CF_DNS_API_TOKEN`) and prints one
   base64 line, the whole `acme.json`, never the token. Carry the file to the site; it holds private keys.
2. **On the InfraVM console**, paste the payload at step 4's *Pre-issued certificates* prompt (or give
   `@/workspace/path/payload.txt`), leave the Cloudflare token empty. For a renewal run
   `sophon-controller --refresh-certificates` and re-enter the existing service credentials.

See [docs/certificates.md](docs/certificates.md) for validation, backups and rate limits.

## Layout

```text
sophon                                             laptop entry point (runs everything in a container)
Containerfile                                      the tools image and the InfraVM runner image
1-prepare.yml  2-provision.yml  4-configure.yml   the playbooks, numbered by step
ansible.cfg  inventory.yml                         Ansible setup
group_vars/all.yml                                 every SOPHON_* setting, in one place

roles/
  prepare/          step 1   cache downloads, build runner + deployment ISO
  proxmox/ vcenter/ step 2   authenticate and create the blank VM
  portainer/        step 4   first-run Portainer setup and licence
  portainer_stack/  step 4   deploy one compose stack through Portainer's API
  coredns/ traefik/ openldap/ keycloak/ gitea/   step 4   the services
library/vcenter_vm.py                              vCenter module (wraps govc)

guest/              files that run inside InfraVM
  install.sh          step 3, from the live ISO
  host-bootstrap.sh   first boot: load images, start Portainer and the runner
  sophon-controller   step 4 entry point (runs src/console.py in the runner)

src/                Python helpers
  bundle.py           builds the deployment ISO
  certificates.py     issue / validate / import the ACME certificate store
  console.py          step 4 terminal wizard
  deployment.py       progress viewer and optional dashboard (dashboard/)
callback_plugins/   Ansible progress reporting for the dashboard
docs/               inputs, architecture, certificates, decisions
tests/              ./sophon test
```

## Tests

```bash
./sophon test      # unit, fixture, progress-viewer and step-boundary tests, in the tools container
```

`tests/test_dashboard.py` drives a real Chromium through Playwright and is skipped unless
Playwright is installed (run it with `python3 tests/test_dashboard.py` on a machine that has it).
The 4-step flow has been verified end to end on both hypervisors; the layout above is the
re-architecture of that working project.
