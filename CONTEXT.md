# Sophon glossary

**Step**: one of the four stages of a deployment: 1 Prepare, 2 Provision, 3 Install, 4 Configure.
`sophon-deploy` and the playbook launcher use the same names (`--step provision|configure`, covering steps 1-2 and 4).

**Prepare**: the connected step that builds every artifact the later steps carry.

**Provision**: creating a blank InfraVM with the installer and deployment ISOs through the hypervisor's management API.

**Install**: step 3, automatic: the installer ISO finds the deployment ISO, puts CoreOS and the Sophon project on InfraVM's only blank disk, offline, and reboots. The first boot asks for the `core` password.

**Configure**: step 4, `sophon-deploy` on the console: sets the network, then runs the runner against Portainer to deploy the services.

**InfraVM**: the single Fedora CoreOS VM that runs Portainer, the runner and every service as rootless Podman containers.

**Runner**: the Ansible container on InfraVM. It has no hypervisor access, SSH or host privileges.

**Installer ISO**: `sophon-installer.iso`; the stock Fedora CoreOS live ISO with a small Ignition config baked in (`coreos-installer iso customize`) that runs the unattended install. Reusable, secret-free.

**Deployment ISO**: `sophon-deployment.iso`; project, runner image, container archives and installer. Reusable, secret-free.

**Artifact**: a file Prepare produces and a later step consumes (image archive, ISO, runner image).

**Service FQDN**: `<service>.<domain>`; how everything addresses a service. Raw IPs are used only for the hypervisor and Portainer's first contact.

**Zone**: `<domain>` itself, public on Cloudflare for DNS-01 and served authoritatively on site by CoreDNS.

**Certificate disk**: a small ISO (label `SOPHON_CERTS`) holding the `acme.json` of one wildcard certificate (`<domain>` and `*.<domain>`) with its full chain and private key, no ACME account. The one secret thing carried into a site without internet.

**Site egress**: internet access available to services at runtime. Expected normally; its absence is a supported, degraded mode (no tunnel publishing, no self-renewal).

**Reconstructible / irreplaceable state**: what a rerun rebuilds, versus what only a backup can restore.
