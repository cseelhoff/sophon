# Runbook: sneakernet certificate refresh

**When:** before the first service deployment, then monthly, at a site with no internet egress.

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
```

Include `kopia.<domain>` only when the optional backup service is deployed.
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

Validate the six default service names, their private-key matches, expiry,
hostname coverage, and trusted certificate chains, then create separate media:

```bash
nix develop
python3 certificates.py --source artifacts/traefik/acme.json \
  --domain example.com --output /private/sophon-certificates.iso
```

The output is mode `0600`, labeled `SOPHON_CERTS`, and contains only `acme.json`
and its import script. **It contains private keys.** Protect its directory and
datastore permissions. Do not add it to the reusable `SOPHON_DEPLOY` bundle.
It contains no Cloudflare token.

## Step 2 — Carry it in

After installing InfraVM, attach and connect this private ISO using the hypervisor
console. If reusing an installation CD drive on VMware, first eject its unmounted
old media inside the guest with `sudo eject /dev/sr0` (confirm the actual device
with `lsblk`). This avoids VMware's locked-CD confirmation pausing the VM.
In the installed `core` console:

```bash
sudo mkdir -p /mnt/sophon-certificates
sudo mount -o ro /dev/disk/by-label/SOPHON_CERTS /mnt/sophon-certificates
sudo bash /mnt/sophon-certificates/import.sh
sudo umount /mnt/sophon-certificates
# Use the certificate CD's actual device.
sudo eject /dev/sr0
```

Eject the certificate ISO and remove its temporary hypervisor datastore copy
after import. Keep the connected machine's protected ACME store for renewal.
The importer atomically places mode-`0600`, `core`-owned identity state at
`/var/lib/sophon/project/artifacts/traefik/acme.json`.

## Step 3 — Seed it into Traefik

On the first deployment, run `sophon-controller` normally and leave the
Cloudflare token empty. Certificates must be imported **before** this run:
OpenLDAP and Keycloak require working LDAPS during deployment.

For a later refresh, supply the existing service credentials and run:

```bash
sophon-controller --refresh-certificates
# Or, with the same service settings exported:
sophon-controller --run --refresh-certificates
```

Do not use tag-filtered `site.yml` runs; the installed runner requires the full
service playbook. Certificate refresh uses the console wizard or `--run`, not
`--dashboard`. The local handoff is validated before certificate replacement.
The `traefik` role's [restore_acme.yml](../../roles/traefik/tasks/restore_acme.yml)
loads its helper image from the prestaged archive, never from a registry, and
seeds a fresh `traefik_traefik_data` volume through Portainer's Docker-API proxy.
Normal reruns preserve an existing live certificate store instead of restoring
a possibly stale snapshot. An explicit refresh stops Traefik before replacing
that store; the normal stack deployment starts it again.

## Step 4 — Verify

Check the chain, hostname, and expiry from a client that trusts public CAs:

```bash
echo | openssl s_client -connect <infravm_ip>:443 -servername auth.<domain> \
  -verify_hostname auth.<domain> -verify_return_error \
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
