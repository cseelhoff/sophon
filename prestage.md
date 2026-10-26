# Sophon prestaging runbook

Prestage prepares the complete offline installation media without touching live
infrastructure. `bootstrap.yml` runs it automatically; run it separately to
prepare a disconnected bootstrap laptop.

```sh
nix develop
ansible-playbook prestage.yml
```

No domain, guest network, console password, SSH key, or service credential is
needed to prepare reusable media.

## Outputs

- `artifacts/coreos/fedora-coreos-live.iso`: unchanged full upstream live ISO,
  verified using the official stable stream's SHA256.
- `artifacts/bootstrap/sophon-deployment.iso`: project snapshot, installer,
  static BusyBox helper, offline Python/Ansible runner, vendored collections,
  and all required container archives, with a checksum manifest.

The deployment ISO uses label `SOPHON_DEPLOY`, Rock Ridge/Joliet names and ISO
level 3 to support archives larger than 4 GiB. Only allowlisted source and
explicitly expected artifacts are bundled. Private keys, tokens, passwords,
runtime logs and laptop site settings are excluded.

The cache also retains upstream metadata, image archives, collections, runner
and helper. Keep `artifacts/` with the matching project when a disconnected
laptop will run Phase 1: the two ISO files alone are sufficient for the guest,
but are not a replacement for the laptop's prestage cache. Prestage verifies
the cached CoreOS ISO and rebuilds the bundle locally to capture source changes.
With a complete valid cache it does not fetch metadata, pull containers, run
Galaxy, build the runner/helper, or update CoreDNS sources.

Use `ansible-playbook prestage.yml -e prestage_force=true` while connected to
refresh cached inputs. This may require substantial disk space and downloads.
Missing or invalid inputs fail explicitly rather than silently using registries
from the installed guest.

## Disconnected deployment

Copy the repository and complete `artifacts/` cache to the bootstrap laptop.
Start the bootstrap dashboard:

```sh
nix develop
python3 deployment.py --phase bootstrap --serve --port 41327
```

Supply Proxmox management credentials, node/storage, bridge/VLAN and VM size.
Phase 1 creates one blank InfraVM disk, uploads both real ISOs with
content-addressed names, attaches them and starts the VM.

Open its live console:

```sh
sudo mkdir -p /mnt/sophon
sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

Enter guest networking and the `core` console password, confirm a fresh target
disk, and let the installer reboot. Log in as `core` and run
`sophon-controller`. It prompts for the domain and service credentials; the
optional dashboard is `sophon-controller --dashboard --port 41327`.
Both ISOs can be detached after persistent installation.

## Rebuild only the deployment ISO

```sh
python3 bundle.py --bootstrap-tools artifacts/bootstrap/busybox
```

The existing `artifacts/containers/manifest.json` declares the expected archives;
`--container` can instead be repeated to declare every archive explicitly.
Missing scripts, helper, runner, collections or archives abort without replacing
a previous ISO. See [the role contract](roles/prestage/README.md).

The normal flow builds no Alpine/NFS/Controller disk, imports no qcow2 image and
uses no NFS transport. Legacy builders remain in the repository but are not
invoked. Backup infrastructure and Kopia are deferred; see
[ADR-0013](docs/adr/0013-two-iso-infravm-installation.md).
