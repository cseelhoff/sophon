# Air-gapped is a supported deployment mode

Deploy fetches nothing. Everything `site.yml` needs must already exist in
`artifacts/`. All artifact acquisition — pulling container images, cloning
sources, fetching the Fedora CoreOS and Alpine images — is confined to
`prestage.yml`, which is run separately while connected. There is no online
variant of Deploy that skips this and fetches on demand.

The most common environment does have full internet access. Prestage still runs
there: it is the only thing that keeps the air-gapped path working without a
second, untested code path. Runtime service egress is a separate question — see
ADR-0006.

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

- Prestage runs for every deployment, including a lab that happens to have
  internet.
- A missing artifact is a hard failure at Deploy, never a silent fallback to a
  network fetch. `proxmox_airgapped` is not needed and should be removed.
- Online-only fetch paths in Deploy are now defects, not features. This
  includes the `apt-get install arp-scan` on the Proxmox node and the
  Fedora CoreOS `curl` fallback chain.

## Amendment: Prestage is invoked by Deploy, not by the operator

The original decision made Prestage a separate command the operator had to
remember. That was the wrong place to pay the cost. The acquisition boundary is
worth defending; the second command is not.

Prestage is now a role (`roles/prestage/`) that `site.yml` includes before any
provisioning. `prestage.yml` is a thin wrapper around the same role.

The role gates itself on artifact presence: it stats every tar it is
responsible for plus the NFS disk image, and if all of them exist it does
nothing — including the `git` clone, which is the only task that unconditionally
needs egress. That single property is what makes auto-invocation safe:

- **Connected site.** One command. Deploy stages what is missing, then
  provisions.
- **Disconnected site.** Stage on a connected machine, carry `artifacts/` in.
  At the site every artifact exists, so the role is a no-op and Deploy still
  touches nothing.
- **Disconnected site, artifact genuinely missing.** The role tries to fetch and
  fails loudly, which is the correct outcome — that deployment could not have
  succeeded anyway.

No `airgapped` flag is reintroduced. There is still one code path; presence of
the artifacts is the only input, and it is a fact about the filesystem rather
than an operator assertion that can be wrong.
