# Guacamole Role

Deploys Apache Guacamole remote desktop gateway.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- PostgreSQL (bundled)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `guacamole_enabled` | `true` | Enable/disable this role |
| `guacamole_image_tag` | `1.5.4` | Guacamole Docker image tag |
| `guacamole_port` | `8090` | Web UI port |
| `guacamole_data_dir` | `/opt/guacamole` | Data directory |
| `guacamole_domain` | `example.local` | Domain |
| `guacamole_oidc_enabled` | `false` | Enable Keycloak SSO |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `guacamole_db_password` | PostgreSQL password |

## Dependencies

- `portainer_stack` role
- `keycloak` role (optional, for SSO)

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: guacamole
      vars:
        guacamole_domain: "remote.example.com"
        guacamole_oidc_enabled: true
```

## Features

- RDP/VNC/SSH in browser
- Keycloak OIDC SSO
- Session recording
- Auto-generates PostgreSQL init script
- Traefik integration
