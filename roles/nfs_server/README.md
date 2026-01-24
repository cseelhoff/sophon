# NFS Server Role

Deploys a lightweight Alpine Linux NFS server on Proxmox with automatic disk provisioning and Proxmox storage integration.

## Purpose

This role is the first step in the Sophon deployment workflow. It creates a shared NFS storage that:
- Serves as the backend for Nexus proxy repositories (docker-proxy, yum-proxy)
- Provides shared storage accessible by Proxmox and all VMs
- Enables air-gapped deployments by pre-staging container images and RPMs

## Features

- **Lightweight**: Alpine Linux (~50MB RAM at idle)
- **RAM-based**: Runs from memory, minimal disk I/O for OS
- **Auto-provisioning**: Unformatted data disk is automatically partitioned and formatted
- **Nexus-ready**: Pre-creates export paths for Nexus proxy blob stores
- **Proxmox integration**: Automatically adds NFS storage to Proxmox

## Requirements

- Proxmox VE with API access
- Static IP address for NFS server
- Network reachable from Proxmox host

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `nfs_server_enabled` | `true` | Enable/disable this role |
| `nfs_server_ip` | **required** | Static IP address (must be specified) |
| `nfs_server_vm_name` | `sophon-nfs` | VM name in Proxmox |
| `nfs_server_cores` | `1` | CPU cores |
| `nfs_server_memory` | `512` | RAM in MB |
| `nfs_server_disk_size` | `1G` | OS disk size (Alpine is tiny) |
| `nfs_server_data_disk_size` | `100G` | Data disk for NFS exports (thin provisioned) |
| `nfs_server_proxmox_storage_name` | `sophon-nfs` | Name in Proxmox storage list |
| `nfs_server_export_root` | `/export` | Root export path |
| `nfs_server_export_paths` | See defaults | List of export directories to create |

## Disk Layout

The NFS server VM has two disks:

1. **virtio0** (OS disk, ~1GB): Alpine Linux root filesystem
2. **virtio1** (Data disk, 100GB thin): NFS exports mounted at `/export`

On first boot, if virtio1 (`/dev/vdb`) is unformatted:
1. Creates GPT partition table
2. Creates single XFS partition
3. Mounts at `/export`
4. Creates Nexus directory structure

## NFS Exports

Default exports (configurable via `nfs_server_export_paths`):

```
/export
├── nexus/
│   ├── docker-proxy/    # Container image tarballs
│   └── yum-proxy/       # RPM packages
└── sophon/
    ├── images/          # VM images (qcow2)
    └── bin/             # Binaries (butane)
```

All exports use options: `rw,sync,no_subtree_check,no_root_squash`

## Dependencies

- `proxmox` role (for API authentication)

## Example Playbook

```yaml
- hosts: localhost
  roles:
    - role: nfs_server
      vars:
        nfs_server_ip: "10.0.1.35"
```

## Workflow Integration

This role integrates with the Sophon deployment workflow:

1. **Detection**: `site.yml` checks if `sophon-nfs` storage exists in Proxmox
2. **Deployment**: If not found and `nfs_server_ip` is set, deploys NFS server
3. **Storage**: Adds NFS to Proxmox as `sophon-nfs` storage
4. **Content**: `nfs_content` role populates exports with images/RPMs
5. **CoreOS**: Mounts NFS exports to load content at first boot

## Air-Gapped Mode

For air-gapped deployments:

```bash
# Phase 1: Prestage includes Alpine image
ansible-playbook prestage.yml

# Phase 2: Upload includes Alpine image upload
ansible-playbook nfs-upload.yml

# Phase 3: Deploy creates NFS server from uploaded image
ansible-playbook site.yml -e airgapped_mode=true -e nfs_server_ip=10.0.1.35
```

## Troubleshooting

### VM Not Booting
```bash
# Check VM status via Proxmox API or console
# Alpine cloud-init logs: /var/log/cloud-init.log
```

### NFS Exports Not Working
```bash
# SSH to NFS server (if enabled) or use Proxmox console
showmount -e localhost
exportfs -v
```

### Disk Not Formatted
```bash
# Check cloud-init output
cat /var/log/cloud-init-output.log
# Manual provisioning
/usr/local/bin/provision-nfs-disk.sh
```
