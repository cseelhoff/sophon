# Bastion Role

Deploys an Alpine Linux privileged access workstation (PAW) on Proxmox with Cloudflared tunnel support for remote access to the Sophon network.

## Purpose

This role solves a common bootstrapping problem: **How do you run Ansible playbooks against VMs in a network you can't directly reach?**

Many users can connect to their Proxmox hypervisor (via VPN, Cloudflare tunnel, or direct access), but don't have a machine inside the same network where CoreOS and other Sophon VMs are deployed. The bastion VM provides:

1. **Network Access**: A lightweight VM inside the target network that can reach all other Sophon VMs
2. **Remote Access**: Cloudflared tunnel for secure access from anywhere without exposing SSH to the internet
3. **Pre-installed Tools**: All necessary tooling to run the complete Sophon playbook (Ansible, Butane, Skopeo, etc.)
4. **Low Overhead**: Alpine Linux runs efficiently with minimal resources (~200MB RAM idle)

## Typical Workflow

```
┌──────────────┐      Cloudflare      ┌──────────────┐      Internal      ┌──────────────┐
│   Operator   │ ──── Tunnel ──────▶  │   Bastion    │ ──── Network ───▶  │   CoreOS     │
│  (anywhere)  │                      │   (Alpine)   │                    │   (Sophon)   │
└──────────────┘                      └──────────────┘                    └──────────────┘
                                             │
                                             ├──▶ NFS Server
                                             ├──▶ Other Services
                                             └──▶ Proxmox API
```

1. Run `bastion.yml` playbook from any machine with Proxmox API access
2. SSH into bastion via Cloudflared tunnel (or direct IP if on same network)
3. Run the main `site.yml` playbook from within the bastion

## Requirements

- Proxmox VE with API access
- SSH key pair for bastion access
- (Optional) Cloudflare account for tunnel access

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `bastion_enabled` | `true` | Enable/disable this role |
| `bastion_vm_name` | `sophon-bastion` | VM name in Proxmox |
| `bastion_cores` | `2` | CPU cores |
| `bastion_memory` | `1024` | RAM in MB |
| `bastion_disk_size` | `8G` | Disk size |
| `bastion_username` | `ansible` | User account to create |
| `bastion_password_hash` | - | Password hash (mkpasswd --method=sha512) |
| `bastion_ssh_public_key` | - | SSH public key for passwordless access |
| `bastion_ip` | - | Static IP for the VM (uses DHCP if not set) |
| `bastion_gateway` | - | Gateway (required if bastion_ip is set) |
| `bastion_cidr` | `24` | Network CIDR |
| `bastion_gateway` | - | Default gateway (required) |
| `bastion_dns` | `1.1.1.1` | DNS server |
| `bastion_alpine_version` | `3.21` | Alpine Linux version |
| `bastion_cloudflared_token` | - | Cloudflare tunnel token (optional) |
| `bastion_cloudflared_tunnel_name` | `sophon-bastion` | Tunnel name |
| `bastion_clone_sophon` | `true` | Clone Sophon repo at boot |
| `bastion_sophon_repo_url` | - | Git URL for Sophon repository |
| `bastion_butane_version` | `0.23.0` | Butane version to install |

## Dependencies

- `proxmox` role (for API authentication)

## Example Playbook

```yaml
- hosts: localhost
  roles:
    - role: bastion
      vars:
        bastion_ip: "10.0.1.5"
        bastion_gateway: "10.0.1.1"
        bastion_ssh_public_key: "ssh-ed25519 AAAA..."
        bastion_cloudflared_token: "eyJhI..."
        bastion_sophon_repo_url: "https://github.com/myorg/sophon.git"
```

## Cloudflare Tunnel Setup

### Option 1: Pre-generated Token (Automated)

```bash
# On your local machine with cloudflared installed
cloudflared tunnel login
cloudflared tunnel create sophon-bastion
cloudflared tunnel token sophon-bastion
# Copy the token to bastion_cloudflared_token variable
```

### Option 2: Manual Setup (After VM Creation)

```bash
# SSH into the bastion
ssh ansible@<bastion_ip>

# Login and create tunnel
cloudflared tunnel login
cloudflared tunnel create sophon-bastion

# Configure the tunnel
cat > /etc/cloudflared/config.yml << EOF
tunnel: sophon-bastion
credentials-file: /root/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: ssh.bastion.example.com
    service: ssh://localhost:22
  - service: http_status:404
EOF

# Start and enable
rc-update add cloudflared default
rc-service cloudflared start
```

### Connecting via Tunnel

```bash
# Add to ~/.ssh/config
Host bastion
  HostName ssh.bastion.example.com
  User ansible
  ProxyCommand cloudflared access ssh --hostname %h

# Then simply
ssh bastion
```

## Notes

- The VM uses Alpine Linux's cloud image with cloud-init for configuration
- QEMU guest agent is installed for better Proxmox integration
- All Ansible dependencies for Sophon are pre-installed
- The bastion is intentionally minimal - no GUI, no unnecessary services
- Consider restricting firewall rules on the bastion to only necessary outbound connections
