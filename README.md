# Sophon

Sophon provisions a self-contained homelab onto a Proxmox host: an Alpine NFS
server, a Fedora CoreOS VM, and a set of services — DNS, reverse proxy,
directory, SSO, Git, and backup — running as rootless Podman containers and
managed through Portainer.

It is built for internet-connected homelabs but stages every artifact ahead of
time, so the same playbooks work against a Proxmox host with no egress at all.

> **These documents describe the intended design.** The codebase does not yet
> match it in several places. See [docs/known-gaps.md](docs/known-gaps.md) for
> the current divergences.

## Documentation

| Document | What's in it |
|---|---|
| [CONTEXT.md](CONTEXT.md) | Glossary — the words this project uses and what they mean |
| [docs/preconditions.md](docs/preconditions.md) | What must be true before you run anything |
| [docs/architecture.md](docs/architecture.md) | How the pieces fit and what depends on what |
| [prestage.md](prestage.md) | Prestage runbook |
| [docs/runbooks/sneakernet-refresh.md](docs/runbooks/sneakernet-refresh.md) | Monthly certificate refresh for disconnected sites |
| [docs/adr/](docs/adr/) | Why things are the way they are |
| [docs/known-gaps.md](docs/known-gaps.md) | Where the code disagrees with the design |

## Two phases

Sophon runs in two phases, and the split is deliberate.

**Prestage** is the only part that touches the internet. It pulls container
images, builds the CoreDNS Docker Discovery image, and builds the Alpine NFS VM
disk, writing everything into `artifacts/`.

**Deploy** fetches nothing. It provisions Proxmox and brings up every service
using only what Prestage produced.

Prestage always runs, even when the target has full internet access — it is the
only thing that keeps the disconnected path working without a second, untested
code path (see
[ADR-0001](docs/adr/0001-air-gapped-a-supported-deployment-mode.md)). If you are
connected, `site.yml` runs it for you and you never think about it. It is a
no-op once `artifacts/` is populated, so it costs nothing on later runs and
never reaches the network at a disconnected site.

Run `prestage.yml` by hand only when the two phases happen on **different
machines** — stage on a connected host, carry `artifacts/` in, deploy there.

## Quick start

Load the required `SOPHON_*` credentials from your password manager into the
controller environment first; see [credential inputs](docs/preconditions.md#credentials).
Deployment validates them before provisioning and never generates replacement
passwords. Existing installations must reuse their existing credentials.

```bash
nix develop

ansible-playbook site.yml \
  -e proxmox_host=10.0.60.2 \
  -e proxmox_password=<password> \
  -e domain_name=example.com \
  -e nfs_ip=10.0.60.10 \
  -e infravm_ip=10.0.60.11
```

### Deployment diagram

Start the local dashboard from the same environment and repository directory:

```bash
python3 deployment.py --serve --port 41327
```

Open the printed loopback URL and choose **Configure**. All supported `SOPHON_*`
settings, **including actual secret values**, prepopulate from the environment
inherited at server startup. Password fields are visually masked, not omitted.
All required passwords must be supplied. Clearing an optional field explicitly
clears its exported value. Empty Gitea database and Kopia password overrides reuse
the supplied Portainer password, preserving the existing defaults. No password
lookup, controller password file, or password-file lock is used.

- **Generate shell deployment command** produces safely quoted exports for the
  entire form, the progress callback, `SOPHON_PROGRESS_FILE`, and
  `ANSIBLE_LOG_PATH`, followed by `ansible-playbook site.yml`. Generation alone
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

Ansible's native log appends to `artifacts/deployment/ansible.log` with mode `0600`.
Browser runs additionally capture console output (including startup errors) in
`artifacts/deployment/deploy.log`, overwritten per Run with mode `0600`. These
logs may contain secrets and are never served to the browser. For terminal
prompts or custom Ansible arguments, the optional launcher remains available:

```bash
python3 deployment.py -- site.yml
```

Pass the same inventory and extra variables you normally give `ansible-playbook`.
The terminal launcher preserves prompts, output, and Ansible's exit code, but
does not start a web server; start `--serve` separately if wanted. The
viewer supports full runs, not check mode, task-start overrides, or tag-filtered
runs. Ordinary `ansible-playbook` commands remain unchanged.

The callback atomically writes structured progress to
`artifacts/deployment/status.json`; the browser polls it about once a second,
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
not the diagram, for that error. The terminal launcher also records startup
failures and interruptions. The server serves only its explicit routes, not
the repository or the rest of `artifacts/`.

After editing the diagram, export it again to `sophon.svg` with cell IDs
preserved, update `CELLS` in `deployment.py` if mapped cells were replaced, and
restart the server. Missing mapped cells fail validation at startup. All stages
now have mapped icons, including the production Proxmox cluster and Kopia.
InfraVM includes Portainer. Labels are extracted from the SVG for tooltips and
the selected stage's details; hover other labeled elements to see that they are
not tracked by this viewer.

Prestage maps only depicted artifacts produced by the current role:
`alpine-nfs.qcow2`, `portainer.tar`, `coredns.tar`, `openldap.tar`, `traefik.tar`,
`keycloak.tar`, `gitea.tar`, and `postgres.tar`. These are diagram aliases, not
literal artifact paths: the NFS disk is `sophon-nfs-alpine.qcow2`, CoreDNS is
`coredns-dockerdiscovery.tar`, and other container archives have vendor/version
names. Their colour follows the overall Prestage outcome, not individual file
verification. Depicted `.sql` backups, `.yml` files, `coreos.qcow2`, and unsupported
container archives remain untracked; the current Prestage role does not produce
them. The diagram is broader than the implemented deployment.

Run the local checks with `python3 tests/test_deployment.py`. They use temporary
Ansible fixtures and never deploy the site.

### Disconnected site

Stage on a connected machine, copy `artifacts/` across, then deploy:

```bash
# Connected machine
ansible-playbook prestage.yml

# At the site, after copying artifacts/ into the repo
ansible-playbook site.yml -e ...
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

Two VMs on Proxmox:

| VM | OS | Role |
|---|---|---|
| `sophon-nfs` | Alpine | Exports `/export` — Proxmox storage, container data, artifact staging, Kopia repository |
| `sophon-infravm` | Fedora CoreOS | Runs every service as a rootless Podman container |

Services on InfraVM, deployed in this order:

| Service | Address | Purpose |
|---|---|---|
| Portainer | `https://<infravm_ip>:9443` | Container management, and the API Ansible deploys through |
| CoreDNS | `dns.<domain>` | Authoritative DNS for the zone; discovers containers and maintains tunnel ingress |
| Traefik | `traefik.<domain>` | Reverse proxy, TLS termination, ACME |
| OpenLDAP | `ldap.<domain>` | Directory |
| Keycloak | `auth.<domain>` | SSO, federated against OpenLDAP |
| Gitea | `git.<domain>` | Git server, SSO via Keycloak |
| Kopia | `kopia.<domain>` | Encrypted backup to the NFS VM over SFTP |

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
ansible-playbook --syntax-check site.yml prestage.yml

# Molecule role tests (roles/*/molecule/)
molecule test
```

`tests/test.yml` holds override values for local integration runs:
`ansible-playbook site.yml -e @tests/test.yml`.

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
 
