# Architecture

How the pieces fit and what breaks what. Vocabulary is in [../CONTEXT.md](../CONTEXT.md).

## Shape

```text
laptop ── hypervisor API ──▶ blank InfraVM + two ISOs     (steps 1-2)
live console ── install.sh ──▶ persistent CoreOS            (step 3)
InfraVM: rootless Portainer + runner container ──▶ services (step 4)
```

Every play targets `localhost`. The laptop only creates a blank VM; it never reaches into the guest
(no SSH, guest agent, NFS, injected Ignition or disk conversion). The two media are reusable and hold no
site data or secrets: the stock CoreOS live ISO and `sophon-deployment.iso` (the service project,
runner image, container archives and installer). Networking, the `core` password, the domain and all
service credentials are entered at the consoles. See [decisions.md](decisions.md).

Three surfaces do all the work; knowing which one is in play explains most failures:

1. **The hypervisor API** (steps 1-2 only). Proxmox: REST ticket + `community.proxmox`. vCenter: `govc`
   with verified HTTPS and environment-only credentials. Existing VMs are only started, and only if this
   workflow marked them; anything else is left untouched.
2. **The live console** (step 3). `guest/install.sh` verifies the media checksums, requires a zero-filled
   disk, renders Ignition locally and runs `coreos-installer --offline`.
3. **The Portainer API** (step 4). Stacks, image loads and volume seeding all go through Portainer's HTTP
   API; there is no other path into InfraVM ([decisions.md](decisions.md)).
   If Portainer is down, fix it by hand, then rerun.

## Artifacts

Step 1 writes everything into `artifacts/` and is cached: with a complete cache it touches no network.

- Portainer's own image is loaded by `guest/host-bootstrap.sh` from the persistent project directory,
  because Portainer cannot deliver itself.
- Every other image is POSTed to Portainer's Docker proxy right before its stack, on every run.
  Compose files use `pull_policy: never`, so a missing archive fails by name instead of pulling silently.

`prepare_container_images` in `roles/prepare/defaults/main.yml` is the complete list; keep it in step with
each role's `*_image_tars`.

## Order of the services

```text
CoreDNS resolves *.<domain> ─▶ Traefik routes by Host and gets certificates
   ─▶ LDAPS at ldap.<domain> verifies ─▶ Keycloak federates OpenLDAP
   ─▶ Gitea and Portainer trust Keycloak (OIDC)
```

Each link is load-bearing: a DNS problem shows up as Keycloak failing on an LDAPS certificate. Read
failures from the left.

## DNS

Two resolvers share port 53 on InfraVM. Podman's aardvark-dns serves `10.89.0.1:53` for container names;
CoreDNS binds only `127.0.0.1:53` and `<infravm_ip>:53` for the zone, and anything it does not own goes to
the operator's upstream DNS (`SOPHON_VNET_DNS`). Containers reach each other through aardvark-dns, which
forwards unknown names to CoreDNS. CoreDNS is a custom build (`coredns-dockerdiscovery`) that publishes
records from container labels and can sync Cloudflare tunnel routes. Before CoreDNS exists the runner maps
the service names to InfraVM in its own `/etc/hosts` ([4-configure.yml](../4-configure.yml)); the host
resolver and other clients are never changed.

## Certificates

Traefik issues one single-name certificate per service through Let's Encrypt DNS-01 (Cloudflare), so no
inbound reachability is needed. Without egress, the whole `acme.json` is carried in as one payload
([certificates.md](certificates.md)).
The certs-dumper container extracts the LDAP certificate from it.

## State

Reconstructible state (the Keycloak realm, seeded users, routing) is rebuilt by a rerun. Irreplaceable
state (Gitea repositories, user-created LDAP entries, `acme.json`, the ACME account) is not;
back it up ([decisions.md](decisions.md)).
