# Decisions

The choices the design rests on, newest understanding first. Each is cheap to state and costly to reverse.

1. **Two reusable ISOs, installed unattended.** `sophon-installer.iso` (the stock CoreOS live ISO with a small
   live Ignition config baked in by `coreos-installer iso customize`) plus `sophon-deployment.iso`. The laptop
   creates a blank VM and attaches both; the installer runs by itself, offline, and only ever to a blank disk, so
   booting it again cannot reinstall. No guest agent, SSH, disk conversion, controller VM or NFS. The `core`
   password is chosen at the first boot (the account is locked until then, so no default password exists) and the
   static network in step 4. Site data and secrets never enter either ISO.
2. **The runner lives on InfraVM.** A container that sees only Portainer's HTTP API: no hypervisor
   credentials, SSH, host root or Podman socket. Step 4 therefore needs nothing from the laptop.
3. **Portainer is the deployment substrate.** Stacks, image loads and volume seeding all go through its API.
   If Portainer is down, fix it by hand and rerun. It is reconstructible, not a backup target.
   Nothing may exist only in Portainer: keep definitions in IaC and supplied credentials externally.
   Backup operation has no Portainer API key or dependency on the management UI.
4. **Deploy fetches nothing.** Step 1 caches every artifact; compose files use `pull_policy: never`; a missing
   archive fails by name. Internet at the site is normal but optional.
5. **Egress is a degraded mode, not a different product.** Without it Cloudflare tunnel publishing and
   automatic renewal stop; nothing blocks deployment. Only Traefik ever talks to Let's Encrypt.
6. **Certificates cross an air gap on a secret disk.** The wildcard `acme.json`, issued and renewed on a connected
   machine, is written to a small ISO that is attached to the InfraVM for step 4. The Cloudflare token never leaves
   that machine. See [certificates.md](certificates.md).
7. **Passwords are never generated or stored.** The operator supplies each and keeps it in a password
   manager; reruns need the same values. Generated identity state (`acme.json`, ACME account) lives under
   `artifacts/` and is irreplaceable.
8. **State is either reconstructible or irreplaceable.** A rerun rebuilds the Keycloak realm, seeded users and
   routing. Gitea repositories, user-created LDAP entries and `acme.json` need
   encrypted native backups and tested restoration. Primary backups contain only
   irreproducible user data and application-generated identity. Databases use native
   exports, never live data-directory copies. The v3 collector reads named read-only
   volume mounts and streams exec stdout through a restricted rootless engine proxy.
   Kopia deploys after core source volumes exist, with every image carried offline.
   Rebuild means rerun Configure, then explicitly restore state, not restore a system
   copy. Configure never restores production state.
9. **Addresses are inputs.** The InfraVM address is confirmed at the console in step 4 (the VM starts on its DHCP
   lease, which is the default; reserve it or type a static one from outside the DHCP pool) and saved, not
   rediscovered; every service name resolves to it.
10. **Existing VMs are never adopted or rewritten.** A VM with the InfraVM name is only started, and only if
    this workflow marked it.
11. **One container image is the laptop toolchain.** `docker run <flags> sophon-tools <command>` runs Ansible, xorriso,
    govc, coreos-installer and Traefik from an image built from the `Containerfile`; the InfraVM runner is that file's smaller
    `runner` stage. Docker or Podman is the only laptop requirement (no Nix, bash, Python or Ansible install),
    and the commands are plain `docker run` lines that behave the same in bash and PowerShell. The tools image
    drives the host engine through its socket for image pulls, saves and builds, and reaches the hypervisor
    through the engine's NAT. Base images are pinned by tag, not digest. Linux/WSL and Windows are verified.
12. **Backup consistency does not depend on upload speed.** Journal exact stop/export
    identities, stop all filesystem writers, reflink-clone declared paths (plain-copy
    fallback only while writers are stopped), perform native exports, then resume
    writers before uploading immutable staging. Interrupted exports are cancelled
    before writer recovery. No backup host units are installed.
13. **The v3 contract is a clean break.** Only v3 labels/manifests are read. The local
    `kopia-legacy` tag preserves the old tool for old snapshots; there are no compatibility
    shims. Unmounted declared volumes fail their group with an actionable message.
    Undeclared volumes are reported but not copied or a whole-run failure. Successful
    groups in a partial run upload with explicit failures and a nonzero operator result.
14. **The primary collector has narrow filesystem/engine access.** Only declared
    named volumes are mounted read-only. UID 0 is inside rootless Podman, with only
    `DAC_OVERRIDE`, `CHOWN`, `FOWNER`, read-only rootfs and no-new-privileges. The separate
    cap-free socket proxy permits list, inspect, exec, stop and start only; no archive,
    image, create/delete or volume APIs. The README justifies each capability.
