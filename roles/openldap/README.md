# OpenLDAP Role

Deploys OpenLDAP directory service with phpLDAPadmin.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `openldap_enabled` | `true` | Enable/disable this role |
| `openldap_image_tag` | `1.5.0` | OpenLDAP Docker image tag |
| `openldap_port_ldap` | `389` | LDAP port |
| `openldap_port_ldaps` | `636` | LDAPS port |
| `openldap_port_admin` | `8089` | phpLDAPadmin port |
| `openldap_domain` | `example.local` | LDAP domain |
| `openldap_organisation` | `Example Org` | Organization name |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `openldap_admin_password` | LDAP admin password |
| `openldap_config_password` | Config admin password |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: openldap
      vars:
        openldap_domain: "example.com"
        openldap_organisation: "My Company"
```

## Features

- User/group directory
- phpLDAPadmin web UI
- TLS support
- Traefik integration
