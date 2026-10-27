# Sophon

Offline-capable deployment of DNS, reverse proxy, directory, SSO and Git services onto one
Fedora CoreOS VM (**InfraVM**), on Proxmox or VMware vCenter. Services run as rootless Podman
containers managed through Portainer; the Ansible runner is a container on InfraVM itself.

## The four steps

The only thing the laptop needs is **Docker or Podman**, on Linux, Windows or macOS, with no bash
or other tools. One container image (`sophon-tools`) holds Ansible, xorriso, govc and Traefik.

### Setup: build the tools image (once per laptop)

On the connected laptop, in the project folder (the one containing `Containerfile`):

```bash
docker build -t sophon-tools -f Containerfile .
```

- **When:** once, before step 1. It downloads base images and tools (the result is about 560 MB), so it needs internet.
- **How often:** never again, unless you update this repository and `Containerfile` changed (tool versions are
  pinned there); then run it again. Changes to playbooks and `src/` need no rebuild, because the project folder is
  mounted into the container on every run.
- **Podman:** use `podman build -t sophon-tools -f Containerfile .`.
- **Offline later:** the image stays in your local engine, so steps 1 to 4 afterwards need no rebuild.
  Check it exists with `docker image ls sophon-tools`.

After that, every laptop step below is a plain `docker run` using that image.

| Step | Where | What happens |
|---|---|---|
| 1. Prepare | connected laptop | Download and cache the stock CoreOS live ISO and container images; build the runner image and `artifacts/bootstrap/sophon-deployment.iso`. |
| 2. Provision | laptop with hypervisor access | Runs step 1 if needed, then creates a blank InfraVM with both ISOs attached and starts it. |
| 3. Install | InfraVM console | Installs CoreOS to the blank disk offline with a static IP and a `core` console password, then reboots. |
| 4. Configure | InfraVM console | Asks for the domain and credentials, then deploys CoreDNS, Traefik, OpenLDAP, Keycloak and Gitea. |

Run the laptop commands from the project directory. In PowerShell write `${PWD}` for `$(pwd)` (cmd: `%cd%`); Step 1
shows the Windows lines in full, and Step 2 and the dashboard follow the same pattern. Every setting is listed in [docs/inputs.md](docs/inputs.md).

### Which engine do I have? (decides the socket path in the commands)

The container drives your engine through its socket, so the left side of that `-v` must be a socket that exists:

| Your engine | Socket to mount |
|---|---|
| Docker Engine or Docker Desktop (Linux, Windows, macOS) | `/var/run/docker.sock` |
| Podman, **including a `docker` command that is really Podman** (`podman-docker`, typical on WSL and Fedora) | `/run/user/$(id -u)/podman/podman.sock` |
| Podman on Windows or macOS (`podman machine`) | `/run/user/1000/podman/podman.sock` (inside the machine; see Step 1) |

If you see `statfs /var/run/docker.sock: no such file or directory`, you are on Podman. Enable its socket once with
`systemctl --user enable --now podman.socket` and use the Podman path. In every command below, replace
`/var/run/docker.sock` before the colon with the path for your engine (the part after the colon never changes).

### Step 1: prepare (internet needed)

Docker:

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock sophon-tools prepare
```

Podman (this works with `docker` in place of `podman` too):

```bash
podman run --rm -it -v "$(pwd):/workspace" -v /run/user/$(id -u)/podman/podman.sock:/var/run/docker.sock sophon-tools prepare
```

**Windows PowerShell, Docker Desktop** (same as the bash line; `${PWD}` is PowerShell's current folder). The socket path
is inside Docker Desktop's Linux VM, so it stays `/var/run/docker.sock`:

```powershell
docker run --rm -it -v "${PWD}:/workspace" -v /var/run/docker.sock:/var/run/docker.sock sophon-tools prepare
```

**Windows PowerShell, Podman** (`podman machine`; PowerShell has no `$(id -u)`, and the socket lives inside the machine VM,
usually `/run/user/1000/podman/podman.sock`):

```powershell
podman run --rm -it -v "${PWD}:/workspace" -v /run/user/1000/podman/podman.sock:/var/run/docker.sock sophon-tools prepare
```

Check the socket path with `podman machine ssh "ls /run/user/*/podman/podman.sock /run/podman/podman.sock"`; if only
`/run/podman/podman.sock` exists (a rootful machine) use that instead. Keep the project under `C:\Users\...`, the folder
`podman machine` shares by default. The Windows commands are **untested**.

### Step 2: provision

Passwords are passed by name (`-e NAME`, no value) from your environment, so they never appear on the command line.
Set the password first:

```bash
# bash
read -rsp 'Password: ' SOPHON_PROXMOX_PASSWORD; export SOPHON_PROXMOX_PASSWORD     # vCenter: SOPHON_VCENTER_PASSWORD
```
```powershell
# PowerShell
$env:SOPHON_PROXMOX_PASSWORD = Read-Host 'Password' -MaskInput                     # vCenter: SOPHON_VCENTER_PASSWORD
```

**Proxmox:**

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state -e SOPHON_PROXMOX_HOST=pve.example.com -e SOPHON_PROXMOX_NODE=pve -e SOPHON_PROXMOX_PASSWORD sophon-tools provision
```

**vCenter:**

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state -e SOPHON_HYPERVISOR=vcenter -e SOPHON_VCENTER_URL=https://vcenter.example.com -e SOPHON_VCENTER_USERNAME=administrator@vsphere.local -e SOPHON_VCENTER_DATACENTER=DC1 -e SOPHON_VCENTER_CLUSTER=Cluster1 -e SOPHON_VCENTER_DATASTORE=datastore1 -e SOPHON_VCENTER_NETWORK="VM Network" -e SOPHON_VCENTER_PASSWORD sophon-tools provision
```

**Dashboard** (a form for the same settings and a live progress diagram; open http://127.0.0.1:41327/):

```bash
docker run --rm --init -it -p 127.0.0.1:41327:41327 -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state sophon-tools dashboard
```

### Another hypervisor (for example Hyper-V or VMware Workstation)

Only Proxmox and vCenter are automated. Elsewhere, do step 2 by hand; steps 3 and 4 are unchanged.

1. Run the step 1 command and copy `artifacts/coreos/fedora-coreos-live.iso` and
   `artifacts/bootstrap/sophon-deployment.iso` to the hypervisor.
2. Create a blank VM: 64-bit Linux, UEFI (Hyper-V: generation 2, Secure Boot off), 8 vCPU, 8 GiB RAM,
   one 40+ GiB (default 100) disk, one NIC that can reach your network (Workstation: bridged).
3. Attach **both** ISOs as two CD/DVD drives, connected at power-on, with the disk first in the boot order.
4. Power on and continue with steps 3 and 4 above. If it boots the live ISO again after install,
   move the disk above the CDs or eject the ISOs.

Notes:
- **Rootful Docker on Linux** creates `artifacts/` as root; add `--user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)"` after `docker run`.
- The container reaches the hypervisor through the engine's normal NAT (no host networking). If the hypervisor is only
  reachable through a VPN or special route, check that route works from containers.
- Only the Linux (Podman and Docker) path has been verified. Windows and macOS are untested; the commands avoid bash
  features on purpose, and `sophon-state` is a named volume so private file modes also hold on Windows.

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
| Before 4 | On a connected machine, make the certificate payload (below). Carry the file to the site. |
| 2 | None, if the laptop can reach the hypervisor's management network. The laptop itself may be offline once steps 1 and the tools image exist. |
| 3 | None. It was always offline. |
| 4 | Paste the payload at the *Pre-issued certificates* prompt (or `@/workspace/path/payload.txt`) and leave the **Cloudflare token empty**. With no token the payload is **required**; step 4 stops if it is missing or does not cover all six names. |

Without a Cloudflare token, Cloudflare tunnel publishing is also unavailable. Give InfraVM an accurate clock, and
point your clients' DNS at CoreDNS on InfraVM (see [docs/inputs.md](docs/inputs.md)).

**Renewals (recurring, every 30 days or so).** Certificates last 90 days and nothing on an air-gapped InfraVM can
renew them, so carry in a fresh payload on a schedule. Run the generator on the connected machine about once a month:
it reuses certificates that still have more than 30 days left and issues new ones only after that, so an early run
just returns the same payload. Import it before the day-90 expiry; a monthly cadence always lands in the window
between day 60 and day 90.

1. **On the connected machine**, for the first time and every renewal:
   ```bash
   docker run --rm --init -it -v "$(pwd):/workspace" -v sophon-state:/state sophon-tools certificates issue --domain example.com --output payload.txt
   ```
   It prompts for an ACME email and a Cloudflare token (or reads `-e CF_DNS_API_TOKEN`) and writes one
   base64 line, the whole `acme.json`, never the token. `--output` avoids PowerShell's `>` re-encoding the text.
   Carry the file to the site; it holds private keys. Run it on the same machine each time (it keeps the ACME account
   in the `sophon-state` volume).
2. **On the InfraVM console**, the first time paste it during step 4 as above. For a renewal run:
   ```bash
   sophon-controller --refresh-certificates
   ```
   Paste the new payload, leave the Cloudflare token empty, and re-enter the existing service credentials (a refresh
   currently reruns the full service deployment). If the certificates have already expired, the same steps recover the site.

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
