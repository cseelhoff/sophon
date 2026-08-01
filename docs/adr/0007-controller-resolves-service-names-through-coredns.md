# The Controller resolves service names through CoreDNS

CoreDNS is authoritative for `<domain>` and answers on `{{ infravm_ip }}:53`.
The Controller must use it. Deploy addresses services by FQDN over validated
TLS — `roles/keycloak/tasks/configure.yml` alone makes 27 calls to
`https://auth.<domain>` — so name resolution on the Controller is a hard
requirement, not a convenience.

Pointing the Controller's resolver at CoreDNS is a documented precondition.
Sophon cannot reconfigure the operator's machine, but it preflights resolution
immediately after the `coredns` role and fails with an actionable message.

## Why this is not already true

The zone is public and the Cloudflare tunnel CNAMEs are live, so a connected
Controller resolves `<domain>` via public DNS and hairpins out through
Cloudflare and back. It works by accident. Without Site egress it fails, and
the failure surfaces partway through Keycloak provisioning as a connection
error rather than as a resolution problem.

## Considered options

- **Write `/etc/hosts` on the Controller.** Requires `become` and mutates the
  operator's machine outside the workspace.
- **Address services by IP with `Host:` header and SNI overrides.** Fights TLS
  validation on every call, and the FQDNs are what the certificates are issued
  for.
- **Accept the hairpin.** Makes air-gapped Deploy fail in a confusing place,
  contradicting ADR-0006.

## Consequences

- Ordering already permits the preflight: `coredns` runs before `traefik`,
  `keycloak`, and `gitea`.
- The preflight should assert that a known FQDN resolves to `infravm_ip`, not
  merely that it resolves. Resolving via the tunnel is the failure this is
  meant to catch.
