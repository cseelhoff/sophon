# Sophon

Sophon provisions a self-contained Fedora CoreOS InfraVM onto Proxmox or VMware vCenter, then
deploys DNS, reverse proxy, directory, SSO, and Git services as rootless Podman
containers managed through Portainer. The Ansible/controller runner is a
container on InfraVM itself; no early NFS or separate Controller VM is needed.

It is built for internet-connected homelabs but stages every artifact ahead of
time, so service deployment also works at a site with no egress. Disconnected
sites must carry pre-issued certificates separately before deploying services.

> **These documents describe the intended design.** The codebase does not yet
> match it in several places. See [docs/known-gaps.md](docs/known-gaps.md) for
> the current divergences.

## Documentation

| Document | What's in it |
|---|---|
| [instructions.md](instructions.md) | User input checklist and Phase 1/2 walkthrough for Proxmox, vCenter, and offline certificate handoff |
| [CONTEXT.md](CONTEXT.md) | Glossary — the words this project uses and what they mean |
| [docs/preconditions.md](docs/preconditions.md) | What must be true before you run anything |
| [docs/architecture.md](docs/architecture.md) | How the pieces fit and what depends on what |
| [prestage.md](prestage.md) | Prestage runbook |
| [docs/runbooks/sneakernet-refresh.md](docs/runbooks/sneakernet-refresh.md) | Monthly certificate refresh for disconnected sites |
| [docs/adr/](docs/adr/) | Why things are the way they are |
| [docs/known-gaps.md](docs/known-gaps.md) | Where the code disagrees with the design |

## Two deployment phases

Connected **Prestage** prepares two complete deployment media:

- `artifacts/coreos/fedora-coreos-live.iso`: the unchanged, full upstream
  CoreOS live ISO, verified against official stream metadata.
- `artifacts/bootstrap/sophon-deployment.iso`: the reusable project, scripts,
  offline runner, collections, static installer helper, and container archives.

1. **Bootstrap laptop:** creates InfraVM with a blank disk and both ISOs,
   using only the selected hypervisor's management access; there is no disk-image conversion,
   QEMU Ignition injection, guest-agent setup, or NFS connection.
2. **InfraVM console:** the operator installs CoreOS offline, enters guest
   networking and the `core` console password, and reboots; the host bootstrap
   starts rootless Portainer, and the local controller runner deploys services.

Networking, passwords, SSH keys, and site-specific settings are not embedded
in the two reusable ISOs. No SSH key or SSH connection is required. The domain
and service credentials are collected after installation. NFS/Kopia are deferred
to a separately designed opt-in backup workflow.
See [ADR-0013](docs/adr/0013-two-iso-infravm-installation.md).

**Fresh deployments only:** conflicting existing InfraVMs are preserved and
rejected unless marked as this two-ISO workflow. Rerunning Phase 1 never replaces
an existing disk or reinstalls CoreOS. Other legacy VMs are not changed.

## Quick start

Start Phase 1 on the bootstrap laptop:

```bash
nix develop
python3 deployment.py --phase bootstrap --serve --port 41327
```

Choose **Configure**, select **Proxmox** or **VMware vCenter**, and supply the
selected provider's placement and access settings, plus VM name, CPU, memory,
and disk size. Proxmox uses ISO/disk storage and bridge/VLAN; vCenter uses a
datacenter, cluster, shared datastore, and an existing port group whose VLAN is
already configured. The displayed minimum disk size is only a
floor; the installer checks actual media size and available space.

Open the new InfraVM's hypervisor console in the live CoreOS environment:

```sh
sudo mkdir -p /mnt/sophon
sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

Confirm the fresh installation disk, static IP/prefix, gateway, DNS, and a
console password for `core`. The script rejects nonblank or mounted disks,
verifies bundle checksums, installs offline, and copies the artifacts to disk.
It does not run the large Ansible image from the live environment's RAM.
After reboot, log in as `core` and run:

```sh
sophon-controller
```

The console wizard asks for the domain and service credentials using hidden
password prompts, confirms deployment, and shows Ansible output. The writable
project is `/var/lib/sophon/project` on the host, `/workspace` in the runner.
Both ISOs can be detached after installation. Retain credentials in your
password manager; later reruns require the same existing values.

**No guest internet:** run `python3 certificates.py issue` on a connected machine.
It prompts for the domain, ACME email and hidden Cloudflare token, issues/reuses
the certificates, and outputs one private base64 `acme.json` payload. Paste that
into the installed service wizard's optional **Pre-issued certificates** field.
Leave the Cloudflare token empty on the disconnected VM. The token remains on
the connected machine; neither reusable installation ISO contains certificates
or private keys. See the [sneakernet runbook](docs/runbooks/sneakernet-refresh.md)
and [four-step walkthrough](instructions.md).

If host setup fails, inspect `journalctl -u sophon-host-bootstrap` and retry
with `sudo systemctl restart sophon-host-bootstrap`; do not reinstall.
Portainer also closes uninitialized setup after an idle timeout. If this happens
while entering service settings, restart that host service and rerun the wizard.
The restart preserves Portainer's data and reopens its initialization window.

For the optional dashboard, run `sophon-controller --dashboard --port 41327`.
It listens on loopback; browser connectivity is your responsibility, not an
installation prerequisite. Run this instead of the console wizard, not alongside
it. The runner has no SSH, privileged host access, or mounted Podman socket:
only Portainer gets the rootless socket and Ansible uses its HTTP interface.

For a terminal run, use `python3 deployment.py --phase bootstrap -- bootstrap.yml`
on the laptop, or export service settings and run `sophon-controller --run`
on installed InfraVM. Do not use the legacy deployment on an existing VM.

No laptop site settings are transferred automatically.
If you use custom Ansible overrides such as container image versions during
Prestage/bootstrap, change the corresponding non-secret project defaults before
bundling so service deployment uses the same versions.
Arbitrary `-e` values are deliberately not serialized into the ISO.

### vCenter Phase 1

The dashboard exposes only the selected provider's fields. For a terminal run:

```bash
export SOPHON_HYPERVISOR=vcenter
export SOPHON_VCENTER_URL=https://vcenter.example.com
export SOPHON_VCENTER_USERNAME=administrator@vsphere.local
read -rsp 'vCenter password: ' SOPHON_VCENTER_PASSWORD; echo
export SOPHON_VCENTER_PASSWORD
export SOPHON_VCENTER_DATACENTER=Datacenter
export SOPHON_VCENTER_CLUSTER=Cluster
export SOPHON_VCENTER_DATASTORE=SharedDatastore
export SOPHON_VCENTER_NETWORK=VLAN51-PortGroup
# Optional: an existing absolute inventory folder; empty uses /Datacenter/vm.
export SOPHON_VCENTER_FOLDER=/Datacenter/vm
# Optional: verified CA PEM on this laptop when vCenter uses an internal CA.
export SOPHON_VCENTER_CA_FILE=/path/to/verified-vcenter-ca.pem
python3 deployment.py --phase bootstrap -- bootstrap.yml
```

`govc`, supplied by `nix develop`, uses VMware's APIs for immutable ISO uploads,
a fresh thin-provisioned disk, VMXNET3 networking, a PVSCSI controller, and two
CD drives. Boot order is disk then CD, so installation starts from the stock ISO
on a blank disk and subsequent boots use the installed OS. Confirm the actual
disk name at the console (normally `/dev/sda` on VMware).

HTTPS verification is always enabled. Validate the internal CA independently
before using it; do not fetch and blindly trust every certificate in a bundle.
Credentials are passed to `govc` in its environment, not command-line arguments,
and persistent `govc` sessions are disabled. vCenter credentials and placement
settings never enter the service runner or reusable bundle.

An existing VM must have the completed Sophon two-ISO ownership marker and must
not be a template. It is only started; disks, media, and installed state are not
rewritten. Ambiguous names and partially configured VMs are rejected for manual
inspection rather than automatically deleted or adopted. The current diagram
has no vCenter icon: vCenter authentication is tracked in the stage list without
highlighting the Proxmox icon.

Phase 2 refreshes service-name entries inside the runner before each deployment,
so changing the domain in Configure takes effect without restarting the runner.
Traefik configures Portainer's hostname route in Phase 2; InfraVM's first boot
exposes only the domain-independent Portainer API on port 9443.
The managed Podman network forwards service-container DNS to InfraVM's CoreDNS;
the installation's upstream DNS remains the host resolver. Gitea deployment
waits for its initializer to finish successfully, including OIDC configuration.

### Deployment diagram

The optional VLAN setting applies to InfraVM. Leave it empty
for an untagged bridge; Sophon then omits the Proxmox NIC `tag` property. Set a
VLAN ID from 1 to 4094 when the bridge requires tagged traffic.

Start the local dashboard from the same environment and repository directory:

```bash
python3 deployment.py --serve --port 41327
```

Open the printed loopback URL and choose **Configure**. Only the current phase's supported `SOPHON_*`
settings, **including actual secret values**, prepopulate from the environment
inherited at server startup. Password fields are visually masked, not omitted.
All required passwords for that phase must be supplied. Clearing an optional field explicitly
clears its exported value. An empty Gitea database password override reuses
the supplied Portainer password, preserving the existing defaults. No password
lookup, controller password file, or password-file lock is used.

- **Generate shell deployment command** produces safely quoted exports for the
  entire form. Bootstrap invokes `ansible-playbook bootstrap.yml` with tracing.
  Phase 2 invokes `sophon-controller --run` from the InfraVM host shell,
  keeping Ansible and trace files inside the runner.
  Generation alone
  does not start a deployment. Use **Copy** to paste it into sh, bash, or zsh on
  this controller, with the project environment active. The exports remain in
  that shell; deployment runs in a subshell to preserve your working directory.
- **Run** confirms the target and launches the same full playbook and trace
  configuration directly from the server, without shell evaluation or prompts.

**Generated commands contain plaintext secrets.** Do not share them. Clipboard
and terminal history may retain them. The server does not save generated commands
or edited settings. Edits stay in the browser tab until reload; closing Configure
clears the generated textbox, not your form. No browser storage is used. Settings
and command endpoints require the dashboard token and responses are not cached.
The server exposes only supported deployment settings, not unrelated environment
secrets. Use it only on a trusted controller; do not expose its loopback port.

Exports made after server startup are not visible to the GUI. Restart it from the
configured terminal to refresh inherited settings. `&` backgrounds the server;
it does not synchronize environments or keep a server alive after terminal exit.
Omit `--port` to select a free port. The listening socket owns the port; there is
no server registry, PID file, or lock file.

**Run one deployment at a time per trace directory.** There are no deployment
locks or cross-terminal exclusion. The trace is not a native lock: overlapping
runs can overwrite each other's progress. Do not both click Run and paste the
command. The page disables Run while the displayed run is active, but that is
not a concurrency guarantee.

Ansible's native log appends to `artifacts/deployment/<phase>/ansible.log` with mode `0600`.
Browser runs additionally capture console output (including startup errors) in
`artifacts/deployment/<phase>/deploy.log`, overwritten per Run with mode `0600`. These
logs may contain secrets and are never served to the browser. For terminal
prompts or custom Ansible arguments, the optional launcher remains available:

```bash
python3 deployment.py --phase bootstrap -- bootstrap.yml
```

Pass the same inventory and extra variables you normally give `ansible-playbook`.
The terminal launcher preserves prompts, output, and Ansible's exit code, but
does not start a web server; start `--serve` separately if wanted. The
viewer supports full runs, not check mode, task-start overrides, or tag-filtered
runs. Ordinary `ansible-playbook` commands remain unchanged.

The callback atomically writes structured progress to
`artifacts/deployment/<phase>/status.json`; the browser polls it about once a second,
updating the SVG and steps only when the report changes. It does not parse or
tail the raw Ansible log. Both Run and copied commands target the same trace
directory (override it with `--state-dir` when starting the server).

Each run resets the diagram. Full colour means that stage's deployment and
configuration completed in this run, not that its service is online. Nested
roles belong to the enclosing stage. Skipped work, failures, interruptions,
and unknown outcomes are distinct; ignored and rescued failures have counters
in the selected stage's details. Selecting a stage also shows task names,
source locations, outcomes, durations, return codes, and sanitized failure
messages/stdout/stderr. Expand a task or filter to failures and recoveries.
History retains the latest 1,000 tasks per stage, up to 4,000 characters per
diagnostic field and 20 failed loop-item summaries. Successful debug output,
module arguments, response bodies, and `no_log` output are excluded. Known
credentials and common credential patterns are redacted; sensitive tasks must
still use `no_log` rather than relying on text redaction alone.

Task details are captured only by new runs using the updated callback. Older
run counters cannot reconstruct them; their private Controller log remains
available locally. The raw log is never served to the browser.

The dashboard stays available after Ansible exits. `--serve` never starts a
deployment by itself. Ctrl+C stops a foreground server. An unfinished trace
whose recorded process has exited is shown as unknown; this is advisory PID
liveness, not locking or service health. A copied command that fails before
Ansible initializes its callback leaves no trace: inspect the terminal/log,
not stage task results, for that error. The dashboard shows a persistent failure
summary and the private Controller log location, even if no stage was reached.
New launcher startup failures also include recovery instructions; older reports
show a generic pre-stage explanation and their exit code when recorded. If
`ansible-playbook` cannot start, stop the viewer and restart it from `nix develop`.
The terminal launcher also records startup failures and interruptions.
The server serves only its explicit routes, not
the repository or the rest of `artifacts/`.

After editing the diagram, export it again to `sophon.svg` with cell IDs
preserved, update `CELLS` in `deployment.py` if mapped cells were replaced, and
restart the server. Missing mapped cells fail validation at startup. All stages
now have mapped icons, including the production Proxmox cluster and Kopia.
InfraVM includes Portainer, but the old `coreos.qcow2` and NFS disk icons are
untracked: the current workflow uses a blank disk and two real ISOs.
Labels are extracted from the SVG for
tooltips and the selected stage's details; hover other labeled elements to see
that they are not tracked by this viewer.

Prestage maps only depicted artifacts produced by the current role:
`portainer.tar`, `coredns.tar`, `openldap.tar`, `traefik.tar`,
`keycloak.tar`, `gitea.tar`, and `postgres.tar`. These are diagram aliases, not
literal artifact paths: CoreDNS is
`coredns-dockerdiscovery.tar`, and other container archives have vendor/version
names. Their colour follows the overall Prestage outcome, not individual file
verification. Depicted `.sql` backups, `.yml` files, and unsupported
container archives remain untracked; the current Prestage role does not produce
them. The `LDAP Backup` icon also remains untracked: the OpenLDAP role does not
produce a backup dump file. The diagram is broader than the implemented deployment.

The dashboard defaults to dark mode, including forms, task results, and the
embedded diagram, regardless of the operating system's theme. The viewer applies
the SVG's native dark colours and removes white label backplates without
inverting service logos or modifying the source `sophon.svg`.

Zoom ranges from 25% to 500%; Fit restores the diagram to the pane's width.
Selecting a stage shows its task results. Use Hide beside the task filter to
close that section and restore the larger diagram view. Progress updates do not
reopen it; select any stage to show task results again.

Run the local checks with `python3 tests/test_deployment.py`. They use temporary
Ansible fixtures and never deploy the site. Browser interaction checks are
separate: with the Python `playwright` package and its Chromium browser installed,
run `python3 tests/test_dashboard.py`. They exercise zoom and hiding task results at desktop
and mobile sizes against a temporary viewer, without deploying anything.

### Disconnected site

Stage on a connected machine, copy `artifacts/` across, then deploy:

```bash
# Connected machine
ansible-playbook prestage.yml

# At the site, after copying the repo and artifacts/ to the bootstrap laptop
python3 deployment.py --phase bootstrap -- bootstrap.yml
```

Read [docs/preconditions.md](docs/preconditions.md) first. Several required
inputs — a publicly registered domain, a Cloudflare API token, a Portainer
Business Edition licence — are not obvious from the command line.

Supply the same credentials and addresses on later runs. Environment variables
are inputs, not durable storage; retain credentials in your password manager.
Anything passed with `-e` takes precedence over the environment. Sophon no longer
reads or writes reusable password files under `artifacts/secrets/`
([migration notes](docs/preconditions.md#existing-deployments)).

`artifacts/` holds private keys and credentials. It is gitignored, it is a
backup source, and losing it means losing access to a running deployment.
Never paste credentials into tracked files.

## What gets deployed

One required VM on Proxmox:

| VM | OS | Role |
|---|---|---|
| `sophon-infravm` | Fedora CoreOS | Runs the controller and service workloads as rootless Podman containers |

Services on InfraVM, deployed in this order:

| Service | Address | Purpose |
|---|---|---|
| Portainer | `https://<infravm_ip>:9443` | Container management, and the API Ansible deploys through |
| CoreDNS | `dns.<domain>` | Authoritative DNS for the zone; discovers containers and maintains tunnel ingress |
| Traefik | `traefik.<domain>` | Reverse proxy, TLS termination, ACME |
| OpenLDAP | `ldap.<domain>` | Directory |
| Keycloak | `auth.<domain>` | SSO, federated against OpenLDAP |
| Gitea | `git.<domain>` | Git server, SSO via Keycloak |

Kopia and backup-server provisioning are not part of this run.

## Running without egress

A site with no internet access still deploys. Two features stop working:
Cloudflare tunnel access, and automatic certificate renewal. Everything else
runs normally on the local network
([ADR-0006](docs/adr/0006-site-egress-is-expected-airgap-is-a-degraded-mode.md)).

Certificates are then carried in by hand, roughly monthly — see
[docs/runbooks/sneakernet-refresh.md](docs/runbooks/sneakernet-refresh.md).

## Containerized Development

This repository includes a VS Code devcontainer for developers who want the Nix
toolchain inside Docker instead of installing Nix directly on their workstation.
The container uses Ubuntu as the VS Code base image, installs single-user Nix,
and enables flakes so the flake can build the Sophon development shell and
Docker-image tarballs without requiring Nix on the host.

Prerequisites on the host:

- Docker or another Docker-compatible engine
- VS Code with the Dev Containers extension

Open the repository in VS Code and choose **Dev Containers: Reopen in Container**.
The devcontainer installs the flake's `sophon-dev-env` package into the
`vscode` user's Nix profile while the image is built, so tools such as
`ansible-playbook`, `ansible-lint`, `butane`, `skopeo`, and `go` are available on
the normal container `PATH` without entering `nix develop` first. The image build
also installs the Ansible Galaxy collections used by the playbooks.

Rebuild the devcontainer after changing `flake.nix` or `flake.lock` so the baked
tool profile is refreshed. `nix develop` still works inside the container and is
useful when testing shell changes, but it is no longer required for ordinary
Ansible commands or VS Code extension discovery.

The devcontainer sets `updateRemoteUserUID` to `false`. This avoids an extra
Dev Containers rebuild stage that can hang on Podman-compatible Docker shims when
they try to resolve the generated local image name as an interactive short name.

The default devcontainer does not mount the host Docker or Podman socket. Docker
hosts usually expose `/var/run/docker.sock`, while rootless Podman hosts expose a
user-specific socket such as `/run/user/1000/podman/podman.sock`; assuming either
one can make Dev Containers fail before the workspace opens. Build the image
tarball inside the devcontainer, then load it from a host terminal.

The flake also exposes a Nix-built Docker image containing the Sophon tooling:

```bash
nix build .#sophon-runner-image
```

From the host, load and run the image with Docker or Podman:

```bash
docker load < result
docker run --rm -it \
   --user "$(id -u):$(id -g)" \
   -v "$PWD:/workspace" \
   sophon-nix-runner:latest
```

Use `podman load` and `podman run` with the same arguments on Podman hosts.

This image is a Nix/Nixpkgs-built container image, not a full NixOS boot inside
Docker. Docker containers share the host kernel, so use a VM when you need a real
NixOS system with its own init, kernel, and system services.

## Secrets

Passwords and tokens are supplied through `SOPHON_*` environment variables or
explicit Ansible overrides (`-e` still takes precedence). Required credentials
are validated before deployment; there is no automatic password generation,
controller password persistence, or required Ansible Vault integration. Never
commit credentials to a tracked file. Logs and deployed service configuration
can still contain secrets; protect them accordingly.

See [docs/preconditions.md](docs/preconditions.md) for the full list of inputs
and [migration notes](docs/preconditions.md#existing-deployments) before running
an existing installation with this credential-input model.

## Testing

```bash
yamllint .
ansible-lint
ansible-playbook --syntax-check bootstrap.yml site.yml prestage.yml

python3 tests/test_deployment.py
python3 tests/test_phases.py
python3 tests/test_console.py
python3 tests/test_installer.py
python3 tests/test_bundle.py
python3 tests/test_controller.py
python3 tests/test_vcenter.py
python3 tests/test_certificates.py
# With Playwright and Chromium installed:
python3 tests/test_dashboard.py

# Molecule role tests (roles/*/molecule/)
molecule test
```

`tests/test.yml` holds legacy override examples, not live integration fixtures.
Run the Python suites above for local, non-provisioning checks. A real service
deployment must run inside the Controller runner and use the site's own values.

## Backup / Restore

### Traefik `acme.json` (Let's Encrypt account + issued certs)

Let's Encrypt enforces a **5 duplicate-certificates / week** rate limit per
identical SAN set. If the `traefik_data` podman volume is ever recreated
(stack redeploy with prune, host rebuild, etc.) without a backup, the next
few redeploys will burn through that quota and lock TLS issuance for ~7 days.

The traefik role auto-snapshots `acme.json` after every deploy and seeds it
back into a fresh volume on the next deploy. To take an on-demand backup
between deploys, run the dedicated playbook:

```bash
ansible-playbook traefik-backup-acme.yml \
  -e domain_name=example.com \
  -e infravm_ip=10.0.60.3 \
  -e portainer_admin_password=<password>
```

Output: `./artifacts/traefik/acme.json` (mode `0600`, gitignored). Treat as a
secret — it contains the ACME account private key and all issued cert keys.
Include `artifacts/traefik/` in your kopia backup set for offsite recovery.

To force a fresh issuance (e.g. when migrating to staging CA), skip the
auto-restore on the next deploy:

```bash
ansible-playbook site.yml -e traefik_acme_restore_on_deploy=false ...
```
 
