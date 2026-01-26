# n8n Role

Deploys n8n workflow automation platform.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- PostgreSQL (bundled)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `n8n_enabled` | `true` | Enable/disable this role |
| `n8n_image_tag` | `latest` | n8n Docker image tag |
| `n8n_port` | `5678` | Web UI port |
| `n8n_data_dir` | `/opt/n8n` | Data directory |
| `n8n_domain` | `example.local` | Domain |
| `n8n_oidc_enabled` | `false` | Enable Keycloak SSO |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `n8n_encryption_key` | Workflow encryption key |
| `n8n_db_password` | PostgreSQL password |
| `n8n_oidc_client_secret` | OIDC client secret |

## Dependencies

- `portainer_stack` role
- `keycloak` role (optional, for SSO)

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: n8n
      vars:
        n8n_domain: "automation.example.com"
        n8n_oidc_enabled: true
```

## Features

- Visual workflow builder
- 400+ integrations
- Keycloak OIDC SSO
- Webhook support
- Traefik integration
