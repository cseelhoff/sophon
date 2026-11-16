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

**Installer ISO**: `sophon-installer.iso`; the stock Fedora CoreOS live ISO with a small Ignition config baked in (`coreos-installer iso customize`) that runs the unattended install. Reusable, secret-free.

**Deployment ISO**: `sophon-deployment.iso`; project, runner image, container archives and installer. Reusable, secret-free.

**Artifact**: a file Prepare produces and a later step consumes (image archive, ISO, runner image).

**Service FQDN**: `<service>.<domain>`; how everything addresses a service. Raw IPs are used only for the hypervisor and Portainer's first contact.

**Zone**: `<domain>` itself, public on Cloudflare for DNS-01 and served authoritatively on site by CoreDNS.

**Certificate disk**: a small ISO (label `SOPHON_CERTS`) holding the `acme.json` of one wildcard certificate (`<domain>` and `*.<domain>`) with its full chain and private key, no ACME account. The one secret thing carried into a site without internet.

**Site egress**: internet access available to services at runtime. Expected normally; its absence is a supported, degraded mode (no tunnel publishing, no self-renewal).

**Reconstructible / irreplaceable state**: what a rerun rebuilds, versus what only a backup can restore.

**Backup group**: v3-labelled workloads whose writers pause together while declared
irreproducible paths are cloned and native dumps are captured. Writers resume before upload.

**Primary snapshot**: real irreproducible files, native dumps and a v3 manifest
with workload images and Sophon source version; not a system or GUI backup.

**Coarse snapshot**: an optional weekly, last-resort cold copy of the rootless
Podman volumes tree. Applications pause only for a strict reflink clone, then
their previously running IDs restart without dependency ordering before upload.
Working storage is a separate host bind outside the copied tree.

**Engine proxy**: the unpublished allowlisted socket proxy used by the backup
container, separate from Arcane. The primary collector has named read-only
volume mounts, not the engine socket or whole volumes directory.
