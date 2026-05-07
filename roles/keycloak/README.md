# Keycloak Role

Deploys Keycloak Identity and Access Management with SSO.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- PostgreSQL (bundled)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `keycloak_enabled` | `true` | Enable/disable this role |
| `keycloak_image_tag` | `23.0` | Keycloak Docker image tag |
| `keycloak_port` | `8180` | Web UI port |
| `keycloak_data_dir` | `/opt/keycloak` | Data directory |
| `keycloak_domain` | `example.local` | Domain |
| `keycloak_realm` | `sophon` | Realm name |
| `keycloak_import_realm` | `true` | Auto-import realm config |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `keycloak_admin_password` | Admin password |
| `keycloak_db_password` | PostgreSQL password |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: keycloak
      vars:
        keycloak_domain: "auth.example.com"
        keycloak_import_realm: true
```

## Features

- OIDC/SAML identity provider
- Auto-provisions SSO clients for:
  - Gitea
  - Portainer
- LDAP federation support
- Realm export template
