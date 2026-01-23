# Proxmox Role

Provides Proxmox API authentication and helper tasks for interacting with Proxmox VE.

## Features

- **Authentication**: Obtains API ticket and CSRF token, stored as `proxmox_ticket` and `proxmox_csrf` facts
- **QGA Exec**: Reusable task to execute commands via QEMU Guest Agent

## Usage

### Basic Authentication

Include as a dependency in your role's `meta/main.yml`:

```yaml
dependencies:
  - role: proxmox
```

Or include explicitly in tasks:

```yaml
- include_role:
    name: proxmox
```

After running, `proxmox_ticket` and `proxmox_csrf` facts are available for API calls.

### QGA Command Execution

Execute commands inside a VM via the QEMU Guest Agent:

```yaml
- include_role:
    name: proxmox
    tasks_from: qga_exec
  vars:
    qga_vmid: "107"
    qga_command: "echo hello && cat /etc/hostname"
    qga_timeout: 180  # optional, seconds

- name: Show result
  debug:
    msg: |
      stdout: {{ qga_result.stdout }}
      stderr: {{ qga_result.stderr }}
      exitcode: {{ qga_result.exitcode }}
      success: {{ qga_result.success }}
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `proxmox_host` | `pve.example.com` | Proxmox API hostname |
| `proxmox_port` | `8006` | Proxmox API port |
| `proxmox_username` | `root` | Username (without realm) |
| `proxmox_user` | `root@pam` | Full username with realm |
| `proxmox_password` | - | API password (required) |
| `proxmox_node` | `proxmox` | Proxmox node name |
| `proxmox_validate_certs` | `false` | Validate SSL certificates |

## Facts Set

After running `main.yml`:
- `proxmox_ticket` - Authentication cookie
- `proxmox_csrf` - CSRF prevention token

After running `qga_exec.yml`:
- `qga_result.stdout` - Command stdout
- `qga_result.stderr` - Command stderr
- `qga_result.exitcode` - Exit code
- `qga_result.success` - Boolean success status
