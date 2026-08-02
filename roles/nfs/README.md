# NFS Server Role

Deploys a diskless Alpine Linux NFS server on Proxmox with automatic disk provisioning and Proxmox storage integration.

## Purpose

This role is the first step in the Sophon deployment workflow. It creates a shared NFS storage that:
- Serves as the backend for Nexus proxy repositories (docker-proxy, yum-proxy)
- Provides shared storage accessible by Proxmox and all VMs
- Enables air-gapped deployments by pre-staging container images and RPMs

## Features

- **Diskless**: OS runs from tiny cloud image (ephemeral), only data disk persists
- **RAM-based**: Alpine runs from memory, cloud-init reconfigures on every boot
- **Auto-provisioning**: Unformatted data disk is automatically partitioned and formatted
- **Nexus-ready**: Pre-creates export paths for Nexus proxy blob stores
- **Proxmox integration**: Automatically adds NFS storage to Proxmox
- **Safe formatting**: Only formats data disk if NO partition table exists

## Requirements

- Proxmox VE with API access
- Static IP address for NFS server
- Network reachable from Proxmox host

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `nfs_enabled` | `true` | Enable/disable this role |
| `nfs_ip` | **required** | Static IP address (must be specified) |
| `nfs_vm_name` | `sophon-nfs` | VM name in Proxmox |
| `nfs_cores` | `1` | CPU cores |
| `nfs_memory` | `512` | RAM in MB |
| `nfs_boot_disk_size` | `1G` | Ephemeral OS disk (stateless) |
| `nfs_data_disk_size` | `100G` | Data disk for NFS exports (thin provisioned, persistent) |
| `nfs_storage_name` | `sophon-nfs` | Name in Proxmox storage list |
| `nfs_export_root` | `/export` | Root export path |
| `nfs_export_paths` | See defaults | List of export directories to create |

## Disk Layout

The NFS server uses a diskless architecture:

1. **ide0** (Boot disk, 1GB): Ephemeral Alpine cloud image (stateless, can be recreated)
2. **virtio0** (Data disk, 100GB thin): NFS exports mounted at `/export` (persistent)

On every boot, cloud-init:
1. Installs nfs-utils, xfsprogs, parted
2. Runs disk provisioning script
3. Mounts data disk and exports NFS shares

On first boot, if virtio0 (`/dev/vda`) is unformatted:
1. Creates GPT partition table
2. Creates single XFS partition
3. Mounts at `/export`
4. Creates Nexus directory structure

**Safety**: If disk already has a partition table, it is NEVER reformatted.

## NFS Exports

Default exports (configurable via `nfs_export_paths`):

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
        nfs_ip: "10.0.1.35"
```

## Workflow Integration

This role integrates with the Sophon deployment workflow:

1. **Detection**: `site.yml` checks if `sophon-nfs` storage exists in Proxmox
2. **Deployment**: If not found and `nfs_ip` is set, deploys NFS server
3. **Storage**: Adds NFS to Proxmox as `sophon-nfs` storage
4. **Content**: `nfs_content` role populates exports with images/RPMs
5. **CoreOS**: Mounts NFS exports to load content at first boot

## Air-Gapped Mode

`site.yml` stages the Alpine image itself when it is missing, so a connected
Controller needs no extra step. For a disconnected site, stage on a connected
machine and copy `artifacts/` across:

```bash
# Connected machine
ansible-playbook prestage.yml

# At the site, after copying artifacts/ into the repo
ansible-playbook site.yml -e nfs_ip=10.0.1.35
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
