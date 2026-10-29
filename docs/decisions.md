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
   If Portainer is down, fix it by hand and rerun.
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
   routing. Gitea repositories, user-created LDAP entries and `acme.json` need a backup, which is deliberately
   out of scope for now.
9. **Addresses are inputs.** The InfraVM address is entered at the console, from outside the DHCP pool, not
   discovered; every service name resolves to it.
10. **Existing VMs are never adopted or rewritten.** A VM with the InfraVM name is only started, and only if
    this workflow marked it.
11. **One container image is the laptop toolchain.** `docker run <flags> sophon-tools <command>` runs Ansible, xorriso,
    govc and Traefik from an image built from the `Containerfile`; the InfraVM runner is that file's smaller
    `runner` stage. Docker or Podman is the only laptop requirement (no Nix, bash, Python or Ansible install),
    and the commands are plain `docker run` lines that behave the same in bash and PowerShell. The tools image
    drives the host engine through its socket for image pulls, saves and builds, and reaches the hypervisor
    through the engine's NAT. Base images are pinned by tag, not digest. Only the Linux path is verified.
