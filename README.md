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

Prestage is mandatory even when the target has full internet access. It costs
one extra command and it is the only thing that keeps the disconnected path
working without a second, untested code path — see
[ADR-0001](docs/adr/0001-air-gapped-is-the-only-deployment-mode.md).

## Quick start

```bash
nix develop

# Phase 1 — connected. Populates artifacts/.
ansible-playbook prestage.yml

# Phase 2 — provisions the site.
ansible-playbook site.yml \
  -e proxmox_host=10.0.60.2 \
  -e proxmox_password=<password> \
  -e domain_name=example.com \
  -e nfs_ip=10.0.60.10 \
  -e infravm_ip=10.0.60.11
```

Read [docs/preconditions.md](docs/preconditions.md) first. Several required
inputs — a publicly registered domain, a Cloudflare API token, a Portainer
Business Edition licence — are not obvious from the command line.

Generated passwords and the addresses you chose are written to `artifacts/` and
reused on later runs, so re-running is safe. Anything passed with `-e` always
wins and is never written to disk
([ADR-0008](docs/adr/0008-generated-state-persists-under-artifacts.md)).

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

Passwords and tokens are supplied with `-e` on the command line, or generated
and persisted under `artifacts/`. Never commit them to a tracked file.

See [docs/preconditions.md](docs/preconditions.md) for the full list of inputs
and [ADR-0008](docs/adr/0008-generated-state-persists-under-artifacts.md) for
how generated state is stored.

## Testing

```bash
# Lint
yamllint -c .yamllint .
ansible-lint -c .ansible-lint

# Molecule role tests
./tests/test.sh
```

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
 
