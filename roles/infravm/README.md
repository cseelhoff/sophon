# InfraVM Role

Deploys a Fedora CoreOS VM that runs all Sophon infrastructure services including Portainer, CoreDNS, Traefik, and other containerized applications.

## Architecture

InfraVM is a CoreOS-based VM that:
- Mounts NFS at `/mnt/nfs` for container image caching
- Runs Portainer for container management
- Optionally runs cloudflared for tunnel access (when `infravm_cloudflared_tunnel_token` is set)
- Loads container images from NFS cache or pulls from registry

## Requirements

- Proxmox VE with API access
- NFS server deployed (sophon-nfs storage in Proxmox)
- SSH key pair for Ansible access

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `infravm_enabled` | `true` | Enable/disable this role |
| `infravm_ansible_host` | `infra.{{ domain_name }}` (with cloudflared) or `infravm_ip` | SSH target for Ansible |
| `prestage_container_images` | See defaults | List of container images to load |
| `infravm_nfs_mount_options` | `ro,noatime,vers=4.1` | NFS mount options |

### VM Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `infravm_vm_name` | `sophon-infravm` | VM name in Proxmox |
| `infravm_cores` | `2` | CPU cores |
| `infravm_memory` | `4096` | Memory in MB |
| `infravm_disk_size` | `32G` | Root disk size |
| `infravm_ip` | (required) | Static IP address |
| `vnet_gateway` | (required) | Network gateway |
| `vnet_cidr` | `24` | Network CIDR |
| `infravm_username` | `core` | VM user name |
| `infravm_password_hash` | (default hash) | Password hash for user |
| `infravm_ssh_public_key` | `~/.ssh/id_rsa.pub` | SSH public key for core user |

### Portainer Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `portainer_admin_password` | (auto-generated) | Initial admin password |
| `portainer_port` | `9443` | HTTPS port for web UI |
| `portainer_image_tag` | `2.39.0` | Portainer CE image tag |

### Cloudflared Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `infravm_cloudflared_tunnel_token` | `$infravm_cloudflared_tunnel_token` | Tunnel token (enables cloudflared if set) |

## Dependencies

- `proxmox` role (for API authentication)

## Workflow

1. Creates CoreOS VM on Proxmox with:
   - NFS mount at `/mnt/nfs`
   - Conditional cloudflared service
   - Portainer systemd service
2. Waits for VM SSH access
3. Loads container images via SSH:
   - Checks NFS cache at `/mnt/nfs/containers/`
   - Uses `podman load` if cached, else `podman pull` and caches to NFS
4. Waits for Portainer API
5. Initializes Portainer admin user

## Example Playbook

```yaml
- hosts: localhost
  vars:
    infravm_ip: "10.1.1.100"
    vnet_gateway: "10.1.1.1"
    nfs_ip: "10.1.1.35"
  roles:
    - infravm
```

## Output

After successful deployment, displays:
- SSH access information
- Portainer URL and credentials
- NFS mount status
- Cloudflared status (if enabled)

## Notes

- Container images are cached on NFS for future deployments and air-gapped use
- When cloudflared is enabled, Ansible connects via tunnel hostname instead of internal IP
- Portainer admin password is auto-generated if not provided (save it!)
