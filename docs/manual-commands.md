# Manual commands (without the wrapper scripts)

`sophon-provision.sh` / `sophon-provision.ps1` run exactly the commands below after detecting your engine and socket.
Use this page when you want to see or customise them. Every setting is listed in [inputs.md](inputs.md).

## Build the tools image

```bash
docker build -t sophon-tools -f Containerfile .      # or: podman build ...
```

Needs internet and about 560 MB; the first build takes several minutes because it compiles `coreos-installer`. Repeat
only when `Containerfile` changes (tool versions are pinned there). Changes to playbooks and `src/` need no rebuild,
because the project folder is mounted into the container on every run.

## Which socket to mount

The container drives your engine through its socket, so the left side of that `-v` must be a socket that exists:

| Your engine | Socket to mount |
|---|---|
| Docker Engine or Docker Desktop (Linux, Windows, macOS) | `/var/run/docker.sock` |
| Podman, **including a `docker` command that is really Podman** (`podman-docker`, typical on WSL and Fedora) | `/run/user/$(id -u)/podman/podman.sock` |
| Podman on Windows or macOS (`podman machine`) | `/run/user/1000/podman/podman.sock` (inside the machine) |

If you see `statfs /var/run/docker.sock: no such file or directory`, you are on Podman: enable its socket once with
`systemctl --user enable --now podman.socket`. In every command below, replace `/var/run/docker.sock` before the colon
with the path for your engine (the part after the colon never changes). For `podman machine`, check the path with
`podman machine ssh "ls /run/user/*/podman/podman.sock /run/podman/podman.sock"`; a rootful machine uses
`/run/podman/podman.sock`. Keep the project under `C:\Users\...` so `podman machine` can share it.

## Step 1: prepare (internet needed)

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock sophon-tools prepare
```
```powershell
docker run --rm -it -v "${PWD}:/workspace" -v /var/run/docker.sock:/var/run/docker.sock sophon-tools prepare
```

Repeat runs rebuild the deployment ISO and the installer ISO (`artifacts/coreos/sophon-installer.iso`) only when an
input changed (`-e prepare_force=true` as extra arguments forces it).

## Step 2: provision

Passwords are passed by name (`-e NAME`, no value) from your environment, so they never appear on the command line.

```bash
read -rsp 'Password: ' SOPHON_PROXMOX_PASSWORD; export SOPHON_PROXMOX_PASSWORD     # vCenter: SOPHON_VCENTER_PASSWORD, Hyper-V: SOPHON_HYPERV_PASSWORD
```
```powershell
$env:SOPHON_PROXMOX_PASSWORD = Read-Host 'Password' -MaskInput
```

**Proxmox:**

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state -e SOPHON_PROXMOX_HOST=pve.example.com -e SOPHON_PROXMOX_NODE=pve -e SOPHON_PROXMOX_PASSWORD sophon-tools provision
```

**vCenter:**

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state -e SOPHON_HYPERVISOR=vcenter -e SOPHON_VCENTER_URL=https://vcenter.example.com -e SOPHON_VCENTER_USERNAME=administrator@vsphere.local -e SOPHON_VCENTER_DATACENTER=DC1 -e SOPHON_VCENTER_CLUSTER=Cluster1 -e SOPHON_VCENTER_DATASTORE=datastore1 -e SOPHON_VCENTER_NETWORK="VM Network" -e SOPHON_VCENTER_PASSWORD sophon-tools provision
```

### Hyper-V on another host (WinRM)

The container drives the host over WinRM/PowerShell; nothing is installed on the host. Once per host, in an
**elevated** PowerShell on that host:

```powershell
Enable-PSRemoting -Force
```

Only if needed (each is a security trade-off; keep the scope narrow):
- The laptop reaches the host over a **Public** network profile, from another subnet, or through WSL/Docker NAT, and the
  connection times out: `Set-NetFirewallRule -Name 'WINRM-HTTP-In-TCP' -RemoteAddress <laptop address or subnet>`.
- You use a **local** account that is not the built-in `Administrator`: set the registry value
  `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\LocalAccountTokenFilterPolicy` to `1`.

The account must be in the host's **Administrators** group. Create the virtual switch first (Hyper-V Manager > Virtual
Switch Manager). Then:

```bash
docker run --rm -it -v "$(pwd):/workspace" -v /var/run/docker.sock:/var/run/docker.sock -v sophon-state:/state -e SOPHON_HYPERVISOR=hyperv -e SOPHON_HYPERV_HOST=hv.example.com -e SOPHON_HYPERV_USERNAME=Administrator -e SOPHON_HYPERV_SWITCH="External" -e SOPHON_HYPERV_PASSWORD sophon-tools provision
```

It creates a Generation 2 VM (Secure Boot off, static memory, no checkpoints) with one blank dynamic disk and the installer
and deployment ISOs as DVD drives, disk first in the boot order, uploads each ISO once (hash-named, verified, skipped on reruns; allow a
while on the first run, since they travel over WinRM), and starts it. Optional settings: `SOPHON_HYPERV_PORT` (5985 HTTP
with NTLM message encryption, or 5986 HTTPS with a verified certificate, plus `SOPHON_HYPERV_CA_FILE`) and
`SOPHON_HYPERV_PATH` (a folder on the host such as `D:\Hyper-V`, default is the host's VM path). This provider has been
checked against the cmdlet definitions and with stubbed PowerShell, but **not yet against a real remote host**.

### Hyper-V on this same Windows machine (no WinRM, no credentials)

The container cannot use your Windows identity, so after step 1 run the PowerShell script natively. It asks for
elevation with one UAC prompt unless you are already an administrator or in Hyper-V Administrators, and needs no
Python or container:

```powershell
.\2-provision-hyperv-local.ps1 -Switch "Default Switch"
```

`-Switch` (or `SOPHON_HYPERV_SWITCH`) names an existing virtual switch (if omitted, the script lists them and asks);
`-Path`, `-Name`, `-Cores`, `-MemoryMB` and `-DiskGB` are optional (`SOPHON_HYPERV_PATH` and `SOPHON_INFRAVM_*` are
honoured). It creates the same VM as the remote provider, plus a third DVD drive for the secret certificate ISO
(inserted if `artifacts\secrets\sophon-certificates.iso` exists, otherwise empty). Verified end to end on a real host.
For an air-gapped site, `.\2-provision-hyperv-local.ps1 -AttachCertificates` inserts that ISO into the existing VM and
`-DetachCertificates` ejects it (the same as `.\sophon-provision.ps1 attach-certificates` / `detach-certificates`).

## Certificates (air-gapped sites only)

On a connected machine, to make the secret certificate ISO (`artifacts/secrets/sophon-certificates.iso`, label
`SOPHON_CERTS`; see [certificates.md](certificates.md)). No engine socket is needed, but the `sophon-state` volume keeps
the ACME account and issued certificate between runs:

```bash
docker run --rm -it -v "$(pwd):/workspace" -v sophon-state:/state -e CF_DNS_API_TOKEN sophon-tools certificates issue --domain example.com
```

It prompts for the ACME email (and the Cloudflare token if `CF_DNS_API_TOKEN` is unset, hidden). Options:
`--email`, `--timeout`, `--output-iso PATH`, `--output-file PATH` (also write the bare `acme.json`).

## Notes

- **Rootful Docker on Linux** creates `artifacts/` as root; add `--user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)"` after `docker run`.
- The container reaches the hypervisor through the engine's normal NAT (no host networking). If the hypervisor is only
  reachable through a VPN or special route, check that route works from containers.
- Linux/WSL (Podman and Docker) and Windows have both been verified end to end; macOS is lightly tested. The
  commands avoid bash features on purpose, and `sophon-state` is a named volume so private file modes also hold on Windows.
- Under WSL, run from a folder on the Linux filesystem (`~/sophon`), not `/mnt/c/...`: ISO builds are much slower over 9p.
