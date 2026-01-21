# Sophon

**Automated homelab infrastructure deployment using Ansible and IaC principles.**

<img src="./sophon.drawio.svg">

## Overview

Sophon automates deploying a complete homelab infrastructure on Proxmox, including:
- Fedora CoreOS VMs with Podman/Portainer
- DNS (CoreDNS), reverse proxy (Traefik), SSL via Cloudflare
- Identity management (OpenLDAP, Keycloak SSO)
- Collaboration tools (Nextcloud, Gitea)
- Remote access (Guacamole)
- Artifact hosting (Sonatype Nexus)
- Automation and dashboards (n8n, Homepage)
- Backup solutions (docker-volume-backup, Kopia)

This project follows an **air-gapped deployment** pattern: container images are pre-downloaded on an internet-connected bootstrap machine, then transferred to isolated infrastructure via HTTP file server.

## Architecture

| Layer | Components |
|-------|------------|
| **Network** | OPNSense firewall, VLANs (Users 10.0.3.x, Infra 10.0.1.x) |
| **Compute** | Proxmox hypervisor, Fedora CoreOS VMs |
| **Containers** | Podman + Portainer for container management |
| **Services** | CoreDNS, Traefik, Nexus, OpenLDAP, Keycloak, Gitea, Nextcloud, n8n, Homepage, Guacamole |
| **Backup** | docker-volume-backup (volumes), Kopia (filesystem) |
| **External** | Cloudflare tunnels for remote access, OneDrive for backups |

## Service Status

| Service | Status | Port(s) | Description |
|---------|--------|---------|-------------|
| CoreOS VM | ✅ Ready | - | Fedora CoreOS on Proxmox |
| Portainer | ✅ Ready | 9443 | Container management |
| CoreDNS | ✅ Ready | 53 | DNS server |
| DNS Sync | ✅ Ready | - | Traefik→CoreDNS sync |
| Traefik | ✅ Ready | 80, 443, 8080 | Reverse proxy + SSL |
| Homepage | ✅ Ready | 3000 | Admin dashboard |
| Gitea | ✅ Ready | 3001, 222 | Git + CI/CD |
| Nexus | ✅ Ready | 8081, 5000 | Artifact repository |
| OpenLDAP | ✅ Ready | 389, 636, 8089 | Directory service |
| Keycloak | ✅ Ready | 8180 | SSO/Identity |
| Nextcloud | ✅ Ready | 8083 | File sync & share |
| n8n | ✅ Ready | 5678 | Workflow automation |
| Guacamole | ✅ Ready | 8090 | Remote desktop gateway |
| Backup | ✅ Ready | - | Volume backups |
| Kopia | ✅ Ready | 51515 | Filesystem backups |

## Backup Strategy

### Configuration Backups (text-based, git-versioned)
Low-churn, auditable configs that benefit from line-by-line change tracking:
- Ansible playbooks, docker-compose files
- OPNSense/switch configs (XML/JSON export)
- Traefik, Keycloak, n8n configs (YAML/JSON)
- OpenLDAP schemas/groups (LDIF export)
- Gitea repository mirrors

### Operational Backups (binary, volume/dump)
High-churn data where text conversion adds no value:
- Nextcloud (DB dump + file rsync)
- PostgreSQL databases (pg_dump)
- Nexus artifacts (volume backup)
- OpenLDAP user data (slapcat)
- Gitea metadata DB

### Backup Tools
- **docker-volume-backup** (`backup` role): Scheduled volume backups with compression
- **Kopia** (`kopia` role): Deduplicating filesystem backups with encryption
- **rclone**: Sync to OneDrive with versioning
- **Ansible/Gitea Actions**: Orchestration

### Storage Targets
1. Local NAS (primary, fast restore)
2. OneDrive (offsite, versioned)

## CI/CD Approach

**Gitea Actions** for git-triggered IaC deployments:
- Merge to main → Ansible playbook executes automatically
- `--check --diff` dry-run on pull requests
- Secrets stored in Gitea (encrypted)
- Container images built and exported for air-gapped deployment

See `.gitea/workflows/` for CI configuration.

## Prerequisites

- **Bootstrap machine** (Linux with internet access)
  - Ansible 2.15+ installed
  - Podman for downloading container images
- **Proxmox hypervisor**
  - API access configured
  - Storage for VMs

## Quick Start

```bash
# Enter development shell (if using Nix)
nix develop

# Install Ansible dependencies
cd ansible/
ansible-galaxy install -r requirements.yml

# Configure variables
cp inventories/production/group_vars/vault.yml.example inventories/production/group_vars/vault.yml
nano inventories/development/group_vars/all.yml

# Optional: Set Proxmox API password
read -s PROXMOX_API_PASSWORD && export PROXMOX_API_PASSWORD

# Deploy everything
ansible-playbook -i inventories/development/inventory.yml site.yml

# Deploy specific services
ansible-playbook -i inventories/development/inventory.yml site.yml --tags "traefik,homepage"

# Production deployment with vault
ansible-playbook -i inventories/production/inventory site.yml --ask-vault-pass
```

## Project Structure

```
sophon/
├── flake.nix                    # Nix development environment
├── .gitea/workflows/            # Gitea Actions CI/CD
│   ├── ansible-lint.yml         # Linting workflow
│   └── build-images.yml         # Container image builds
├── ansible/
│   ├── site.yml                 # Master playbook
│   ├── ansible.cfg              # Ansible configuration
│   ├── requirements.yml         # Galaxy dependencies
│   ├── .ansible-lint            # Linting rules
│   ├── .yamllint                # YAML linting
│   ├── inventories/
│   │   ├── development/         # Dev environment
│   │   │   └── group_vars/all.yml
│   │   └── production/          # Prod environment
│   │       └── group_vars/
│   │           ├── all.yml
│   │           └── vault.yml.example
│   ├── roles/
│   │   ├── coreos/              # Provision VM on Proxmox
│   │   ├── portainer/           # Container management
│   │   ├── portainer_stack/     # Stack deployment API
│   │   ├── coredns/             # DNS server
│   │   ├── dns_sync/            # Traefik→DNS sync sidecar
│   │   ├── traefik/             # Reverse proxy + SSL
│   │   ├── homepage/            # Dashboard
│   │   ├── gitea/               # Git server + Actions
│   │   ├── nexus/               # Artifact repository
│   │   ├── openldap/            # Directory service
│   │   ├── keycloak/            # SSO/Identity
│   │   ├── nextcloud/           # File sync
│   │   ├── n8n/                 # Workflow automation
│   │   ├── guacamole/           # Remote desktop
│   │   ├── backup/              # Volume backups
│   │   └── kopia/               # Filesystem backups
│   └── tests/
│       ├── molecule/            # Test configurations
│       └── test.sh              # Molecule test runner
├── old_bash_version/            # Legacy bash implementation (reference)
└── ansible-nas/                 # Reference project for patterns
```

## SSO Integration

Keycloak provides centralized authentication for all services:
- Gitea (OIDC)
- Nextcloud (OIDC)
- Guacamole (OIDC)
- n8n (OIDC)
- Portainer (OIDC)

Enable SSO in `group_vars/all.yml`:
```yaml
gitea_oauth_enabled: true
nextcloud_oidc_enabled: true
guacamole_oidc_enabled: true
n8n_oidc_enabled: true
```

## Testing

```bash
# Molecule tests (per-role)
cd ansible && ./tests/test.sh

# Integration test on dev CoreOS
ansible-playbook -i inventories/development/inventory.yml site.yml
ssh admin@<coreos-ip> "podman ps"
```

## Documentation

- [Ansible README](ansible/README.md) - Detailed Ansible documentation
- [Vault Guide](ansible/README.md#secrets-management-with-ansible-vault) - Secrets management
- [SSO Setup](ansible/README.md#sso-integration-keycloak) - Keycloak configuration

## Reference Projects
- [ansible-nas](https://github.com/davestephens/ansible-nas): Role organization patterns
- [notthebee/infra](https://github.com/notthebee/infra): Homelab automation
- [TechnoTim/k3s-ansible](https://github.com/techno-tim/k3s-ansible): Kubernetes deployment

## Inspiration
- [Jim's Garage](https://www.youtube.com/@yourlounge) (YouTube)
- [TechnoTim](https://www.youtube.com/@TechnoTim) (YouTube)
