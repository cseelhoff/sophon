# Certificates

Traefik gets one Let's Encrypt certificate per service name through the Cloudflare DNS-01
challenge, and renews by itself while it has internet. Its state is a single file, `acme.json`:
the ACME account key plus every issued certificate chain and private key. The other services
(LDAPS in particular) read their certificates out of it, so one file is all that has to move.

At a site **without internet** nothing can issue or renew, so you carry that one file in.

## Make the payload (connected machine)

```bash
docker run --rm --init -it -v "$(pwd):/workspace" -v sophon-state:/state sophon-tools certificates issue --domain example.com --output payload.txt
```

Prompts (on stderr) for the domain, an ACME email and a Cloudflare token (hidden), then issues
`traefik`, `portainer`, `dns`, `ldap`, `auth` and `git` under the domain with a throwaway Traefik
(the one baked into the tools image). It keeps its ACME account and certificates in the `sophon-state`
Docker volume (`/state/certificates/<domain>/`), so running it again **reuses** anything with more
than 30 days left and only asks for the token when it must issue. The payload is **one line**: the base64
of the complete `acme.json`, written to `--output` (mode 0600). Without `--output` it goes to stdout, but
PowerShell's `>` may re-encode it, so prefer `--output`.

Options: `--email`, `--timeout`; the token can come from `-e CF_DNS_API_TOKEN` (passed by name from your
environment). It is never part of the payload.

## Import it (InfraVM, step 4)

Paste the payload into the optional **Pre-issued certificates** prompt of `sophon-controller`
(`@/workspace/path` reads a file instead), and leave the Cloudflare
token empty. Sophon checks that all six names are covered by trusted, unexpired chains whose keys
match, then stores the file privately (mode 0600, replaced atomically) and Traefik is seeded from it.
A malformed, account-only or wrong-domain payload is rejected before anything is replaced.

## Renew

Repeat the two steps about monthly (certificates last 90 days). Run `sophon-controller
--refresh-certificates`, paste the new payload, and supply the existing service credentials:
a refresh currently reruns the full service deployment. A normal rerun keeps the live store.
If certificates already expired, the same procedure recovers the site.

## Handle with care

- The payload contains private keys. Base64 is not encryption: treat it, the saved file and your
  clipboard like a password. Neither ISO ever contains certificates or the Cloudflare token.
- Back up the `sophon-state` volume (`docker run --rm -v sophon-state:/state -v "$(pwd):/workspace" sophon-tools tar -czf sophon-state.tgz -C /state .`); losing the ACME account key means starting over.
- Let's Encrypt allows 5 duplicate certificates per week; do not issue repeatedly.
- A disconnected InfraVM needs an accurate clock, or valid certificates look expired.
