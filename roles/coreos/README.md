# CoreOS Role

Provisions a Fedora CoreOS VM on Proxmox with Ignition configuration.

## Requirements

- Proxmox VE with API access
- SSH key pair for VM access
- Network connectivity between Ansible controller and Proxmox

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `coreos_enabled` | `true` | Enable/disable this role |
| `coreos_proxmox_api_host` | - | Proxmox API hostname/IP |
| `coreos_proxmox_api_user` | `root@pam` | Proxmox API user |
| `coreos_proxmox_node_name` | `pve` | Proxmox node name |
| `coreos_proxmox_storage` | `local-lvm` | Proxmox storage pool |
| `coreos_vm_name` | `sophon-coreos` | VM name in Proxmox |
| `coreos_ip` | - | Static IP for the VM |
| `coreos_cidr` | `24` | Network CIDR |
| `coreos_gateway` | - | Default gateway |
| `coreos_cores` | `2` | CPU cores |
| `coreos_memory` | `4096` | RAM in MB |
| `coreos_username` | `admin` | User created in VM |
| `coreos_ssh_private_key_path` | `~/.ssh/id_rsa` | Path to SSH private key |
| `coreos_ssh_public_key_path` | `{{ coreos_ssh_private_key_path }}.pub` | Path to SSH public key |
| `nfs_server_ip` | `10.1.1.35` | NFS server for container images and RPMs |
| `nfs_docker_images_path` | `/exports/nexus/docker-proxy` | NFS path for docker images (Nexus docker-proxy blob store) |
| `nfs_rpm_packages_path` | `/exports/nexus/yum-proxy` | NFS path for RPM packages (Nexus yum-proxy blob store) |
| `coreos_nfs_docker_mount_point` | `/mnt/nfs/docker-images` | Mount point in CoreOS for docker images NFS share |
| `coreos_nfs_rpm_mount_point` | `/mnt/nfs/rpm-packages` | Mount point in CoreOS for RPM packages NFS share |
| `coreos_container_images` | See defaults | List of container images to load from NFS |

## Dependencies

- `nfs_content` role (run first to prepare NFS shares)

## NFS Content Preparation

Before deploying CoreOS, run the nfs_content role to populate the NFS shares:

```bash
# Enter devshell (provides skopeo, dnf5)
nix develop

# Run NFS content preparation
ansible-playbook nfs-content.yml
```

This downloads RPMs and container images to the NFS server. See `roles/nfs_content/README.md` for details.

## Example Playbook

```yaml
- hosts: localhost
  roles:
    - role: coreos
      vars:
        coreos_proxmox_api_host: "192.168.1.100"
        coreos_ip: "10.0.1.50"
        coreos_gateway: "10.0.1.1"
```

## Notes

- The Proxmox API password can be set via `PROXMOX_API_PASSWORD` environment variable
- Ignition config is embedded in VM args for first-boot configuration
- SSH key is automatically generated if not present
- All external dependencies (RPMs and container images) are loaded from NFS at first boot
- The NFS paths are designed to serve as backend blob stores for Nexus proxy repositories:
  - `nfs_docker_images_path` → Nexus docker-proxy blob store
  - `nfs_rpm_packages_path` → Nexus yum-proxy blob store
- No CD-ROM or ISO is required - the VM boots with only the CoreOS disk and NFS mounts
