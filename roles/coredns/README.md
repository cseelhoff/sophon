# CoreDNS Role

Deploys CoreDNS for internal DNS resolution.

## Requirements

- CoreOS VM with Podman
- Portainer for stack deployment

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `coredns_enabled` | `true` | Enable/disable this role |
| `coredns_image_tag` | `1.11.1` | CoreDNS Docker image tag |
| `coredns_port` | `53` | DNS port |
| `coredns_data_dir` | `/opt/coredns` | Config directory |
| `coredns_domain` | `example.local` | Local domain |
| `coredns_upstream_dns` | `1.1.1.1` | Upstream DNS forwarder |

## Dependencies

- `portainer_stack` role

## Example Playbook

```yaml
- hosts: coreos
  roles:
    - role: coredns
      vars:
        coredns_domain: "home.local"
        coredns_upstream_dns: "8.8.8.8"
```

## Features

- Local zone hosting
- Upstream DNS forwarding
- Lightweight (Go-based)
- Prometheus metrics endpoint
