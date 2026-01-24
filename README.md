# Sophon Ansible Playbooks

Infrastructure as Code for deploying homelab services on Fedora CoreOS with Podman.

## Architecture Overview

```
                    ┌────────────────────────────────────────────────────────┐
                    │                   DEPLOYMENT MODES                      │
                    ├──────────────────────┬─────────────────────────────────┤
                    │     ONLINE MODE      │        AIR-GAPPED MODE          │
                    │  (Internet Access)   │    (Disconnected Network)       │
                    └──────────┬───────────┴─────────────┬───────────────────┘
                               │                         │
                               │                   ┌─────▼─────────┐
                               │                   │ prestage.yml  │ Phase 1
                               │                   │ (on internet) │ Download
                               │                   └─────┬─────────┘
                               │                         │
                               │                   ══════╪══════ NETWORK SWITCH
                               │                         │
                               │                   ┌─────▼─────────┐
                               │                   │nfs-upload.yml │ Phase 2
                               │                   │ (air-gapped)  │ Upload
                    ┌──────────▼───────────┐       └─────┬─────────┘
                    │ Proxmox downloads &  │             │
                    │ uploads to NFS       │             │
                    └──────────┬───────────┘             │
                               │                         │
                               └───────────┬─────────────┘
                                           ▼
                               ┌───────────────────────┐
                               │   Deploy CoreOS VM    │
                               └───────────┬───────────┘
                                           ▼
                               ┌───────────────────────┐  YES   ┌──────────────┐
                               │  CoreOS Reachable?    ├───────▶│  Continue to │
                               └───────────┬───────────┘        │  Portainer   │
                                           │ NO                 └──────────────┘
                                           ▼                            ▲
                               ┌───────────────────────┐                │
                               │  Deploy Bastion VM    │                │
                               │  (+Cloudflared?)      │                │
                               └───────────┬───────────┘                │
                                           ▼                            │
                               ┌───────────────────────┐  YES           │
                               │ SSH Reachable?        ├────────────────┤
                               │ (direct/cloudflared)  │ (ProxyJump)    │
                               └───────────┬───────────┘                │
                                           │ NO                         │
                                           ▼                            │
                               ┌───────────────────────┐                │
                               │  QGA Remote Exec      ├────────────────┘
                               │  (via Proxmox API)    │
                               └───────────────────────┘
```

## Quick Start

### Online Mode (Recommended)

Deploy a complete homelab in one command:

```bash
nix develop  # Enter development shell
ansible-playbook site.yml -i inventories/production/inventory.yml
```

You'll be prompted for:
1. **Proxmox API host** (e.g., `192.168.1.100` or `pve.example.com`)
2. **Domain name** (e.g., `homelab.local`)
3. **Proxmox password** (hidden input)

The playbook will then:
1. Download artifacts to NFS (via Proxmox)
2. Create and boot a CoreOS VM with Ignition config
3. Verify connectivity (or deploy bastion if unreachable)
4. Deploy Portainer, Traefik, CoreDNS, and all enabled services

### Air-Gapped Mode

For networks without internet access:

```bash
# ═══════════════════════════════════════════════════════════════════
# PHASE 1: Download artifacts (on internet-connected machine)
# ═══════════════════════════════════════════════════════════════════
nix develop
ansible-playbook prestage.yml

# Artifacts saved to /tmp/sophon_prestage/

# ═══════════════════════════════════════════════════════════════════
# SWITCH NETWORK: Disconnect from internet, connect to air-gapped LAN
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# PHASE 2: Upload to NFS server
# ═══════════════════════════════════════════════════════════════════
ansible-playbook nfs-upload.yml -i inventories/production/inventory.yml

# ═══════════════════════════════════════════════════════════════════
# PHASE 3: Deploy infrastructure
# ═══════════════════════════════════════════════════════════════════
ansible-playbook site.yml -i inventories/production/inventory.yml -e airgapped_mode=true
```

### With Cloudflared Tunnel (Remote Access)

If you can't directly SSH to the deployed VMs but have a Cloudflare account:

```bash
export CLOUDFLARED_TUNNEL_TOKEN="your-tunnel-token"
ansible-playbook site.yml -i inventories/production/inventory.yml
```

The bastion VM will automatically configure Cloudflared for remote access.

### Scripting / CI (Skip Prompts)

```bash
ansible-playbook site.yml -i inventories/production/inventory.yml \
  -e coreos_proxmox_api_host=pve.example.com \
  -e domain_name=homelab.local \
  -e coreos_proxmox_api_password=secret
```

## Key Playbooks

| Playbook | Purpose |
|----------|---------|
| `site.yml` | Full orchestration - deploys everything |
| `prestage.yml` | Air-gapped Phase 1 - downloads artifacts |
| `nfs-upload.yml` | Air-gapped Phase 2 - uploads to NFS |
| `deploy.yml` | Deployment only (skips prestaging) |
| `coreos.yml` | CoreOS VM only |
| `bastion.yml` | Bastion VM only |
| `nfs-content.yml` | NFS content population only |

## Common Options

## Prerequisites

- Python 3.10+
- Ansible 2.15+
- Access to Proxmox API

## Directory Structure

```
sophon/
├── flake.nix             # Nix development environment
├── ansible.cfg           # Ansible configuration
├── requirements.yml      # Galaxy dependencies
├── site.yml              # Master orchestration playbook
├── prestage.yml          # Air-gapped Phase 1 (download)
├── nfs-upload.yml        # Air-gapped Phase 2 (upload)
├── deploy.yml            # Deployment playbook
├── coreos.yml            # CoreOS VM playbook
├── bastion.yml           # Bastion VM playbook
├── inventories/
│   ├── development/      # Dev environment
│   │   ├── inventory.yml
│   │   └── group_vars/
│   │       └── all.yml
│   └── production/       # Prod environment
│       ├── inventory.yml
│       └── group_vars/
│           ├── all.yml
│           └── vault.yml.example
├── roles/
│   ├── proxmox/          # Proxmox API interactions
│   ├── coreos/           # CoreOS VM provisioning
│   ├── bastion/          # Alpine bastion VM with Cloudflared
│   ├── nfs_content/      # NFS artifact management
│   ├── connectivity_check/ # SSH reachability testing
│   ├── qga_remote_exec/  # QGA-based remote execution
│   ├── portainer/        # Container management
│   ├── portainer_stack/  # Stack deployment API
│   ├── traefik/          # Reverse proxy
│   ├── coredns/          # DNS server
│   ├── homepage/         # Dashboard
│   ├── gitea/            # Git server
│   ├── nexus/            # Artifact repository
│   ├── openldap/         # Directory service
│   ├── keycloak/         # Identity/SSO
│   ├── nextcloud/        # File sync
│   ├── n8n/              # Workflow automation
│   ├── guacamole/        # Remote desktop gateway
│   ├── backup/           # Volume backups
│   └── kopia/            # Filesystem backups
└── tests/
    ├── molecule/         # Test configurations
    └── test.sh           # Molecule test runner
```

## Secrets Management with Ansible Vault

### Initial Setup

```bash
# Create vault password file (do NOT commit this)
echo "your-vault-password" > ~/.vault_pass
chmod 600 ~/.vault_pass

# Copy vault template
cp inventories/production/group_vars/vault.yml.example inventories/production/group_vars/vault.yml

# Encrypt the vault
ansible-vault encrypt inventories/production/group_vars/vault.yml
```

### Editing Secrets

```bash
# Edit encrypted vault
ansible-vault edit inventories/production/group_vars/vault.yml

# View encrypted vault
ansible-vault view inventories/production/group_vars/vault.yml

# Re-encrypt with new password
ansible-vault rekey inventories/production/group_vars/vault.yml
```

### Running Playbooks with Vault

```bash
# Interactive password prompt
ansible-playbook -i inventories/production/inventory site.yml --ask-vault-pass

# Using password file
ansible-playbook -i inventories/production/inventory site.yml --vault-password-file ~/.vault_pass

# Environment variable
export ANSIBLE_VAULT_PASSWORD_FILE=~/.vault_pass
ansible-playbook -i inventories/production/inventory site.yml
```

## SSO Integration (Keycloak)

Keycloak provides single sign-on for all services. To enable:

1. Set `keycloak_enabled: true` in inventory
2. Configure OIDC for each service:

```yaml
# group_vars/all.yml or vault.yml
gitea_oauth_enabled: true
gitea_oauth_client_secret: "{{ vault_keycloak_gitea_client_secret }}"

nextcloud_oidc_enabled: true
nextcloud_oidc_client_secret: "{{ vault_keycloak_nextcloud_client_secret }}"

guacamole_oidc_enabled: true
n8n_oidc_enabled: true
```

3. Generate client secrets:
```bash
openssl rand -hex 32
```

4. The realm is auto-provisioned via `realm-export.json.j2`

## Role Enable/Disable

Each role can be enabled/disabled via inventory variables:

```yaml
# group_vars/all.yml
traefik_enabled: true
nextcloud_enabled: false
n8n_enabled: true
guacamole_enabled: true
backup_enabled: true
kopia_enabled: true
```

## Deployment Order

The `site.yml` playbook deploys services in dependency order:

1. **coreos** - Provision base VM
2. **portainer** - Container management
3. **coredns** - DNS server
4. **traefik** - Reverse proxy
5. **homepage** - Dashboard
6. **gitea** - Git server
7. **nexus** - Artifacts
8. **openldap** - Directory
9. **keycloak** - SSO (depends on openldap if LDAP enabled)
10. **nextcloud** - Files
11. **n8n** - Automation
12. **guacamole** - Remote desktop
13. **backup** - Volume backups
14. **kopia** - Filesystem backups

## Podman & NFS Architecture

All roles are designed for Fedora CoreOS with Podman:

- Uses `podman` commands (no Docker daemon required)
- Compose files deployed via Portainer API
- Container images loaded from NFS-served tarballs
- RPM packages installed from NFS at first boot
- Systemd socket activation for rootless containers

### NFS Server Layout

The NFS server (default `10.1.1.35`) serves as:
- **Nexus docker-proxy backend**: `/exports/nexus/docker-proxy/`
- **Nexus yum-proxy backend**: `/exports/nexus/yum-proxy/`
- **Proxmox ISO storage**: `/var/lib/vz/template/iso/`

This allows Nexus to proxy cached artifacts without internet access.

## Air-Gapped Deployment

For fully disconnected networks, follow this three-phase workflow:

### Phase 1: Prestage (Internet Required)

Downloads all artifacts to local staging directory:

```bash
nix develop
ansible-playbook prestage.yml
```

**Downloaded artifacts** (`/tmp/sophon_prestage/`):
- Fedora CoreOS QCOW2 image
- Alpine Linux cloud image (bastion)
- Container images: Portainer, Traefik, CoreDNS, OpenLDAP, Nexus, etc.
- RPM packages: qemu-guest-agent + dependencies
- Butane binary

### Phase 2: NFS Upload (Air-Gapped Network)

After switching networks:

```bash
ansible-playbook nfs-upload.yml -i inventories/production/inventory.yml
```

**NFS destinations** (on `nfs_server_ip`):
- `/var/lib/vz/template/iso/` - VM images
- `/exports/nexus/docker-proxy/` - Container tarballs
- `/exports/nexus/yum-proxy/` - RPM packages
- `/exports/sophon/bin/` - Butane binary

### Phase 3: Deploy

```bash
ansible-playbook site.yml -i inventories/production/inventory.yml -e airgapped_mode=true
```

### Key Variables

```yaml
# group_vars/all.yml
airgapped_mode: false          # Set true for air-gapped
nfs_server_ip: "10.1.1.35"     # NFS server address
nfs_docker_images_path: "/exports/nexus/docker-proxy"
nfs_rpm_packages_path: "/exports/nexus/yum-proxy"
```

## Bastion VM & Remote Access

When the Ansible controller cannot directly reach the CoreOS VM, the workflow
automatically deploys an Alpine-based bastion VM on Proxmox.

### Access Methods (in priority order)

1. **Cloudflared Tunnel** - If `CLOUDFLARED_TUNNEL_TOKEN` is set
   - Bastion configures Cloudflared daemon
   - Ansible uses ProxyJump through tunnel

2. **Direct SSH** - If bastion is reachable
   - Ansible uses SSH ProxyJump through bastion

3. **QGA Remote Execution** - Last resort
   - Uploads sophon.tgz via Proxmox QGA file-write API
   - Executes ansible-playbook via QGA exec API
   - Environment variables passed as base64-encoded block

### Manual Bastion Deployment

```bash
# Without Cloudflared
ansible-playbook bastion.yml -i inventories/production/inventory.yml

# With Cloudflared
CLOUDFLARED_TUNNEL_TOKEN=xxx ansible-playbook bastion.yml -i inventories/production/inventory.yml
```

## Testing

### Molecule (Role Testing)

```bash
# Install test dependencies
pip install molecule molecule-docker ansible-lint yamllint

# Run all molecule tests
./tests/test.sh

# Test a specific role
cd roles/traefik
molecule -c ../../tests/molecule/base.yml test
```

### Integration Testing (Dev CoreOS VM)

For full integration testing, deploy to a development CoreOS VM:

```bash
# Deploy to dev environment
ansible-playbook -i inventories/development/inventory.yml site.yml

# Verify services
ssh admin@<coreos-ip> "podman ps"
curl -k https://<coreos-ip>:9443/api/status  # Portainer
curl http://<coreos-ip>:3000                  # Homepage
```

## Linting

```bash
# YAML lint
yamllint -c .yamllint .

# Ansible lint
ansible-lint -c .ansible-lint

# Pre-commit (if configured)
pre-commit run --all-files
```

## Troubleshooting

### Common Issues

**SSH Connection Refused**
```bash
# Check SSH key permissions
chmod 600 ~/.ssh/sophon_prod
ssh -i ~/.ssh/sophon_prod admin@10.0.0.100
```

**Portainer API Errors**
```bash
# Verify Portainer is running
curl -k https://10.0.0.100:9443/api/status
```

**Image Load Failures**
```bash
# Verify HTTP file server
curl http://localhost:8000/traefik.tar -o /dev/null -w "%{http_code}"
```

**Stack Deployment Failures**
```bash
# Check Portainer logs on CoreOS
ssh admin@10.0.0.100 "podman logs portainer"
```

### Logs

```bash
# View container logs on CoreOS
ssh admin@coreos-ip "podman logs <container-name>"

# View systemd journal
ssh admin@coreos-ip "journalctl -u podman"
```

## Configuration Reference

See [inventories/development/group_vars/all.yml](inventories/development/group_vars/all.yml) for all available variables.

Key settings:
- `domain_name` - Base domain for all services
- `coreos_ip` - Target CoreOS VM IP
- `portainer_port` - Portainer API port (default: 9443)
- `http_file_server_port` - Image distribution server port (default: 8000)
- `validate_certs` - TLS certificate validation (default: false for dev)

## Contributing

1. Create a feature branch
2. Add/modify roles following ansible-nas patterns
3. Include molecule tests for new roles
4. Run linting before submitting PR
5. Update documentation as needed
