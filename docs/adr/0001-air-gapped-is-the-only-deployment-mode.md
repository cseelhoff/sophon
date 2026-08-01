# Air-gapped is the only deployment mode

Deploy fetches nothing. Everything `site.yml` needs must already exist in
`artifacts/`. All artifact acquisition — pulling container images, cloning
sources, fetching the Fedora CoreOS and Alpine images — is confined to
`prestage.yml`, which is run separately while connected. There is no online
variant of Deploy that skips this and fetches on demand.

The most common environment does have full internet access. Prestage is still
mandatory there: it costs a connected operator one extra command, and it is the
only thing that keeps the air-gapped path working without a second, untested
code path. Runtime service egress is a separate question — see ADR-0006.

## Considered options

- **Dual-mode.** Keep `proxmox_airgapped` as a real branch so `site.yml` works
  online or offline. Rejected: every role would carry an untested twin path.
  The repo already demonstrates the failure mode — `proxmox_airgapped` was
  declared and branched on nowhere, `nfs_reachable` was documented and never
  implemented, and the air-gap workflow referenced an `nfs-upload.yml` that did
  not exist.
- **CI-produced bundle.** Drop `prestage.yml` and have CI publish a versioned
  artifact tarball. Rejected for now as premature; `prestage.yml` is the
  air-gap boundary and keeping it operator-run keeps it debuggable.

## Consequences

- `prestage.yml` must be run even for a lab that happens to have internet.
- A missing artifact is a hard failure at Deploy, never a silent fallback to a
  network fetch. `proxmox_airgapped` is not needed and should be removed.
- Online-only fetch paths in Deploy are now defects, not features. This
  includes the `apt-get install arp-scan` on the Proxmox node and the
  Fedora CoreOS `curl` fallback chain.
