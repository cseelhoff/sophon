# M3: SFTP onboarding and deployment diagnostics

## Delivered boundary

Configure generates/reuses an ed25519 key under project `credentials/kopia`,
prints its public `authorized_keys` line, scans supported RSA/ECDSA/Ed25519 host
keys and asks for independently verified SHA256 fingerprints. Directory/files
are 0700/0600. The private key and endpoint pins are outside Git, reusable media
and primary backup mounts. Imported key/pin environment overrides remain available.
Saved trust cannot be replaced by an override; changed key sets fail closed.

Before any stack deployment, the console authenticates to SFTP, tests exclusive
write/read/delete, queries space when supported, and checks Kopia. Only its exact
missing-repository diagnostic, an unused path and explicit operator approval
permit creation. `SOPHON_KOPIA_CREATE_REPOSITORY=true` confirms that case for
automation; it never reinitializes existing storage and needs no reset step.
The playbook rejects direct Configure invocations that skipped console preflight.
Runtime itself still only connects.

Prepare carries the SSH clients, pinned Paramiko and Kopia binary in the runner.
The role reads only structured redacted runtime diagnostics when startup/health
fails, and explicitly fails with that reason; it never prints raw container logs
or the Portainer token. The operator procedure and recovery implications are in
[inputs](inputs.md#kopia-backup-inputs) and the [role guide](../roles/kopia/README.md).

## Commands actually run

### Standard regression workflow

```sh
bash sophon-provision.sh test
```

Final output:

```text
Ran 151 tests in 86.021s
OK (skipped=3)
Deployment step checks passed
```

Coverage includes generated identity/trust persistence, confirmation refusal,
changed host keys despite overrides, exact missing-repository classification,
ambiguous/wrong-password failures, a path changing during confirmation, unsupported
space reporting versus actual permission/authentication failures, RSA-4096
overrides exceeding the old 4096-character input ceiling, console/deployment
ordering, cancellation/EOF without stack dispatch, and explicit refusal of
Configure without preflight.

The role HTTP fixture supplies Docker-framed structured errors containing
synthetic secrets. Its failing deployment exposes `invalid repository password`
but not the password, decoded private material or manager token. Healthy services
with historical restarts still pass. Existing certificate-flow fixtures retain
all their assertions and substitute only the independently tested SFTP seam.
Deployment-step checks still require generated SSH material at the playbook
boundary; it is no longer incorrectly classified as an operator-required input.

### Actual Prepare and offline media

```sh
docker build -t sophon-tools -f Containerfile .
bash sophon-provision.sh prepare
```

The initial Prepare rebuilt/saved the runner and collector:

```text
localhost : ok=19 changed=3 unreachable=0 failed=0 skipped=6 rescued=0 ignored=0
```

Final cached-archive Prepare:

```text
localhost : ok=17 changed=1 unreachable=0 failed=0 skipped=8 rescued=0 ignored=0
```

Checked extraction inside the tools image used a fresh temporary directory:

```sh
xorriso -osirrox on -indev artifacts/bootstrap/sophon-deployment.iso \
  -extract /project/src/backup_destination.py "$temp/backup_destination.py" \
  -extract /project/src/console.py "$temp/console.py" \
  -extract /project/4-configure.yml "$temp/4-configure.yml" \
  -extract /project/roles/kopia/tasks/main.yml "$temp/kopia.yml" \
  -extract /project/bootstrap/sophon-version "$temp/version"
cmp src/backup_destination.py "$temp/backup_destination.py"
cmp src/console.py "$temp/console.py"
cmp 4-configure.yml "$temp/4-configure.yml"
cmp roles/kopia/tasks/main.yml "$temp/kopia.yml"
cat "$temp/version"
```

```text
source-sha256:22bce2346303a34a82ce386db6cea0db89ec1703b83da88a7a766031d5a3fb58
Offline M3 pairing/console/guard/diagnostic-role byte comparisons: PASS
```

The prepared runner was checked separately:

```sh
docker run --rm --entrypoint python3 sophon:latest -c \
  'import paramiko,subprocess;assert paramiko.__version__=="4.0.0";print("Prepared runner Paramiko:",paramiko.__version__);print(subprocess.check_output(["kopia","--version"],text=True).strip());subprocess.run(["ssh-keyscan","-h"],capture_output=True,check=False);print("Prepared runner SFTP pairing dependencies: PASS")'
```

```text
Prepared runner Paramiko: 4.0.0
0.23.1 build: 72ec08fd8edb86c67ed27099bf1b955e1f308ffa from: kopia/kopia
Prepared runner SFTP pairing dependencies: PASS
```

Bundle tests include synthetic private files at the actual
`credentials/kopia` paths and assert their contents never enter media.

### Real dev console, SFTP and Kopia

Only VM 107 (`10.0.60.66`) was used. Production VMs/repositories were not accessed.
The [live check](../tests/spikes/kopia_m3.py) runs the actual console/preflight and
native Kopia against disposable SFTP paths on dev loopback. Only stack dispatch
is a recorder, so no application stacks, tunnels, runners or certificate issuance
start. It temporarily authorizes its generated key for SFTP only and removes that
exact line afterward, preserving existing authorized keys.

After transferring the current source/Containerfile, the dev commands were:

```sh
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'podman --cgroup-manager=cgroupfs build --target runner \
     -f ~/m3-source/Containerfile -t localhost/sophon:m3-dev ~/m3-source'
ssh -oBatchMode=yes -oStrictHostKeyChecking=yes \
  -oUserKnownHostsFile="$DEV_KNOWN_HOSTS" -i "$DEV_KEY" dev@10.0.60.66 \
  'python3 ~/m3-source/kopia_m3.py --dev-only --source ~/m3-source \
     --image localhost/sophon:m3-dev --sftp-root /srv/sophon-m0/sftp \
     --output ~/m3-results.json'
```

Observed runner build:

```text
Successfully tagged localhost/sophon:m3-dev
849bd58fbe8af6b587ab7d02ffc5e6b3046a44da6ce9410cf53d8a97e8774aca
```

Final live output:

```text
project_key_permissions_and_independent_fingerprints: true
missing_repository_requires_confirmation: true
confirmed_creation_reused_key_and_existing_repository: true
authenticated_write_read_delete_and_free_space: true
wrong_password_with_creation_approval_never_reinitialized: true
explicit_unattended_creation_of_an_empty_destination: true
nonempty_destination_never_initialized: true
changed_saved_host_key_rejected_even_with_override: true
no_application_stacks_deployed: true
production_contacted: false
temporary_authorized_key_removed: true
project_credentials_and_repositories_removed: true
```

The actual runtime CLI emitted this redacted message in its structured error:

```text
Kopia repository connect failed: failed to open repository: unable to create format manager: invalid repository password
error connecting to repository: unable to create format manager: invalid repository password
```

Native tests compare fingerprints against server public-key files read through
the independently authenticated dev channel, not against another unverified
scan. The changed-key case alters only disposable saved trust, then provides the
correct override and confirms it cannot bypass that saved trust. It does not
rotate sshd's real keys. Repository format bytes remain unchanged on reruns and
wrong-password attempts, including when creation approval is exported.

## Earlier failures and limits

Network suspension interrupted archive transfer and then SSH/browser connectivity;
the user restored access. The dev runner was built from the same pinned
Containerfile rather than claiming the incomplete archive loaded. The prepared
local runner/media were checked independently above.

Rapid consecutive scans triggered OpenSSH's unauthenticated-connection penalty.
The drill spaces scenarios six seconds apart; server security settings were not
weakened. Scanning now requests only RSA/ECDSA/Ed25519 client-supported types,
avoiding unsupported security-key probes. The initial test also assumed a literal
format filename; it now finds exactly one native `kopia.repository*` format file,
including the backend suffix, and compares its bytes across operations.

Not verified: a full live Portainer Configure rollout, production migration,
Fedora CoreOS/SELinux enforcement, every SFTP implementation, actual server-side
host-key rotation, non-22 live servers, disk-full live failures or an unsupported
statvfs extension on a real server. Non-22 pin syntax and unsupported-space
handling are exercised in focused tests, not claimed as live-server results.

Review against the M2 branch point covered standards and the M3 specification.
M4's optional weekly coarse layer remains a separate gated milestone.
