# Artifacts reach the site by fuse-mounting the NFS export from the Controller

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
