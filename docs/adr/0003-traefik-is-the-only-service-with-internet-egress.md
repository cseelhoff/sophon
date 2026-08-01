---
status: superseded by ADR-0006
---

# Traefik is the only service allowed direct internet egress

**Superseded.** This ADR narrowed runtime egress to Traefik alone, which would
have removed the Cloudflare tunnel integration. That integration is a
first-class feature and the common deployment target is an internet-connected
homelab. See ADR-0006.

The certificate constraints recorded here still hold and are restated in
ADR-0006.

---

Traefik keeps its live Let's Encrypt configuration — `certificatesResolvers`
with the Cloudflare DNS-01 challenge, one single-name certificate per router,
storage in `/data/acme.json` — exactly as it would be configured with full
internet access. It is a deliberate, narrow hole: no other Sophon service
reaches the internet at runtime, and no service depends on Traefik having done
so.

When the site has no egress, certificates are minted while connected and
carried in as an artifact. See ADR-0004.

## Considered options

- **Private CA with a `*.<domain>` wildcard.** Rejected: wildcards are
  unacceptable, and a wildcard's private key would be a long-lived secret
  living in `artifacts/`.
- **Private CA plus an in-cluster ACME server (step-ca).** Rejected: a whole
  additional service to prestage and bootstrap, and it changes Traefik's
  configuration shape.
- **Multi-SAN certificate covering every hostname.** Rejected: SAN lists are
  unacceptable, and they leak the full service inventory into every
  certificate.

## Consequences

- `domain_name` must be a real, publicly-registered zone under Cloudflare
  management. `.local` and `.internal` domains cannot be used — DNS-01 proves
  control of a public zone. The zone being public does not make the site
  reachable; no A record needs to point at anything routable.
- A Cloudflare zone-edit API token is required.
- Certificates are per-FQDN, driven by router discovery, so no hostname list
  needs to be maintained anywhere in the connected case.
- Traefik must have a correct clock. An air-gapped InfraVM has no public NTP
  source and currently no time configuration at all.
