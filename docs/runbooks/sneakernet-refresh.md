# Runbook: sneakernet certificate refresh

**When:** monthly, at a site with no internet egress.

**Why:** Traefik cannot reach Let's Encrypt or Cloudflare, so certificates
cannot renew themselves. They are minted on a connected machine and carried in.
See [ADR-0004](../adr/0004-certificates-cross-the-air-gap-as-a-sneakernet-refresh.md).

**Skip this** if the site has egress. Traefik renews on its own.

## Timing

Let's Encrypt certificates last 90 days. Traefik renews at 30 days remaining.
Doing this monthly gives you three attempts before anything expires — one
missed trip is recoverable, two is not.

Put it on a calendar. There is no alert when it does not happen; you find out
when browsers start refusing connections.

## What crosses the boundary

Only `acme.json`. **The Cloudflare API token must not go to the disconnected
site** — it can rewrite your public DNS, and it has no purpose there because
nothing at the site can reach Cloudflare anyway.

## Step 1 — On the connected machine

You need a machine with internet access, the Cloudflare API token, and a copy
of this repo.

Issue one certificate per service FQDN. Sophon's Traefik configuration uses
per-router single-name certificates — no wildcards, no SAN lists — so the list
must be explicit:

```
traefik.<domain>
portainer.<domain>
dns.<domain>
ldap.<domain>
auth.<domain>
git.<domain>
kopia.<domain>
```

Add any service you have deployed beyond the defaults. A name missing here is a
name that will be serving an expired certificate next month.

Use whatever ACME client you prefer with the Cloudflare DNS-01 provider, and
produce a Traefik-format `acme.json` containing the account key and all
certificates. The simplest approach is to keep a long-lived Traefik instance on
the connected machine, configured with the same resolver, and copy its
`acme.json` — that way the format is guaranteed to match.

Place the result at:

```
artifacts/traefik/acme.json
```

Mode `0600`. This file contains the ACME account private key and every issued
certificate's private key. Treat it as a secret in transit — encrypt the
removable media.

## Step 2 — Carry it in

Copy `artifacts/traefik/acme.json` onto the Controller at the disconnected
site, into the same path in its checkout.

## Step 3 — Seed it into Traefik

```bash
ansible-playbook site.yml --tags traefik \
  -e domain_name=<domain> \
  -e infravm_ip=<infravm_ip> \
  -e portainer_admin_password=<password>
```

The `traefik` role's [restore_acme.yml](../../roles/traefik/tasks/restore_acme.yml)
writes the file into the `traefik_traefik_data` volume through Portainer's
Docker-API proxy and restarts Traefik.

## Step 4 — Verify

Check one certificate's expiry from the Controller:

```bash
echo | openssl s_client -connect <infravm_ip>:443 -servername auth.<domain> 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

`notAfter` should be roughly 90 days out. Repeat for at least one more name —
a partial refresh is the failure mode this check exists to catch.

## If certificates have already expired

Nothing is lost. Follow the same procedure; the site is briefly untrusted, not
broken. Browsers will refuse the services and Keycloak's LDAPS federation will
fail until Traefik restarts with the new file.

## Notes

- **Clock skew.** A disconnected InfraVM has no NTP source and will drift. Large
  skew makes a valid certificate look not-yet-valid or expired. Check the time
  on InfraVM while you are on site.
- **Do not re-issue repeatedly.** Let's Encrypt allows 5 duplicate certificates
  per week for an identical name set. Burning that quota locks issuance for
  seven days.
- **Back up `artifacts/traefik/`.** Losing the ACME account key means starting
  over with a new account.
