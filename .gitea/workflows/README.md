# Gitea Actions Workflows

This directory contains Gitea Actions workflows for CI/CD automation.

## Workflows

### ansible-lint.yml
Runs on pushes to `main`/`develop` branches when Ansible files change:
- **yamllint**: Validates YAML syntax
- **ansible-lint**: Checks Ansible best practices
- **syntax-check**: Validates playbook syntax

### build-images.yml
Builds and exports container images for air-gapped deployment:
- Triggered on changes to role defaults (image version updates)
- Can be manually triggered with specific images
- Exports images as artifacts for transfer to HTTP file server

## Usage

### Manual Image Build
Trigger the build-images workflow manually:
1. Go to Actions → Build Container Images
2. Click "Run workflow"
3. Specify images: `coredns,traefik` or `all`

### Air-Gapped Deployment Flow
1. Workflow builds and exports container images as `.tar` files
2. Download artifacts from Gitea
3. Upload to HTTP file server
4. Ansible playbooks pull from HTTP file server

## Requirements

For self-hosted Gitea Actions runners:
- Docker installed
- Python 3.11+
- Sufficient disk space for container images
