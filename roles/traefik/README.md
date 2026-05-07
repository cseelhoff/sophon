# Traefik Role

Deploys Traefik reverse proxy with automatic SSL via Let's Encrypt.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment
- Cloudflare account (for DNS challenge)

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `traefik_enabled` | `true` | Enable/disable this role |
| `traefik_image_tag` | `v3.2` | Traefik Docker image tag |
| `traefik_port_http` | `80` | HTTP entrypoint port |
| `traefik_port_https` | `443` | HTTPS entrypoint port |
| `traefik_domain` | `example.local` | Base domain |
| `traefik_acme_email` | - | Let's Encrypt email |
| `traefik_dns_provider` | `cloudflare` | ACME DNS provider |
| `traefik_log_level` | `INFO` | Log verbosity |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `traefik_cf_dns_api_token` | Cloudflare API token for DNS challenge |
| `traefik_dashboard_auth` | htpasswd auth for dashboard |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: traefik
      vars:
        traefik_domain: "example.com"
        traefik_acme_email: "admin@example.com"
```

## Features

- Automatic HTTPS via Let's Encrypt
- Cloudflare DNS challenge (works behind NAT)
- Security headers middleware
- IP whitelist middleware
- Docker/Podman provider auto-discovery
