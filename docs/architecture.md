# Architecture

How the pieces fit and what breaks what. Vocabulary is in [../CONTEXT.md](../CONTEXT.md).

## Shape

```text
laptop ── hypervisor API ──▶ blank InfraVM + installer and deployment ISOs   (steps 1-2)
installer ISO ── install.sh --auto ──▶ persistent CoreOS    (step 3, unattended)
InfraVM: rootless Portainer + runner container ──▶ services (step 4)
```

Every play targets `localhost`. The laptop only creates a blank VM; it never reaches into the guest
(no SSH, guest agent, NFS or disk conversion). The two media are reusable and hold no site data or secrets:
`sophon-installer.iso` (the stock CoreOS live ISO with a small live Ignition config baked in by
`coreos-installer iso customize`) and `sophon-deployment.iso` (the service project, runner image, container archives and
installer). The `core` password is chosen at the first boot, the network, domain and all service credentials in step 4.
See [decisions.md](decisions.md).

Three surfaces do all the work; knowing which one is in play explains most failures:

1. **The hypervisor API** (steps 1-2 only). Proxmox: REST ticket + `community.proxmox`. vCenter: `govc`
   with verified HTTPS and environment-only credentials. Hyper-V: PowerShell over WinRM (`pypsrp`); every value
   reaches the host as base64 JSON, never inside PowerShell source. Existing VMs are only started, and only if this
   workflow marked them; anything else is left untouched.
2. **The live system** (step 3). `guest/sophon-autoinstall` (baked into the installer ISO) starts `guest/install.sh --auto` from
   the deployment ISO; it verifies the media checksums, requires exactly one unpartitioned disk with no filesystem
   signatures, renders Ignition locally and runs `coreos-installer --offline`.
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
the operator's upstream DNS (`SOPHON_VNET_DNS`, default `1.1.1.1`; it must be a real resolver and never a machine that
forwards its own DNS back to InfraVM, or lookups loop). Containers reach each other through aardvark-dns, which
forwards unknown names to CoreDNS. CoreDNS is a custom build (`coredns-dockerdiscovery`) that publishes
records from container labels and can sync Cloudflare tunnel routes. A `cloudflared` container (the `cloudflared`
role, deployed after Traefik) is the tunnel's connector: it reads the tunnel's token from the Cloudflare API and
forwards public requests to Traefik. Without it Cloudflare answers error 1033. Before CoreDNS exists the runner maps
the service names to InfraVM in its own `/etc/hosts` ([4-configure.yml](../4-configure.yml)); the host
resolver and other clients are never changed.

## Certificates

Traefik uses one wildcard certificate (`<domain>` and `*.<domain>`, an ECDSA P-256 key) from Let's Encrypt DNS-01
(Cloudflare) for every service, so no inbound reachability is needed. Each router names the wildcard itself, so Traefik
never requests per-service certificates. Without egress, a small `acme.json` holding just that certificate and key is
carried in on a small secret ISO ([certificates.md](certificates.md)). The certs-dumper container extracts it for LDAPS and
names the files after the certificate's main domain (`<domain>.crt`, `<domain>.key`).

## State

Reconstructible state (the Keycloak realm, seeded users, routing) is rebuilt by a rerun. Irreplaceable
state (Gitea repositories, user-created LDAP entries, `acme.json`, the ACME account) is not;
back it up ([decisions.md](decisions.md)).
