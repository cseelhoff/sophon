# NFS Content Role

Prepares NFS shares with RPM packages and container images for CoreOS first-boot.

## Requirements

- SSH access to the NFS server
- Tools on the Ansible controller: `skopeo`, `dnf`, `rsync`
- The NFS server should be in your inventory

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `nfs_content_enabled` | `true` | Enable/disable this role |
| `nfs_content_server` | `{{ nfs_server_ip }}` | NFS server hostname/IP |
| `nfs_content_rpm_path` | `{{ nfs_rpm_packages_path }}` | Path on NFS for RPMs |
| `nfs_content_docker_path` | `{{ nfs_docker_images_path }}` | Path on NFS for container images |
| `nfs_content_fedora_version` | `43` | Fedora version for RPM downloads |
| `nfs_content_arch` | `x86_64` | Architecture for RPM downloads |
| `nfs_content_rpm_packages` | `[qemu-guest-agent]` | RPM packages to download |
| `nfs_content_container_images` | See defaults | Container images to download |
| `nfs_content_local_staging_dir` | `/tmp/sophon_nfs_content` | Local staging directory |

## What Gets Downloaded

### RPM Packages
- `qemu-guest-agent` (with all dependencies resolved by dnf)

### Container Images
- `portainer/portainer-ce:2.27.1`
- `coredns/coredns:1.12.0`
- `traefik:v3.2`
- `osixia/openldap:1.5.0`
- `osixia/phpldapadmin:0.9.0`
- `sonatype/nexus3:3.72.0`

## Dependencies

The role requires these tools on the Ansible controller. If using the project's flake.nix devshell, they are automatically available:

```bash
nix develop  # From project root
```

Or install manually:
- `skopeo` - For downloading container images
- `dnf` - For downloading RPM packages with dependency resolution
- `rsync` - For syncing content to NFS server

## Example Playbook

```yaml
- hosts: localhost
  roles:
    - role: nfs_content
      vars:
        nfs_server_ip: "10.1.1.35"
        nfs_rpm_packages_path: "/exports/nexus/yum-proxy"
        nfs_docker_images_path: "/exports/nexus/docker-proxy"
```

## Standalone Playbook

Create `nfs-content.yml`:

```yaml
---
- name: Prepare NFS content for CoreOS
  hosts: localhost
  gather_facts: true
  roles:
    - nfs_content
```

Run with:
```bash
ansible-playbook nfs-content.yml -i inventories/production/inventory.yml
```

## Integration with CoreOS Role

This role should be run **before** the coreos role. The coreos role expects the NFS shares to be populated with:

- RPMs at `{{ nfs_rpm_packages_path }}/`
- Container images at `{{ nfs_docker_images_path }}/`

## Notes

- Downloads are idempotent - existing files are not re-downloaded
- Container images are saved in `docker-archive` format compatible with `podman load`
- The NFS paths are designed to also serve as Nexus proxy repository blob stores
