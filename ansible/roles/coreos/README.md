# CoreOS Role

Provisions a Fedora CoreOS VM on Proxmox with Ignition configuration.

## Requirements

- Proxmox VE with API access
- SSH key pair for VM access
- Network connectivity between Ansible controller and Proxmox

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `coreos_enabled` | `true` | Enable/disable this role |
| `proxmox_api_host` | - | Proxmox API hostname/IP |
| `proxmox_api_user` | `root@pam` | Proxmox API user |
| `proxmox_node_name` | `proxmox` | Proxmox node name |
| `coreos_vm_name` | `coreos` | VM name in Proxmox |
| `coreos_ip` | - | Static IP for the VM |
| `coreos_cidr` | `24` | Network CIDR |
| `coreos_gateway` | - | Default gateway |
| `coreos_cores` | `2` | CPU cores |
| `coreos_memory` | `4096` | RAM in MB |
| `coreos_username` | `admin` | User created in VM |
| `ssh_private_key_path` | - | Path to SSH private key |

## Dependencies

- `http_file_server` role (for transferring QCOW2 image)

## Example Playbook

```yaml
- hosts: localhost
  roles:
    - role: coreos
      vars:
        proxmox_api_host: "192.168.1.100"
        coreos_ip: "10.0.1.50"
        coreos_gateway: "10.0.1.1"
```

## Notes

- The Proxmox API password can be set via `PROXMOX_API_PASSWORD` environment variable
- Ignition config is embedded in VM args for first-boot configuration
- SSH key is automatically generated if not present
