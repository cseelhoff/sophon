# Nextcloud Role

Deploys Nextcloud file sync and collaboration platform.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- PostgreSQL (bundled)
- Redis (bundled)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `nextcloud_enabled` | `true` | Enable/disable this role |
| `nextcloud_image_tag` | `28` | Nextcloud Docker image tag |
| `nextcloud_port` | `8083` | Web UI port |
| `nextcloud_data_dir` | `/opt/nextcloud` | Data directory |
| `nextcloud_domain` | `example.local` | Domain |
| `nextcloud_oidc_enabled` | `false` | Enable Keycloak SSO |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `nextcloud_admin_password` | Admin password |
| `nextcloud_db_password` | PostgreSQL password |
| `nextcloud_oidc_client_secret` | OIDC client secret |

## Dependencies

- `portainer_stack` role
- `keycloak` role (optional, for SSO)

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: nextcloud
      vars:
        nextcloud_domain: "cloud.example.com"
        nextcloud_oidc_enabled: true
```

## Features

- File sync and share
- Office document editing
- Keycloak OIDC SSO
- Redis caching
- Traefik integration
