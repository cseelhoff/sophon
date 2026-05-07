# Kopia Role

Deploys [Kopia](https://kopia.io) as Sophon's backup transport + retention
layer. Service-specific consistent dumps (e.g. `gitea dump`) write to
`/var/mnt/nfs/backups/<service>/` on the InfraVM; Kopia snapshots that tree
into an encrypted, deduplicated filesystem repository on the NFS share and
enforces retention policy.

## What this role deploys

- `kopia` – long-running container running `kopia server` (web UI + REST API +
  in-process scheduler).
- `kopia-init` – one-shot sidecar that creates/connects the repository,
  applies the global retention policy, and registers the snapshot source.

Web UI: `https://kopia.<domain>/`

## Role variables

See [defaults/main.yml](defaults/main.yml). Common knobs:

| Variable | Default | Purpose |
|---|---|---|
| `kopia_enabled` | `true` | Toggle role on/off |
| `kopia_admin_user` / `kopia_admin_password` | `admin` / `portainer_admin_password` | Web UI login |
| `kopia_repository_password` | `portainer_admin_password` | Repo encryption passphrase — **back this up!** |
| `kopia_repo_path` | `/var/mnt/nfs/kopia/repo` | Where snapshots are stored (NFS) |
| `kopia_backups_source` | `/var/mnt/nfs/backups` | Tree that kopia snapshots |
| `kopia_snapshot_cron` | `0 */6 * * *` | Schedule passed to `kopia policy set` |
| `kopia_keep_*` | see defaults | Retention policy |

## Adding a service to the backup tree

1. Add a backup sidecar to the service's compose stack that writes consistent
   dumps to `/var/mnt/nfs/backups/<service>/`. Example pattern (Gitea):
   ```yaml
   gitea-backup:
     image: gitea/gitea:1.22
     volumes:
       - gitea_data:/data
       - /var/mnt/nfs/backups/gitea:/backups:z
     command: |
       loop: gitea dump -c /data/gitea/conf/app.ini --type tar.gz \
         --file /backups/gitea-dump-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
   ```
2. Kopia's scheduled policy on `/source` (= `/var/mnt/nfs/backups`) picks
   up the new files automatically — no kopia-side changes required.

## Restore

From any container with the kopia binary and repo password:

```bash
kopia repository connect filesystem --path=/repository
kopia snapshot list /source
kopia snapshot restore <snapshot-id> /restore-target
```

Or via the web UI under **Snapshots → Restore Files**.

## Dependencies

- `portainer_stack` (deploy mechanism)
- `traefik` (TLS reverse proxy)
- NFS share mounted on the InfraVM at `/var/mnt/nfs`
