# Portainer Stack Role

Deploys Docker Compose stacks via Portainer API.

## Requirements

- Portainer running with API access
- Valid API key

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `portainer_url` | - | Portainer URL (e.g., `https://localhost:9443`) |
| `portainer_api_key` | - | Portainer API key |
| `stack_name` | - | Name for the stack |
| `docker_compose_content_decoded` | - | Docker Compose YAML content |
| `stack_env_vars` | `[]` | Environment variables for stack |

## Usage

This role is typically included by other roles:

```yaml
- name: Deploy my stack
  ansible.builtin.include_role:
    name: portainer_stack
  vars:
    portainer_url: "https://{{ coreos_ip }}:9443"
    stack_name: "myapp"
    docker_compose_content_decoded: "{{ lookup('template', 'docker-compose.yml.j2') }}"
    stack_env_vars:
      - name: DB_PASSWORD
        value: "{{ db_password }}"
```

## Notes

- Stacks are created or updated idempotently
- Environment variables are passed securely via API
- Stack deployment waits for Portainer endpoint to be ready
