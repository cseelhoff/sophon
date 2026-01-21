# Portainer Role

Deploys Portainer CE for container management.

## Requirements

- CoreOS VM with Podman
- Network access to pull container image (or air-gapped via HTTP file server)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `portainer_enabled` | `true` | Enable/disable this role |
| `portainer_image_tag` | `latest` | Portainer Docker image tag |
| `portainer_port` | `9443` | HTTPS port for web UI |
| `portainer_data_dir` | `/opt/portainer` | Data directory |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `portainer_admin_password` | Initial admin password |
| `portainer_api_key` | API key for stack deployments |

## Dependencies

None

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: portainer
```

## Notes

- First login creates admin user
- API key needed for `portainer_stack` role operations
- Supports OIDC SSO via Keycloak
