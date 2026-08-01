# Certificates cross the air-gap as a sneakernet refresh

When the site has no egress, Traefik cannot renew. A connected machine mints
the certificates and the operator carries `artifacts/traefik/acme.json` in by
hand, roughly monthly. The site-side Traefik configuration is unchanged — it
simply finds `/data/acme.json` already populated.

## How it works

- The transport already exists: `roles/traefik/tasks/restore_acme.yml` pushes
  the local `acme.json` into the `traefik_traefik_data` volume through the
  Portainer-proxied Docker archive API, before the stack starts.
- Issuance is driven from the connected side, which means the FQDN list must
  be explicit there — one single-name certificate per service hostname, since
  wildcards and SAN lists are both ruled out. A service added after a refresh
  has no certificate until the next one.
- The Cloudflare API token stays on the connected machine. It must not cross
  the air-gap boundary; a zone-edit token on a disconnected box is all risk and
  no benefit.

## Consequences

- Certificates are 90 days and Traefik renews at 30 days remaining, so a
  monthly cadence allows three missed attempts before anything expires.
- A missed refresh degrades quietly: Traefik keeps serving the stored
  certificate and logs renewal failures. It does not fall back to self-signed
  and does not stop serving.
- `roles/traefik/tasks/backup_acme.yml` inverts in this case. The connected
  side holds the authoritative copy, so the site-side snapshot can only return
  what was carried in.
