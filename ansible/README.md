# Sophon Ansible Playbooks

Infrastructure as Code for deploying homelab services on Fedora CoreOS with Podman.

## Quick Start

Deploy a complete homelab in one command:

```bash
cd ansible && ansible-playbook -i inventories/development/inventory.yml site.yml
```

That's it! You'll be prompted for:
1. **Proxmox API host** (e.g., `192.168.1.100` or `pve.example.com`)
2. **Domain name** (e.g., `homelab.local`)
3. **Proxmox password** (hidden input)

The playbook will then:
1. Prompt for your Proxmox password (API authentication)
2. Use your existing `~/.ssh/id_rsa` key (or generate one)
3. Download Fedora CoreOS to Proxmox storage
4. Create and boot a CoreOS VM with DHCP networking
5. Deploy Portainer, Traefik, CoreDNS, and all enabled services

### Air-Gapped Proxmox

If your Proxmox server can't reach the internet, the playbook will fail gracefully with SSH/console commands you can run manually to transfer the CoreOS image.

### Scripting / CI (Skip Prompts)

For non-interactive use, provide values via `-e` flags:

```bash
ansible-playbook -i inventories/development/inventory.yml site.yml \
  -e coreos_proxmox_api_host=pve.example.com \
  -e domain_name=homelab.local \
  -e coreos_proxmox_api_password=secret
```

### Common Options

```bash
# Use a specific SSH key
ansible-playbook -i inventories/development/inventory.yml site.yml \
  -e ssh_private_key_path=~/.ssh/my_key

# Use static IP instead of DHCP
ansible-playbook -i inventories/development/inventory.yml site.yml \
  -e coreos_network_mode=static \
  -e coreos_ip=10.0.0.100

# Specify Proxmox storage
ansible-playbook -i inventories/development/inventory.yml site.yml \
  -e coreos_proxmox_storage=local-zfs
```

### Inventory-Based Deployment

For repeatable or multi-environment deployments, use inventory files:

```bash
# Install Ansible collections
ansible-galaxy install -r requirements.yml

# Development
ansible-playbook -i inventories/development/inventory.yml site.yml

# Production (with vault)
ansible-playbook -i inventories/production/inventory site.yml --ask-vault-pass

# Deploy specific services
ansible-playbook -i inventories/development/inventory.yml site.yml --tags "traefik,homepage"

# Dry run
ansible-playbook -i inventories/development/inventory.yml site.yml --check
```

## Prerequisites

- Python 3.10+
- Ansible 2.15+
- Access to Proxmox API (port 8006)
- SSH key for CoreOS access (auto-generated if needed)

## Directory Structure

```
ansible/
├── ansible.cfg           # Ansible configuration
├── requirements.yml      # Galaxy dependencies
├── site.yml             # Master playbook
├── inventories/
│   ├── development/     # Dev environment
│   │   ├── inventory.yml
│   │   └── group_vars/
│   │       └── all.yml
│   └── production/      # Prod environment
│       ├── inventory
│       └── group_vars/
│           ├── all.yml
│           └── vault.yml.example
├── roles/               # Service roles
│   ├── coreos/          # Base VM provisioning
│   ├── portainer/       # Container management
│   ├── portainer_stack/ # Stack deployment API
│   ├── traefik/         # Reverse proxy
│   ├── coredns/         # DNS server
│   ├── homepage/        # Dashboard
│   ├── gitea/           # Git server
│   ├── nexus/           # Artifact repository
│   ├── openldap/        # Directory service
│   ├── keycloak/        # Identity/SSO
│   ├── nextcloud/       # File sync
│   ├── n8n/             # Workflow automation
│   ├── guacamole/       # Remote desktop gateway
│   ├── backup/          # Volume backups
│   └── kopia/           # Filesystem backups
└── tests/
    ├── molecule/        # Test configurations
    ├── inventories/     # Test inventories
    ├── test.yml         # Test variables
    ├── test.sh          # Molecule test runner
    └── test-vagrant.sh  # Vagrant integration test
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

## Podman Compatibility

All roles are designed for Fedora CoreOS with Podman:

- Uses `podman` commands instead of Docker
- Compose files deployed via Portainer API
- Container images pulled directly from registries
- Optional air-gapped mode for offline deployments
- Systemd socket activation for rootless containers

## Air-Gapped Deployment

By default, the playbook pulls container images directly from registries. For fully air-gapped environments:

```bash
ansible-playbook -i localhost, ansible/site.yml \
  -e proxmox_api_host=pve.example.com \
  -e domain_name=homelab.local \
  -e airgapped_mode=true
```

This starts a local HTTP file server and pre-loads images from tarball files. To prepare images:

1. Build images on a connected machine (or use Gitea Actions CI):
   ```bash
   docker pull traefik:v3.2
   docker save -o traefik.tar traefik:v3.2
   ```

2. Place tarball files in the HTTP file server directory

3. Ansible serves and loads images via:
   ```bash
   curl -o image.tar http://fileserver:8000/image.tar
   podman load -i image.tar
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
