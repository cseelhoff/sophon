# Sophon

Self-contained, offline-capable platform appliance that deploys DNS, reverse proxy, SSO, directory, Git, unified artifact/container registry, container management, and encrypted backups onto a single **Fedora CoreOS** VM (**InfraVM**) across **Proxmox**, **VMware vCenter**, and **Microsoft Hyper-V**.

All services run as declarative rootless Podman Compose stacks reconciled on the guest itself, paired with an optional turnkey Alpine SFTP Backup Server VM for zero-downtime backups and safe standby disaster recovery.

---

## Installation & Operations

👉 **Full step-by-step installation, screenshots, split-DNS setup, restore drills, and certificate renewal:**
**[Sophon Operations Guide (`docs/operator-guide.md`)](docs/operator-guide.md)**

### Quick Start (Unattended Two-Command Deploy)

Requires only **Docker or Podman** on your workstation (Linux, WSL2, macOS, or Windows; on WSL2, clone inside `~/` rather than `/mnt/c/...` for fast ISO builds):

```sh
# 1. Build offline OS/app ISOs + mode-0600 secrets ISOs (prompts for unset values and offers to save .env)
sh sophon-build-isos.sh           # PowerShell: .\sophon-build-isos.ps1

# 2. Provision the SFTP Backup VM (if paired) and production InfraVM
sh sophon-provision.sh            # PowerShell: .\sophon-provision.ps1
```

- Deploying interactively on the VM console or onto an unmanaged hypervisor (e.g., VMware Workstation)? See **[Manual & Interactive Installation](docs/manual-installation.md)**.
- Using an existing NAS or custom Linux host for SFTP backups? See **[Manual Backup Server Setup](docs/manual-backup-server.md)**.

---

## Included Services

| Service | Endpoint | Role |
|---|---|---|
| **Appliance Status & Cutover UI** | `https://<domain>` (`status.<domain>`) | LAN-only appliance status, one-click standby-to-production promotion, and TLS certificate rotation |
| **Arcane** | `https://arcane.<domain>` | OIDC-authenticated Compose stack UI & Pure GitOps reconciler |
| **Keycloak** + **OpenLDAP** | `https://auth.<domain>`, `ldap.<domain>` | Central OIDC/SSO identity provider backed by OpenLDAP (`arcane-admins`, `nexus-admins`, `nexus-developers`) |
| **Gitea** | `https://git.<domain>` | Git hosting, Gitea Actions CI/CD, and file-backed Git LFS |
| **Sonatype Nexus Repository** | `https://nexus.<domain>`, `registry.<domain>`, `registry-push.<domain>` | Unified hosted + upstream caching proxy registry for OCI containers, PyPI, npm, APT, Maven, Cargo, and Raw artifacts |
| **Kopia** | `https://kopia.<domain>` | Encrypted, deduplicated `6h` hot backups (`pg_dumpall`, `slapcat`, Btrfs CoW volume staging) + optional weekly Btrfs snapshots over SFTP |
| **Traefik** + **CoreDNS** (+ **Cloudflared**) | `https://traefik.<domain>`, `dns.<domain>` | Wildcard Let's Encrypt TLS termination, container-aware split-horizon DNS, and optional Cloudflare Tunnel publishing |

---

## Key Capabilities

- **Online or fully air-gapped:** Every OS image, container archive, and runner dependency is baked into local ISOs during `sophon-build-isos`. Air-gapped sites carry a pre-issued 90-day Let's Encrypt wildcard certificate on `sophon-secrets.iso` and rotate it with one click in `https://<domain>`.
- **Zero-downtime encrypted backups:** Workloads opt in via `sophon.backup.*` container labels—hot logical database dumps and Btrfs CoW reflinks stream to an SFTP repository without stopping containers.
- **Unified Day-1 & Safe Standby recovery:** The same `sophon-secrets.iso` performs a Day-1 build when the SFTP repository is empty, or automatically boots into **Safe Standby Mode** (read-only SFTP, Cloudflare tunnel disabled, backups paused) and restores the latest snapshot when backups exist.
- **Reconstructible from code:** Nothing lives exclusively in UI state—rebuilding or upgrading is always *"deploy a clean VM from code, restore state from Kopia, and promote at `https://<domain>`."*

---

## Workstation Commands

| Script (`.sh` / `.ps1`) | Purpose |
|---|---|
| `sophon-build-isos` | Build/update offline OS, app, backup appliance, and mode-`0600` secrets ISOs (`--os-only` skips secrets ISOs; `--renew-certs` forces early TLS renewal) |
| `sophon-provision` | Deploy paired SFTP Backup Server VM + production InfraVM (`--recreate` replaces InfraVM; `backup` deploys only the Backup VM) |
| `sophon-provision-dev` | Recreate an isolated standby restore-drill VM (`SOPHON_DEV_INFRAVM_NAME` on `SOPHON_DEV_NETWORK_CIDR`) and verify full snapshot recovery |
| `sophon-mount-secrets` | Hot-attach a renewed `sophon-secrets.iso` to a running InfraVM for zero-downtime certificate rotation |
| `sophon-destroy-vm` | Stop and delete a Sophon-managed VM (`--vm-name <name>`) |

---

## Documentation

- **[Operations Guide](docs/operator-guide.md)** — End-to-end installation, Cloudflare token & split-DNS setup, verification, restore drills, production cutover, and monthly certificate renewal
- **[Manual & Interactive Installation](docs/manual-installation.md)** — Console-driven `sophon-deploy`, unmanaged hypervisors, and interactive recovery
- **[Manual Backup Server Setup](docs/manual-backup-server.md)** — Using an existing SFTP server/NAS or setting up the backup appliance interactively
- **[Configuration & Inputs Reference](docs/inputs.md)** — `.env` variables, hypervisor parameters, and version upgrade notes
- **[TLS Certificates](docs/certificates.md)** — Connected vs. air-gapped wildcard certificate lifecycle
- **[Architecture](docs/architecture.md)**, **[Design Decisions](docs/decisions.md)**, & **[Nexus + Pure GitOps Plan](docs/nexus-and-gitops-plan.md)**
- **[Kopia Backup & Restore Reference](roles/kopia/README.md)** — `sophon.backup.*` container labels, snapshot layout, and selective group recovery
