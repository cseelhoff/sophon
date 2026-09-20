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

## Credential inputs

| Variable | Environment input |
|----------|-------------------|
| `keycloak_admin_password` | `SOPHON_KEYCLOAK_ADMIN_PASSWORD` |
| `keycloak_db_password` | `SOPHON_KEYCLOAK_DB_PASSWORD` |
| `keycloak_portainer_client_secret` | `SOPHON_KEYCLOAK_PORTAINER_CLIENT_SECRET` |

All are required by `site.yml`; none is generated or persisted on the controller.
Reuse existing values on reruns. See [credential migration](../../docs/preconditions.md#existing-deployments).

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
