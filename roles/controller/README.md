# Controller bootstrap

Fresh deployments only. Run after the Proxmox authentication and vnet discovery
roles, before InfraVM bootstrap. This role only contacts the Proxmox HTTPS API;
it never SSHs into a VM or the hypervisor and never invokes the legacy NFS role.
It returns `controller_vmid` and the compatibility fact `nfs_vmid`.

## Inputs

| Variable | Default / requirement |
| --- | --- |
| `controller_image_path` | `{{ playbook_dir }}/artifacts/controller-vm/sophon-controller-alpine.qcow2` (prestaged) |
| `controller_iso_path` | `{{ playbook_dir }}/artifacts/bootstrap/sophon-deployment.iso` (prestaged) |
| `controller_ssh_public_key` | `SOPHON_CONTROLLER_SSH_PUBLIC_KEY`, required public key text |
| `controller_console_password` | `SOPHON_CONTROLLER_PASSWORD`, required independent console password |
| `controller_vm_name`, `controller_marker` | Fixed to `sophon-controller`, `sophon-controller-v1` |
| `controller_cores`, `controller_memory` | `4`, `4096` MiB |
| `controller_os_disk_size`, `controller_data_disk_size` | `20G`, `100G`; data must exceed 3x ISO size + 2 GiB |
| `controller_dns` | `vnet_dns` (one supplied DNS IP, no public fallback) |
| `controller_ready_retries`, `controller_ready_delay` | `180`, `10` seconds |
| `nfs_storage_name` | `sophon-nfs` (Proxmox storage ID, not a legacy VM) |
| `proxmox_disk_storage_id` | `local-lvm` |

Also requires the authenticated Proxmox role facts/settings (`proxmox_api`,
`proxmox_ticket`, `proxmox_csrf`, `proxmox_node`, `proxmox_validate_certs`,
`proxmox_iso_storage_id`, `proxmox_iso_storage_path`), `nfs_ip`,
`vnet_gateway`, numeric `vnet_cidr` prefix, `vnet_interface`, optional `vnet_vlan`,
and `portainer_image_tarname` from global settings.

`tasks/upload.yml` is reusable with `controller_upload_source` (absolute local
file path) and `controller_upload_filename` (safe ISO filename, including
`.qcow2.iso` for OS import). It sends the ticket/CSRF token through curl's stdin,
validates upload JSON and UPID completion, and checks storage visibility.
It uses `ansible_playbook_python` and curl on the laptop.

## Prestaging contract

Copy all files from `roles/controller/files/` into the image rootfs at
`/usr/local/share/sophon-controller/`, then execute
`/bin/sh /usr/local/share/sophon-controller/controller-setup.sh` **inside** the
rootfs (a proot build is supported). The legacy NFS builder's customization
line parser cannot execute this script.
Uploads are named by their local SHA256 and reused by exact volume ID on a
retry before VM creation; no existing VM hardware is changed on resume.

Install these Alpine packages before executing the script:
`qemu-guest-agent nfs-utils openssh openssh-client-default e2fsprogs
e2fsprogs-extra util-linux-misc blkid findmnt flock wipefs mount openrc podman
fuse-overlayfs iptables ip6tables doas python3 diffutils nfs-utils-openrc
rpcbind-openrc qemu-guest-agent-openrc openssh-server-common-openrc`. The `util-linux`
package itself is empty on Alpine 3.21; explicit subpackages matter. Base Alpine
provides `base64`, `ip`, `mount`, and `mountpoint`. Cgroups are enabled at boot;
rootful Podman uses overlay storage on `/export/containers-storage`.

No password or operator SSH key goes into the image or ISO. The root account
is locked; QGA installs `operator`'s SSH key and console password. SSH password
authentication is disabled. `/etc/doas.conf` grants only the root-owned
`/usr/local/bin/sophon-controller` wrapper, not a shell or unrestricted Podman.
Its `setenv` rule preserves only the exact Phase2 names in the root-owned
`/usr/local/share/sophon-controller/service-env` file (the role's `files/service-env`).
There is no `keepenv`, wildcard environment forwarding, or bootstrap credential
forwarding.

The attached ISO9660 volume must be labelled `SOPHON_DEPLOY`:

```
/project/deployment.py
/project/site.yml
/project/site.json
/project/...                       # roles, group_vars, runner dependencies
/project/artifacts/containers/...   # includes portainer_image_tarname
/runner.tar                        # sophon-nix-runner:latest
/SHA256SUMS
```

`SHA256SUMS` must cover **every** regular file under `project/` and `runner.tar`
(ordinary `sha256sum` format, relative paths, optional `./` prefix), and not
itself. Symlinks, traversal, duplicates, checksum mismatches, and bundled Kopia
private keys are rejected. The manifest checks integrity, not authenticity:
the operator must trust the prestaged image and ISO.

Import copies the project to `/export/sophon`, loads the runner without pulling,
hardlinks the Portainer tar into `/export/containers`, generates the persistent
Kopia keypair and all SSH host-key pins in
`/export/sophon/artifacts/kopia/{id_ed25519,known_hosts}`, authorizes SFTP-only
user `kopia` for `/export/kopia/repo`, and then publishes
`/var/lib/sophon/controller-ready`. Logs persist in
`/var/log/sophon-import.log`; a failure marker causes bootstrap to fail before
InfraVM creation. NFS is exported to the configured vnet subnet with
`no_root_squash`; this is a trusted infrastructure network, not an untrusted
multi-tenant export. Podman storage/project permissions remain root-only.
The import log is root-owned and operator-group-readable (`0640`), so
`operator` can run `tail -n 100 /var/log/sophon-import.log` without doas even
when import failed and the runner cannot start.

## Safety and recovery

Any `sophon-nfs` VM (including a template) causes refusal **before mutation**.
An existing `sophon-controller` must be a single running non-template QEMU VM
on the configured node, have description exactly `sophon-controller-v1`, and
have matching `/etc/sophon-controller-marker` in the guest. A stopped existing
VM is refused, rather than powered on before its guest ownership is verified.
The role never deletes a VM or modifies existing VM hardware.

Only a VM created in the current invocation gets permission to initialize
`/dev/vdb`. Formatting requires no disk signatures **and every byte zero**.
The UUID is recorded before formatting and checked on every boot, along with
the mounted filesystem and the data marker. Any unrelated filesystem, raw
nonzero data, UUID mismatch, or conflicting mount is refused, never wiped.
The whole-disk zero check can take several minutes on large disks. An interrupted
format that leaves a partial filesystem fails closed and requires operator
inspection; it is never automatically reformatted.

An import uses a staging directory and an atomic rename, never overwriting
`/export/sophon` or generated keys on resume. Reboots remount `/export` by UUID
and restart the NFS/import checks without requiring the ISO after successful
import. The ISO remains attached for manual recovery.

From the operator console or key-only SSH session run:

```
doas /usr/local/bin/sophon-controller
```

The wrapper runs host-networked, offline, non-privileged Podman with only the
persistent project mounted at `/workspace`; default arguments are
`python3 deployment.py --phase services --serve --port 41327`. The GUI's default
listener remains loopback-only. Use an SSH local forward if remote access is
needed. Arguments can be passed explicitly to the wrapper. It replaces the host
environment with a fixed PATH/HOME plus only explicitly supplied Phase2
allowlisted variables and sets `SOPHON_DEPLOYMENT_PHASE=services` inside the
container. Supplied values, including empty strings, are preserved exactly;
unset variables stay unset and let the application use its `site.json`/defaults.
Podman receives `--env NAME`, never secret values in its command arguments.
All Proxmox/Controller variables, arbitrary host variables, and any caller-set
`SOPHON_DEPLOYMENT_PHASE` are excluded. No environment values are written into
the image, ISO, an env file, or the doas configuration.
Arguments are passed to `deployment.py`, not interpreted as a replacement
command. For example, run
`doas /usr/local/bin/sophon-controller --phase services -- site.yml`;
do not repeat the `python3 deployment.py` prefix.
The GUI's generated shell command can be run from any operator host directory:
export its allowlisted `SOPHON_*` settings, then run
`doas sophon-controller --phase services --state-dir /workspace/artifacts/deployment/services -- site.yml`.
The `/workspace` argument is consumed inside the runner; no host `cd` or
host-side Ansible installation is required.
Nonsecret `site.json` loading and service credential entry belong to the GUI;
neither is injected through an ISO environment file or bootstrap credentials.
The wrapper starts the dashboard without requiring a domain. Enter
`SOPHON_DOMAIN` in Phase 2 Configure, or export it before a terminal run; it
is not carried in the ISO's `site.json`.
Before service deployment, `site.yml` validates the domain and InfraVM address
and refreshes a marked block in the runner's `/etc/hosts`. It maps the apex plus
`traefik`, `auth`, `ldap`, `git`, `portainer`, `kopia`, and `dns` to InfraVM.
Other entries are preserved; subsequent runs replace the block, removing stale
domain/address mappings. The Alpine host's resolver is unchanged.
This setup happens after form entry, so changing the domain or InfraVM IP in
Configure does not require a wrapper restart. Do not run `site.yml` directly on
the host; it is a runner-only playbook.

Focused checks: `python3 tests/test_controller.py` (stdlib + PyYAML).
