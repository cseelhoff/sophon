# Install InfraVM from stock CoreOS media and a separate deployment bundle

The separate Controller/NFS VM creates an unnecessary early storage and management dependency. Instead, the bootstrap laptop prepares a stock Fedora CoreOS live ISO and a Sophon artifact ISO, creates InfraVM with a fresh disk, and attaches both; the deployment runner lives on InfraVM, and an NFS VM is optional backup infrastructure rather than a prerequisite.

The operator starts Phase 2 with a command from the hypervisor's live console. The entry script confirms the installation disk, installs CoreOS with first-boot configuration and persistent deployment files, then reboots into the installed deployment entry point; starting containers only in the temporary live environment is not a persistent installation.

No SSH public key or SSH connection is required for this local installation and deployment workflow. The live-console entry script collects the installed account's password locally and supplies its hash in the generated first-boot configuration; passwords are not bundled on either ISO, and console access does not require enabling SSH password authentication.

The installed account retains the standard CoreOS username `core`; arbitrary account naming is not part of this flow. Existing rootless Podman account conventions are retained.

The installed VM's static IP, prefix length, gateway, and DNS are collected at the Phase 2 live console, not embedded by the bootstrap laptop. Phase 1 selects the hypervisor-side bridge/VLAN, while the two installation ISOs remain reusable across sites.

Installation copies the deployment project and required artifacts to persistent InfraVM storage, checking capacity before copying. Subsequent boots and service reruns do not depend on either ISO remaining attached; application volumes and deployment state also persist on disk.

After reboot, the primary continuation path is a console wizard that collects the domain and service credentials, hides password input, and runs deployment with visible output. The existing web dashboard remains an optional alternative, not a prerequisite requiring browser connectivity.

The normal installation and service run exclude backup-VM provisioning and Kopia deployment, and require neither NFS settings nor Kopia credentials. A later opt-in backup workflow will be designed separately rather than expanding this redesign or introducing hypervisor credentials into the site-local service phase.

The host entry script and installed first-boot service perform the limited host setup, load the offline images, and start Portainer and the Controller runner. The runner manages service workloads through Portainer HTTP with its project/artifact storage mounted, not through SSH, a directly mounted Podman socket, or privileged host access; Portainer itself retains the rootless Podman socket needed to manage workloads.

This replaces the separate Controller VM architecture in [ADR-0012](0012-two-phase-controller-handoff.md). The implementation uses `bootstrap.yml` for the management-only VM lifecycle and `site.yml` for local Portainer-based services. After reboot the operator logs in as `core` and runs `sophon-controller`; `--dashboard` selects the optional web interface. Automated fixture checks do not replace an actual CoreOS installation and live-hypervisor validation.
