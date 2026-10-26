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

One base64 payload containing the complete `acme.json`: ACME account identity,
issued certificate chains, and their private keys. The account key alone cannot
issue certificates offline. **The Cloudflare API token must not go to the disconnected
site** — it can rewrite your public DNS, and it has no purpose there because
nothing at the site can reach Cloudflare anyway.

## Preferred workflow: one command, then one import

### 1. On the connected machine

From this repository's Python environment (`nix develop` supplies it), with
Docker, Podman, or native Traefik 3.x available:

```bash
python3 certificates.py issue
```

The command prompts for a domain, contact email, and hidden Cloudflare DNS API
token as needed. Scope the token to Zone / Zone / Read and Zone / DNS / Edit.
It issues the six default service names below and reuses its private per-domain
ACME account/store under `~/.local/state/sophon/certificates/`. Existing
certificates with more than 30 days remaining are exported without reissuance
or a token prompt. Do not run two issuers against the same store concurrently.

Only the base64 payload goes to stdout; prompts and progress go to stderr.
Use `umask 077; python3 certificates.py issue --domain example.com > payload.txt`
to avoid retaining the payload in terminal scrollback. Optional `--email`,
`--state-dir`, `--engine docker|podman|traefik`, and `--timeout` arguments support
automation. The token may come from `CF_DNS_API_TOKEN`; never pass it literally
in command arguments. Issuer logs and identity state are private.

### 2. On installed InfraVM

For first deployment, run `sophon-controller` and paste the string into the
optional **Pre-issued certificates** prompt in Step 4. The prompt is hidden and
supports large input; `@/workspace/path/to/payload.txt` imports a file instead.
The optional dashboard has a textarea for the same input. Some hypervisor
consoles impose their own clipboard limits; use the file or dashboard alternative
if needed. No certificate ISO, mount, or separate importer command is required.

Leave the Cloudflare token empty on the disconnected machine. The payload is
validated before private atomic storage, is excluded from Ansible's environment
and copied commands, and never enters either reusable installation ISO.
Traefik uses the store and the existing dumper extracts LDAP's PEM files.
Base64 is not encryption: protect the payload and clipboard as private keys.

For a later refresh, repeat the connected command, then run
`sophon-controller --refresh-certificates` and paste the new payload. This
currently runs the full service playbook and requires the existing service
credentials. It is not a password rotation. If the store is already imported
and all existing settings exported, `sophon-controller --run --refresh-certificates`
is the noninteractive alternative. Refresh is console-only, not `--dashboard`.

Normal reruns preserve the live store. Use explicit refresh to replace it.
Verify HTTPS, LDAPS and SSO afterward; retain the protected connected store for
future renewal and retire unprotected exported copies.

## Legacy/manual certificate ISO transport

The following remains supported for sites whose transfer procedure requires
optical media. It is not required for the paste workflow above.

### Step 1 — On the connected machine

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

### Step 2 — Carry it in

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

### Step 3 — Seed it into Traefik

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

## Verify

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
