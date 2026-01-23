# Gitea Role

Deploys Gitea Git server with Actions runner support.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- PostgreSQL (bundled)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `gitea_enabled` | `true` | Enable/disable this role |
| `gitea_image_tag` | `1.21` | Gitea Docker image tag |
| `gitea_port_http` | `3001` | Web UI port |
| `gitea_port_ssh` | `222` | SSH port |
| `gitea_data_dir` | `/opt/gitea` | Data directory |
| `gitea_domain` | `example.local` | Domain |
| `gitea_oauth_enabled` | `false` | Enable Keycloak SSO |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `gitea_db_password` | PostgreSQL password |
| `gitea_secret_key` | Session secret |
| `gitea_oauth_client_secret` | OIDC client secret |

## Dependencies

- `portainer_stack` role
- `keycloak` role (optional, for SSO)

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: gitea
      vars:
        gitea_domain: "git.example.com"
        gitea_oauth_enabled: true
```

## Features

- Git hosting with web UI
- Gitea Actions (CI/CD)
- Keycloak OIDC SSO
- Built-in PostgreSQL
