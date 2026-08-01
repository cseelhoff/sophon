# Site egress is expected; air-gapped operation is a degraded mode

Supersedes ADR-0003.

The common deployment target is an internet-connected homelab. Services may use
Site egress at runtime, and two features depend on it: Traefik's live ACME
renewal, and the Cloudflare tunnel integration that publishes services for
external access. Both are first-class and neither is removed.

Air-gapped operation is a supported degraded mode, not a separate deployment
mode. Without Site egress those two features stop; everything else continues to
work on the local network, and certificates arrive by Sneakernet refresh
(ADR-0004). Nothing in Deploy blocks on egress.

## The Cloudflare tunnel integration

`coredns-dockerdiscovery` watches container labels and maintains both the local
DNS records and the tunnel ingress rules for the zone. It is the mechanism that
makes a new service externally reachable by adding a label, and it works today.

This does not reintroduce the bootstrap tunnel. ADR-0002 removed
`portainer-bootstrap.<domain>` because the Controller reaches InfraVM directly;
tunnels serve end users, not Sophon itself.

## Certificate constraints, carried forward from ADR-0003

- Traefik's configuration is the ordinary online configuration:
  `certificatesResolvers` with the Cloudflare DNS-01 challenge, storage in
  `/data/acme.json`.
- One single-name certificate per router. No wildcards, no SAN lists.
- `domain_name` must be a real, publicly-registered zone under Cloudflare
  management. `.local` and `.internal` cannot be used, because DNS-01 proves
  control of a public zone. A public zone does not make the site reachable.
- A Cloudflare API token is required, scoped for the zone's DNS records and for
  tunnel configuration.

## Consequences

- Deploy must never fail because egress is absent. Anything that would block on
  a network call needs a bounded timeout and a clear degraded outcome.
- Traefik needs a correct clock. An air-gapped InfraVM has no public NTP source
  and currently no time configuration at all.
- The Cloudflare token does cross the air-gap boundary in the tunnel case,
  because cloudflared needs it at the site. In a genuinely disconnected
  deployment it should be omitted rather than shipped.
