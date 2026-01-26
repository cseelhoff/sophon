# Kopia Role

Deploys Kopia for filesystem-level backups with deduplication.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `kopia_enabled` | `true` | Enable/disable this role |
| `kopia_image_tag` | `latest` | Kopia Docker image tag |
| `kopia_port` | `51515` | Web UI port |
| `kopia_data_dir` | `/opt/kopia` | Config directory |
| `kopia_repository_path` | `/mnt/nas/kopia` | Repository location |
| `kopia_retention_daily` | `7` | Daily snapshots to keep |
| `kopia_retention_weekly` | `4` | Weekly snapshots to keep |
| `kopia_retention_monthly` | `6` | Monthly snapshots to keep |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `kopia_repository_password` | Repository encryption password |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: kopia
      vars:
        kopia_repository_path: "s3://bucket/kopia"
```

## Features

- Block-level deduplication
- Client-side encryption
- Compression
- Multiple backends (filesystem, S3, B2, Azure, GCS, rclone)
- Web UI for browsing/restore
- Scheduled snapshots
