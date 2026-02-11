# CoreDNS Build Role

Builds CoreDNS with the dockerdiscovery plugin and exports it as a container image tar file.

## Requirements

The following tools must be available (provided by the Nix flake):
- `go` - Go compiler
- `gnumake` - GNU Make
- `git` - Git
- `buildah` - Container image builder

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `coredns_version` | `v1.12.0` | CoreDNS version to build |
| `coredns_dockerdiscovery_plugin` | `docker:github.com/kevinjqiu/coredns-dockerdiscovery` | Plugin line to add |
| `coredns_build_dir` | `/tmp/coredns-build` | Temporary build directory |
| `coredns_image_name` | `coredns-dockerdiscovery` | Output image name |
| `coredns_image_tag` | `latest` | Output image tag |
| `coredns_image_output` | `{{ playbook_dir }}/nfs/coredns-dockerdiscovery.tar` | Output tar file path |
| `coredns_force_rebuild` | `false` | Force rebuild even if tar exists |

## Usage

```yaml
- hosts: localhost
  connection: local
  roles:
    - coredns_build
```

Or with custom variables:

```yaml
- hosts: localhost
  connection: local
  roles:
    - role: coredns_build
      vars:
        coredns_version: "v1.12.0"
        coredns_force_rebuild: true
```

## Output

The role produces an OCI-compatible container image tar file that can be loaded with:

```bash
# Docker
docker load < nfs/coredns-dockerdiscovery.tar

# Podman
podman load < nfs/coredns-dockerdiscovery.tar

# Skopeo (to copy to registry)
skopeo copy oci-archive:nfs/coredns-dockerdiscovery.tar docker://registry/coredns-dockerdiscovery:latest
```
