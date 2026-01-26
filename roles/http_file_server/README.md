# HTTP File Server Role

Serves files via HTTP for air-gapped deployments.

## Requirements

- Python 3 on Ansible controller

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `http_file_server_port` | `8000` | HTTP server port |
| `http_file_server_dir` | `/tmp/http_files` | Directory to serve |
| `file_to_serve` | - | Filename to make available |
| `file_download_url` | - | URL to download file from |

## Usage

This role is typically included by other roles (e.g., `coreos`) to transfer files:

```yaml
- name: Serve CoreOS image
  include_role:
    name: http_file_server
  vars:
    file_to_serve: "fedora-coreos.qcow2.img"
    file_download_url: "https://builds.coreos.fedoraproject.org/..."
```

## Features

- Downloads and caches files locally
- Serves via Python HTTP server
- Supports compressed files (auto-extract)
- Used for air-gapped container image transfer
