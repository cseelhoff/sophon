# Backup Role

Deploys docker-volume-backup for automated container volume backups.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- NAS/storage target accessible

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `backup_enabled` | `true` | Enable/disable this role |
| `backup_image_tag` | `v2` | docker-volume-backup image tag |
| `backup_schedule` | `0 2 * * *` | Cron schedule (default: 2 AM daily) |
| `backup_data_dir` | `/opt/backup` | Local backup staging |
| `backup_retention_days` | `7` | Days to keep local backups |
| `backup_target_dir` | `/mnt/nas/backups` | Remote backup destination |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: backup
      vars:
        backup_schedule: "0 3 * * *"
        backup_target_dir: "/mnt/nas/sophon-backups"
```

## Features

- Automated volume snapshots
- Compression (gzip)
- Configurable retention
- NAS/S3/rclone targets
- Pre/post backup hooks
