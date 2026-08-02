---
status: amended
---

# Artifacts reach the site by fuse-mounting the NFS export from the Controller

**Amended.** NFS remains the transport for artifacts that must exist before
Portainer does — which in practice is Portainer's own image tar and nothing
else. Every other container image now reaches the endpoint through Portainer's
Docker-API proxy (`POST /api/endpoints/<id>/docker/images/load`). See
"Amendment: two transports, split by bootstrap order" below.

The Controller mounts the NFS VM's export with `fuse-nfs` and copies every
prestaged artifact into it. InfraVM then loads all container image tars from
`/var/mnt/nfs/containers/` at first boot, before Portainer or any stack exists.

## Why this transport

`fuse-nfs` runs unprivileged, so the Controller needs no root and no kernel NFS
client, and it is already vendored in `flake.nix`. `roles/nfs/tasks/main.yml`
already performs the mount and copies the Portainer tar through it — this
decision generalises what is there rather than introducing anything new.

Going via NFS also dissolves an ordering problem. Portainer's own image cannot
arrive through the Portainer API, so any API-based transport needs a second
mechanism just to bootstrap itself. Staging everything on NFS before InfraVM
first boots means there is only one mechanism.

## Considered options

- **SFTP to the NFS VM.** Workable — the Alpine image ships `openssh` and the
  role already provisions a keyed user for Kopia — but it is a second transport
  where NFS already reaches the same filesystem.
- **Portainer-proxied Docker `POST /images/load`.** Cannot bootstrap Portainer.
- **A local OCI registry on InfraVM.** Requires rewriting every compose file to
  reference an internal registry, and the registry itself needs bootstrapping.
- **Base64 through the QGA exec channel.** Hundreds of megabytes through a
  WebSocket exec channel.

## Consequences

- A failed mount is fatal. Today it warns and continues, which under ADR-0001
  means silently producing a deployment that only works if the site has
  internet.
- The export is `all_squash` to anonuid 1000 / anongid 100, which matches the
  rootless Podman user on InfraVM, so copied files are readable there.
- `sophon-init.service` must load every tar, not just Portainer's. The loop
  already exists in `roles/infravm/templates/infravm.bu.j2`, commented out.
- Compose files need `pull_policy: never`. Otherwise Portainer pulls from
  Docker Hub, the loaded images go unused, and a broken transport stays
  invisible until the site is genuinely disconnected.

## Amendment: two transports, split by bootstrap order

The "one mechanism" argument above traded a real defect for a stylistic win.
Loading images at first boot means an image only ever arrives once. Bump a tag,
rebuild the CoreDNS image, or add a service, and the tar lands on NFS but never
reaches Podman, because `sophon-init.service` already ran. Recovering means
rebuilding the VM.

`roles/coredns` had already worked around this with a bespoke `uri` task
POSTing its tar to Portainer's Docker-API proxy. That mechanism runs on every
play, so images track the repo. The rejection of it above — "cannot bootstrap
Portainer" — is true but does not disqualify it for the other seven images.

So the split is:

- **NFS + `sophon-init.service`:** Portainer's tar only. It cannot arrive any
  other way.
- **Portainer `/docker/images/load`:** everything else, on every run. The
  generalised implementation lives in `roles/portainer_stack`, which takes a
  `portainer_stack_image_tars` list, `stat`s each path, and fails if one is
  missing before it touches the stack.

This costs a second transport and buys idempotent image delivery. The failure
mode also improves: a missing tar now fails on the Controller, naming the file,
instead of surfacing as a stack that will not start.

`prestage_container_images` in `prestage.yml` and the per-role
`*_image_tars` lists must be kept in agreement by hand. Nothing enforces it.
