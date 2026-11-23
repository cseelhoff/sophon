# Certificates

Traefik uses **one wildcard certificate** for every service: it names the domain and `*.<domain>` (so `traefik`,
`arcane`, `dns`, `kopia`, `ldap`, `auth` and `git` are all covered) and uses an ECDSA P-256 key, from Let's Encrypt through
the Cloudflare DNS-01 challenge. Every router that requests a certificate carries the wildcard in its own labels, so
Traefik never asks for per-service certificates. Its state is a single file, `acme.json`; the other services
(LDAPS in particular) read their certificate out of it, so one small file is all that has to move.

At a site **without internet** nothing can issue or renew, so you carry that certificate in.

**Why not just the ACME account key?** The account key only signs requests to Let's Encrypt. The certificate itself
is signed by the CA (RFC 8555: the client sends a certificate request, the CA issues the certificate), so a site with
no route to Let's Encrypt cannot create certificates from the key alone.

**Wildcard trade-off.** A stolen key impersonates every name under the domain instead of one. Here every service
runs on the same InfraVM and shares one `acme.json` anyway, so the VM is already the trust boundary. A wildcard also
keeps the individual service names out of public Certificate Transparency logs.

## Make the secret certificate ISO (connected machine)

```sh
sh sophon-build-isos.sh           # or: sh sophon-build-isos.sh certificates issue --domain example.com
```

Prompts (on stderr) for the domain, an ACME email and a Cloudflare token (hidden), then issues the certificate for
`example.com` and `*.example.com` with a throwaway Traefik (the one baked into the tools image). It keeps its ACME
account and certificate in the `sophon-state` Docker volume (`/state/certificates/<domain>/`), so running it again
**reuses** a certificate with more than 65 days left and automatically renews when 65 days or fewer remain (pass
`--renew-certs` to `sophon-build-isos.sh` or `--force` to `certificates issue` to force an early 90-day renewal).

The result is `artifacts/secrets/sophon-secrets.iso` (mode 0600, volume label `SOPHON_CERTS`), a small disk
holding `acme.json` with the wildcard certificate, its **full chain** and its private key (preserving any existing
`/sophon-seed.json` if you also use unattended seeding), and no ACME account or token. Because the whole file travels,
nothing has to be pasted and nothing has to be rebuilt from the deployment ISO, whatever chain Let's Encrypt currently uses.

Options: `--email`, `--timeout`, `--output-iso PATH` (another location) and `--output-file PATH` (also write the bare
`acme.json`). Domain, email and token can come from `SOPHON_DOMAIN`, `SOPHON_ACME_EMAIL` and `SOPHON_CF_TOKEN` in `.env`
or your environment. An older store in the volume that holds
per-service certificates is not reused: a new wildcard certificate is issued.

## Attach and import it (InfraVM, step 4)

Attach the ISO to the InfraVM as a CD/DVD: on this PC's Hyper-V `.\sophon-provision.ps1 attach-certificates` (a VM
created while the ISO exists already has it), elsewhere through your hypervisor.

`sophon-deploy` looks for a disk labelled `SOPHON_CERTS`. If it finds one it mounts it read-only, copies
`acme.json` into the project's `imports` folder (mode 0600) and unmounts it. Answer **n** to "Does this site have internet access?" and press
Enter at the **Pre-issued certificates file** prompt to accept that file (you can also type another path under
`/workspace`). Sophon checks that the domain and every service name are covered by a trusted, unexpired chain whose
key matches, then stores the file privately (mode 0600, replaced atomically), removes the copy under `imports`, and
Traefik is seeded from it. A malformed, account-only, per-service or wrong-domain file is rejected before anything
is replaced. Afterwards eject the disk (`.\sophon-provision.ps1 detach-certificates`) and delete the ISO from the
laptop.

## Renew

Repeat monthly (certificates last 90 days, and `sophon-build-isos.sh` automatically renews once ≤65 days remain):

1. Run `sh sophon-build-isos.sh` (`.\sophon-build-isos.ps1`) on the connected workstation.
2. Run `sh sophon-mount-secrets.sh` (`.\sophon-mount-secrets.ps1`) to hot-attach the renewed `sophon-secrets.iso` to
   `sophon-infravm` (and delete the local workstation copy unless `--keep-local` is passed).
3. Open `https://<domain>` and click **Update Certificates** (or run `sophon-refresh-certs` as `core` on the VM console
   if the previous certificate already expired). This validates the new `acme.json`, updates Traefik, reloads
   `certs-dumper` and OpenLDAP, triggers a Kopia backup snapshot, and ejects the certificate ISO automatically.

An InfraVM that was deployed with the older per-service certificates keeps them on a normal rerun. Connected sites
will request the wildcard on their own; air-gapped sites can use **Update Certificates** (or `sophon-refresh-certs`).

## Handle with care

- The certificate ISO contains a private key. Treat it like a password and delete it once the site has imported it.
  The deployment ISOs never contain certificates or the Cloudflare token.
- Back up the `sophon-state` volume (`docker run --rm -v sophon-state:/state -v "$(pwd):/workspace" sophon-tools tar -czf sophon-state.tgz -C /state .`); losing the ACME account key means registering a new account.
- Let's Encrypt allows 5 duplicate certificates per week; do not issue repeatedly.
- A disconnected InfraVM needs an accurate clock, or valid certificates look expired.
