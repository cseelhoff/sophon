# Nexus Role

Deploys Sonatype Nexus Repository Manager with Docker registry.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `nexus_enabled` | `true` | Enable/disable this role |
| `nexus_image_tag` | `3.64.0` | Nexus Docker image tag |
| `nexus_port` | `8081` | Web UI port |
| `nexus_docker_port` | `5000` | Docker registry port |
| `nexus_data_dir` | `/opt/nexus` | Data directory |
| `nexus_provision_repos` | `true` | Auto-create Docker repos |

## Secrets (via Vault)

| Variable | Description |
|----------|-------------|
| `nexus_admin_password` | Admin password |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: nexus
      vars:
        nexus_provision_repos: true
```

## Features

- Maven/npm/Docker repository hosting
- Auto-provisions Docker repositories:
  - `docker-hosted` - local images
  - `docker-proxy` - DockerHub cache
  - `ghcr-proxy` - GitHub Container Registry cache
  - `quay-proxy` - Quay.io cache
  - `docker-group` - unified registry
- REST API automation
