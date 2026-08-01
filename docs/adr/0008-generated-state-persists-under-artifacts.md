# Generated state persists under artifacts/, and `-e` always wins

Secrets and allocated addresses that Sophon generates are written to
`artifacts/` on first run and read back on every run after that. Any value
supplied on the command line with `-e` takes precedence and is never written to
disk, so operators using an external secret store are unaffected.

This makes Deploy re-runnable, which ADR-0004 already requires: the monthly
Sneakernet refresh is a tagged re-run, and it cannot work if every run
regenerates the credentials the running services were configured with.

## Mechanism

`lookup('password', ...)` already does this. `site.yml` passes `/dev/null`,
which is the lookup's documented "generate but do not save" path; passing a
real path under `artifacts/secrets/` makes it generate once and read
thereafter. The `-e` precedence already holds — extra-vars override the
`set_fact` block at the top of `site.yml`.

Allocated addresses persist the same way. `nfs_ip` and `infravm_ip` are
currently re-derived by arp-scan on every run, and a powered-off VM's address
looks free. Persisting them makes arp-scan first-run discovery rather than a
per-run lottery.

## Considered options

- **Ansible Vault committed to the repo.** A second secret-handling mechanism
  alongside `artifacts/`, and it needs its own key distributed out-of-band.
- **Sophon generates nothing.** Pushes every credential onto the operator for
  what is meant to be a one-command deployment.
- **Stay single-shot.** Would require ADR-0004 to say the operator must retain
  a complete `-e` list out-of-band, indefinitely, to renew certificates.

## Consequences

- `artifacts/` is unambiguously secret-bearing. It already holds the Kopia
  private key and `acme.json`; this makes that the rule rather than an
  accident. It stays gitignored and should be a Kopia backup source.
- Losing `artifacts/` means losing access to a running deployment.
- Deleting a persisted secret file is how you rotate — the next run regenerates
  it, and the affected service must be reconfigured or redeployed.
