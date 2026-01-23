# Homepage Role

Deploys Homepage dashboard for service discovery.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `homepage_enabled` | `true` | Enable/disable this role |
| `homepage_image_tag` | `latest` | Homepage Docker image tag |
| `homepage_port` | `3000` | Web UI port |
| `homepage_data_dir` | `/opt/homepage` | Config directory |
| `homepage_domain` | `example.local` | Domain for Traefik |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: homepage
```

## Features

- Auto-discovers services via Docker labels
- Widget support (system stats, weather, etc.)
- Customizable layout
- Traefik integration
