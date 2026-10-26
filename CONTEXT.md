# Sophon

Sophon provisions a self-contained homelab onto a Proxmox host. Everything the
deployment needs is staged ahead of time, then applied in Bootstrap and Service
deployment. The site is normally
internet-connected; a site with no egress is a supported degraded mode.

## Language

**Prestage**:
The connected phase that produces every artifact the deployment will need. The
connected preparation is independent of the networks used for provisioning.
_Avoid_: deployment, service configuration

**Deploy**:
The overall operation comprising Bootstrap, Handoff, and Service deployment.
_Avoid_: a single controller session

**Bootstrap**:
The first deployment phase: prepare the installation media and provision the
target InfraVM through the hypervisor management network.
_Avoid_: service deployment

**Handoff**:
The operator-managed transition from the Bootstrap laptop to InfraVM.
It implies no particular physical connection, VPN, tunnel, or routing arrangement.
_Avoid_: automatic network switch

**Service deployment**:
The site-local deployment of Portainer and services from the Controller runner
on InfraVM, without hypervisor management access or credentials.
_Avoid_: bootstrap, full provisioning

**Deployment bundle**:
The offline project and its service artifacts, excluding passwords, API tokens,
private keys, and prior deployment state.
_Avoid_: backup, credential store

**Deployment progress**:
The observed advancement and outcome of provisioning work in a particular
Sophon run, not a statement of the site's current service health.
_Avoid_: online status, service health

**Deployment stage**:
A top-level unit of work in a Sophon run, including the dependency checks,
deployment, and configuration work it invokes; nested work belongs to that stage.
_Avoid_: role status, task status

**Deployment complete**:
A component's deployment and configuration work completed successfully in a
particular Sophon run; this does not assert that the component is currently online.
_Avoid_: online, healthy, ready

**Service health**:
The observed ability of a Sophon service to perform its intended function at
the time of a check, independently of a deployment run's outcome.
_Avoid_: deployment progress, deployed

**Artifact**:
A file produced by Prestage and consumed by Deploy, such as a container image
archive or VM disk image. Distinct from generated identity state.
_Avoid_: cache, asset, blob

**Generated state**:
State Sophon creates on first run and reuses on every run after, such as the
Kopia keypair. Stored under `artifacts/`. Losing it can mean losing access to a
running deployment. Passwords are operator-supplied inputs, retained outside
Sophon and passed through the environment or explicit Ansible overrides.
_Avoid_: config, credentials, facts

**Reconstructible state**:
Service state that Ansible declares and a re-run rebuilds — the Keycloak realm
and its clients, seeded LDAP users, Traefik routing. Must survive a restart,
does not need backing up.
_Avoid_: derived state, ephemeral state

**Irreplaceable state**:
Service state that exists nowhere in the repo — Gitea repositories, user-created
LDAP entries, `acme.json`, `artifacts/`. Kopia's responsibility.
_Avoid_: user data, persistent state

**Air-gap boundary**:
The point between Prestage and Deploy where the operator physically moves to
the disconnected network. Crossing it is manual and one-way.
_Avoid_: network switch, cutover

**Site egress**:
Internet access available to services at runtime, after Deploy. Expected in the
common case, and distinct from the connectivity Prestage needs.
_Avoid_: internet access, online mode

**Egress-dependent feature**:
A capability that stops working without Site egress but never blocks Deploy —
Cloudflare tunnel access and automatic certificate renewal. Losing one degrades
the site; it does not break it.
_Avoid_: optional feature, online-only feature

**Sneakernet refresh**:
The periodic manual operation that carries freshly issued certificates across
the air-gap boundary when there is no Site egress.
_Avoid_: cert sync, manual renewal

## Machines and networks

**Controller**:
The execution environment running Ansible for the current deployment phase.
_Avoid_: assuming one machine performs every phase, dedicated Controller VM

**Bootstrap laptop**:
The initial operator machine that prepares artifacts and performs Bootstrap
through the hypervisor management network.
_Avoid_: Controller runner

**Controller runner**:
The containerized deployment environment on InfraVM that runs site-local
Ansible automation.
_Avoid_: Controller VM, NFS VM, Bootstrap laptop

**vnet**:
The guest network InfraVM and an optional NFS VM attach to; InfraVM's static
address is chosen by the operator during installation.
_Avoid_: bridge, LAN, network

**NFS VM**:
An optional separate storage VM used when an external backup destination is
required; it is not a prerequisite for installing InfraVM or starting services.
_Avoid_: Controller VM, bootstrap artifact server, generic storage for all service data

**InfraVM**:
The Fedora CoreOS VM that runs the Controller runner and every Sophon service
as rootless Podman containers.
_Avoid_: CoreOS VM, container host, sophon-infravm

## Naming and reachability

**Service FQDN**:
The `<service>.<domain>` name a service is addressed by. Everything reaches a
Sophon service this way — the Controller, other services, and end users. Direct
IP addressing is reserved for Sophon's own provisioning of Proxmox, the NFS VM,
and Portainer.
_Avoid_: hostname, URL, endpoint

**Zone**:
`<domain>` itself. Publicly registered under Cloudflare so ACME DNS-01 can
validate it, and served authoritatively on the site by CoreDNS. The public side
holds no A records for site services.
_Avoid_: domain, DNS zone
