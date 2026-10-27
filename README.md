# Sophon

Offline-capable deployment of DNS, reverse proxy, directory, SSO and Git services onto one
Fedora CoreOS VM (**InfraVM**), on Proxmox or VMware vCenter. Services run as rootless Podman
containers managed through Portainer; the Ansible runner is a container on InfraVM itself.

## The four steps

The only thing the laptop needs is **Docker or Podman**, on Linux, Windows or macOS, with no bash
or other tools. One container image (`sophon-tools`) holds Ansible, xorriso, govc and Traefik.
Build it once from this repository, then every laptop step is a plain `docker run`:

```bash
docker build -t sophon-tools -f Containerfile .
```

| Step | Where | Command | What happens |
|---|---|---|---|
| 1. Prepare | connected laptop | `docker run … sophon-tools prepare` | Download and cache the stock CoreOS live ISO and container images; build the runner image and `artifacts/bootstrap/sophon-deployment.iso`. |
| 2. Provision | laptop with hypervisor access | `docker run … sophon-tools provision` | Runs step 1 if needed, then creates a blank InfraVM with both ISOs attached and starts it. |
| 3. Install | InfraVM console | `sudo bash /mnt/sophon/install.sh` | Installs CoreOS to the blank disk offline with a static IP and a `core` console password, then reboots. |
| 4. Configure | InfraVM console | `sophon-controller` | Asks for the domain and credentials, then deploys CoreDNS, Traefik, OpenLDAP, Keycloak and Gitea. |

Run these from the project directory. The same lines work in bash, PowerShell and cmd
(`"$(pwd)"` is `"%cd%"` in cmd):

```bash
# Step 1 (connected)
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock sophon-tools prepare

# Step 2 (Proxmox). The password is passed by name from your environment, never typed on the command line.
#   bash:        read -rsp 'Proxmox password: ' SOPHON_PROXMOX_PASSWORD; export SOPHON_PROXMOX_PASSWORD
#   PowerShell:  $env:SOPHON_PROXMOX_PASSWORD = Read-Host 'Proxmox password' -MaskInput
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state -e SOPHON_PROXMOX_HOST=pve.example -e SOPHON_PROXMOX_NODE=pve -e SOPHON_PROXMOX_PASSWORD sophon-tools provision
```

For vCenter add `-e SOPHON_HYPERVISOR=vcenter` and the `SOPHON_VCENTER_*` settings instead; every
input is listed in [docs/inputs.md](docs/inputs.md). Prefer a form? Run the dashboard, then open
http://127.0.0.1:41327/ and fill in the settings there:

```bash
docker run --rm --init -it -p 127.0.0.1:41327:41327 -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state sophon-tools dashboard
```

Notes:
- **Podman:** use `podman` instead of `docker`, enable its socket once (`systemctl --user enable --now podman.socket`) and mount it:
  `-v /run/user/$(id -u)/podman/podman.sock:/var/run/docker.sock`.
- **Linux with rootful Docker** creates `artifacts/` as root; add `--user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)"`.
- The container reaches the hypervisor through the engine's normal NAT (no host networking), so it needs no special
  setup on Docker Desktop. If the hypervisor is only reachable through a VPN or special route, check that route works from containers.
- Only the Linux (Podman and Docker) path has been verified. Windows and macOS are untested; the commands avoid bash
  features on purpose, and `sophon-state` is a named volume so private file modes also hold on Windows.

In the VM's console (the stock live ISO), step 3:

```bash
sudo mkdir -p /mnt/sophon && sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

After the reboot, log in as `core` and run `sophon-controller` (step 4). Both ISOs can then be detached.

### Another hypervisor (Hyper-V, VMware Workstation, ...)

Only Proxmox and vCenter are automated. Elsewhere, do step 2 by hand; steps 3 and 4 are unchanged.

1. Run the step 1 command and copy `artifacts/coreos/fedora-coreos-live.iso` and
   `artifacts/bootstrap/sophon-deployment.iso` to the hypervisor.
2. Create a blank VM: 64-bit Linux, UEFI (Hyper-V: generation 2, Secure Boot off), 8 vCPU, 8 GiB RAM,
   one 40+ GiB (default 100) disk, one NIC that can reach your network (Workstation: bridged).
3. Attach **both** ISOs as two CD/DVD drives, connected at power-on, with the disk first in the boot order.
4. Power on and continue with steps 3 and 4 above. If it boots the live ISO again after install,
   move the disk above the CDs or eject the ISOs.

### Certificates without internet

Run once a month (certificates last 90 days); the first run is part of step 4.

1. **On a connected machine**, any time before step 4 and again for each renewal:
   ```bash
   docker run --rm --init -it -v "$(pwd):/workspace" -v sophon-state:/state sophon-tools certificates issue --domain example.com --output payload.txt
   ```
   It prompts for an ACME email and a Cloudflare token (or reads `-e CF_DNS_API_TOKEN`) and writes one
   base64 line, the whole `acme.json`, never the token. `--output` avoids PowerShell's `>` re-encoding the text.
   Carry the file to the site; it holds private keys.
2. **On the InfraVM console**, paste the payload at step 4's *Pre-issued certificates* prompt (or give
   `@/workspace/path/payload.txt`), leave the Cloudflare token empty. For a renewal run
   `sophon-controller --refresh-certificates` and re-enter the existing service credentials.

See [docs/certificates.md](docs/certificates.md) for validation, backups and rate limits.

## Layout

```text
Containerfile                                      the tools image (default) and the InfraVM runner image
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
  cli.py              the container's entry point: prepare, provision, dashboard, certificates, test
  bundle.py           builds the deployment ISO
  certificates.py     issue / validate / import the ACME certificate store
  console.py          step 4 terminal wizard
  deployment.py       progress viewer and optional dashboard (dashboard/)
callback_plugins/   Ansible progress reporting for the dashboard
docs/               inputs, architecture, certificates, decisions
tests/              unit and fixture tests
```

## Tests

```bash
docker run --rm -v "$(pwd):/workspace" sophon-tools test   # unit, fixture, progress-viewer and step-boundary tests
```

`tests/test_dashboard.py` drives a real Chromium through Playwright and is skipped unless
Playwright is installed (run it with `python3 tests/test_dashboard.py` on a machine that has it).
The 4-step flow has been verified end to end on both hypervisors; the layout above is the
re-architecture of that working project.
