# Laptop prestaging

`bootstrap.yml` and `prestage.yml` include this role with `tasks_from: bootstrap`.
It stages service image archives, a full upstream Fedora CoreOS live ISO,
vendored Ansible collections, the Nix Python/Ansible runner and a static BusyBox
helper, then creates the Sophon deployment ISO. It does not contact Proxmox.

The transferable guest media are:

- `artifacts/coreos/fedora-coreos-live.iso`
- `artifacts/bootstrap/sophon-deployment.iso`

The live ISO is unchanged upstream media, verified against cached official
stable-stream metadata. No minimal/network-dependent ISO, qcow2 conversion,
Alpine build, or old Controller/NFS role is part of this path.

The deployment ISO has label `SOPHON_DEPLOY`, Rock Ridge/Joliet names and ISO
level 3 for archives larger than 4 GiB. It contains `/install.sh`,
`/tools/busybox`, `/runner.tar`, `/project`, and `/SHA256SUMS`. Only allowlisted
source trees/files, explicitly expected container archives and vendored
collections enter the project. Plain bootstrap metadata identifies the
Portainer image and archive; it is not sourced as shell code.

The bundled `site.json` is empty: no laptop site settings or credentials are
transferred. The live installer creates installed network defaults locally.
Private keys, tokens, passwords, runtime state and hidden source files are
excluded. Source symlinks and external collection links are rejected; upstream
collection links to regular files within the collection tree are copied as
regular files. All required inputs are checked before an existing ISO is replaced.

```sh
# Rebuild only the deployment ISO, using the cached archive manifest:
python3 bundle.py --bootstrap-tools artifacts/bootstrap/busybox
```

With a complete valid cache, prestaging does not invoke Nix, Galaxy, registry
pulls, source updates or CoreOS HTTP requests. It still verifies the cached
CoreOS checksum and rebuilds the bundle locally to capture current source.
`-e prestage_force=true` refreshes cached inputs while connected. Collection
installation is laptop-only and forces a local copy even when the laptop already
has the collection installed elsewhere; the runner uses vendored collections.
Changes to `requirements.yml` invalidate the collection cache; changes to
`flake.nix` or `flake.lock` invalidate the runner and static helper cache.

`tasks/main.yml` retains a legacy standalone NFS image workflow; invoking it
directly is not the supported two-ISO entry point. See [the runbook](../../prestage.md).
