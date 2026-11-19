# Sophon glossary

**Step**: one of the four stages of a deployment: 1 Prepare, 2 Provision, 3 Install, 4 Configure.
`sophon-deploy` and the playbook launcher use the same names (`--step provision|configure`, covering steps 1-2 and 4).

**Prepare**: the connected step that builds every artifact the later steps carry.

**Provision**: creating a blank InfraVM with the installer and deployment ISOs through the hypervisor's management API.

**Install**: step 3, automatic: the installer ISO finds the deployment ISO, puts CoreOS and the Sophon project on InfraVM's only blank disk, offline, and reboots. The first boot asks for the `core` password.

**Configure**: step 4, `sophon-deploy` on the console: sets the network, then runs the runner to reconcile declarative Compose stacks over the rootless Podman socket.

**InfraVM**: the single Fedora CoreOS VM that runs the runner, Arcane and every service as rootless Podman containers.

**Runner**: the Ansible container on InfraVM. It reconciles Compose stacks in `/var/lib/sophon/stacks` over the rootless Podman socket and
preflights the declared SFTP destination; it has no hypervisor/guest SSH or host root privileges.

**CoreOS Installer ISO**: `sophon-coreos.iso`; the stock Fedora CoreOS live ISO with a small Ignition config baked in (`coreos-installer iso customize`) that runs the unattended install. Reusable, secret-free.

**Apps Bundle ISO**: `sophon-apps.iso`; project, runner image, container archives and installer. Reusable, secret-free.

**SFTP Server ISO**: `sophon-sftp-server.iso`; standalone hybrid BIOS/UEFI Alpine 3.24 installer ISO built by Prepare that installs a turnkey, confined OpenSSH `internal-sftp` backup server onto a single blank disk (`sophon-backup-setup` / `sophon-authorize-key`). Reusable, secret-free.

**Artifact**: a file Prepare produces and a later step consumes (image archive, ISO, runner image).

**Service FQDN**: `<service>.<domain>`; how everything addresses a service. Raw IPs are used only for hypervisor management APIs and SFTP backup endpoints.

**Zone**: `<domain>` itself, public on Cloudflare for DNS-01 and served authoritatively on site by CoreDNS.

**Secrets disk**: `artifacts/secrets/sophon-secrets.iso` (mode `0600`, label `SOPHON_CERTS`), unifying optional unattended seed configuration (`/sophon-seed.json`, including prod `SOPHON_NETWORK_CIDR`, dev `SOPHON_DEV_NETWORK_CIDR`, and paired `sophon-backup` + `sophon-restore` SFTP keys) and/or the pre-issued air-gapped wildcard certificate (`/acme.json`).

**Site egress**: internet access available to services at runtime. Expected normally; its absence is a supported, degraded mode (no tunnel publishing, no self-renewal).

**Reconstructible / irreplaceable state**: what a rerun rebuilds, versus what only a backup can restore.

**Backup group**: `sophon.backup.enable=true` workloads grouped together (defaulting
to `com.docker.compose.project`) whose hot volume reflinks, `.kopiaignore` rules
and native stdout database dumps are staged together by Kopia's before-snapshot
action without stopping containers.

**Primary snapshot**: real irreproducible files, native dumps and a v3 manifest
with workload images and Sophon source version, scheduled natively by Kopia
(`backup@sophon:/sophon-state`); not a system or GUI backup.

**Coarse snapshot**: an optional weekly, last-resort crash-consistent Btrfs
read-only subvolume snapshot of the rootless Podman `volumes` subvolume on `/var`.
Zero containers stop and no engine socket is mounted; Kopia uploads the read-only
snapshot from a disjoint working directory on `/var` and destroys the snapshot when complete.

**Dump proxy**: the unpublished, zero-capability Python socket proxy (`dump-proxy`)
used by `kopia`. It strips `Config.Env` from container inspection and executes
only dump commands declared on the target container's own `sophon.backup.*` labels;
arbitrary `/exec`, `/stop`, `/start` and `/archive` endpoints are rejected.
