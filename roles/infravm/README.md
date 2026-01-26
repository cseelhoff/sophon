# InfraVM Role

Deploys a Fedora CoreOS VM that runs all Sophon infrastructure services including Portainer, CoreDNS, Traefik, and other containerized applications.

## Architecture

InfraVM is a CoreOS-based VM that:
- Mounts NFS at `/mnt/nfs` for container image caching
- Runs Portainer for container management
- Optionally runs cloudflared for tunnel access (when `CLOUDFLARED_TUNNEL_TOKEN` is set)
- Loads container images from NFS cache or pulls from registry

## Requirements

- Proxmox VE with API access
- NFS server deployed (sophon-nfs storage in Proxmox)
- SSH key pair for Ansible access

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `infravm_enabled` | `true` | Enable/disable this role |
| `infravm_ansible_host` | `infravm.{{ domain_name }}` (with cloudflared) or `coreos_base_ip` | SSH target for Ansible |
| `infravm_container_images` | See defaults | List of container images to load |
| `infravm_nfs_mount_options` | `ro,noatime,vers=4.1` | NFS mount options |

### VM Configuration (passed to coreos_base)

| Variable | Default | Description |
|----------|---------|-------------|
| `coreos_base_vm_name` | `sophon-infravm` | VM name in Proxmox |
| `coreos_base_cores` | `2` | CPU cores |
| `coreos_base_memory` | `4096` | Memory in MB |
| `coreos_base_disk_size` | `32G` | Root disk size |
| `coreos_base_ip` | (required) | Static IP address |
| `coreos_base_gateway` | (required) | Network gateway |
| `coreos_base_ssh_public_key` | `~/.ssh/id_rsa.pub` | SSH public key for admin user |

### Portainer Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `portainer_admin_password` | (auto-generated) | Initial admin password |
| `portainer_port` | `9443` | HTTPS port for web UI |
| `portainer_image_tag` | `2.27.1` | Portainer CE image tag |

### Cloudflared Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `cloudflared_tunnel_token` | `$CLOUDFLARED_TUNNEL_TOKEN` | Tunnel token (enables cloudflared if set) |

## Dependencies

- `coreos_base` role (included via meta/main.yml)
- `proxmox` role (for API authentication)

## Workflow

1. Creates CoreOS VM via `coreos_base` role with:
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
    coreos_base_ip: "10.1.1.100"
    coreos_base_gateway: "10.1.1.1"
    nfs_server_ip: "10.1.1.35"
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
