# Kopia redesign: M0 spike (2026-11-13)

**Stop here for operator review. M1 has not started.** No production backup
implementation, role, label, manifest or restore code is changed by this branch.

## Result and proposed direction

The four primitives are feasible in the measured dev environment. There is one
important capability trade-off:

* `DAC_READ_SEARCH + CHOWN + FOWNER` reads other sub-UIDs and clones their files,
  but GNU `cp --archive` silently loses the tested user xattr, despite exit 0.
* `DAC_OVERRIDE + CHOWN + FOWNER` preserves the tested contents, ownership, modes,
  hardlinks, symlinks and user xattr. Removing either `CHOWN` or `FOWNER` fails.

Propose the latter, **the smallest successful set tested for GNU `cp --archive`**,
with UID 0 inside the rootless user namespace, all other capabilities dropped,
read-only input mounts, read-only container rootfs and `no-new-privileges`.
`DAC_OVERRIDE` does not bypass a read-only mount: the write control returned
`EROFS` (errno 30) with this set.

| Capability | Reason demonstrated by the spike |
|---|---|
| `DAC_OVERRIDE` | Traverse/read private files owned by other mapped UIDs and retain user xattrs when copying their metadata after ownership changes. The narrower read/search alternative lost the xattr. |
| `CHOWN` | Preserve numeric UID/GID in staging. Its removal produced ownership errors. |
| `FOWNER` | Preserve permissions/metadata after changing ownership. Its removal produced permission-preservation errors. |

This is not a claim that three capabilities are the theoretical minimum for
every possible copy algorithm. An alternative would be an ownership-last
custom copier with fewer capabilities; that alternative is **not tested or
implemented**. Approval of the measured approach, or a request for another
spike, is needed before M1.

## Isolation, legacy baseline and access

* Local `kopia-legacy` points to `21d5bbe647466e0d6f32d207819e51496afc8d77`.
  The tag has not been pushed.
* The preceding uncommitted onboarding changes are preserved in the named stash
  `Sophon SFTP onboarding before Kopia redesign`. They are not part of this branch.
* New disposable Proxmox VM 107, `sophon-m0-dev`, was created from a checksummed
  Debian cloud image, not cloned from a Sophon VM. It has 4 vCPUs, 4 GiB RAM, a
  12 GiB OS disk and a separate, initially blank 16 GiB test disk.
* Only that new test disk was formatted as XFS, with `reflink=1`.
  Rootless Podman's graph root is `/srv/sophon-m0/containers`.
* The SFTP fixture uses a newly generated, SFTP-only SSH key and newly created
  temporary repository paths under `/srv/sophon-m0/sftp` on the dev VM.
* A one-day, privilege-separated API token is stored locally with mode `0600`.
  Its grants are VM 107, the new `sophon-m0` storage, node audit, and `SDN.Use`
  on the dev VM's selected bridge/VLAN. API TLS is pinned before sending auth.
  The production VM and production disk-storage permission queries returned:

  ```text
  Production VM permissions: {'/vms/101': {}}
  Production storage permissions: {'/storage/vm-storage': {}}
  ```

* No production VM or production repository connection was made. No Sophon
  Configure, Cloudflare tunnel, certificate issuance or Gitea runner was started.
  The prototype starts Podman's compatibility service as a foreground child
  only for the test; it creates no backup host unit.

The API credential is not in this report, Git, process arguments or logs.

## Measured environment

```text
Rootless Podman 5.4.2
kernel 6.12.116+deb13-cloud-amd64
Backing Filesystem: xfs
UID map:
         0       1000          1
         1     100000      65536
```

The fixture has a private directory owned by `999:998`, mode `0700`, containing
a 64 MiB random file owned by `1001:1002`, mode `0400`, a hardlink, a symlink and
the user xattr `user.sophon-m0=preserved`. These are mapped sub-UIDs/GIDs, not
the host login user's identity.

Images used, with observed repository digests (first `RepoDigests` entry):

| Image tag | Digest |
|---|---|
| `docker.io/library/python:3.14.10-slim-trixie` | `sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f` |
| `docker.io/library/haproxy:3.2.6-alpine` | `sha256:3a819392f5a6af2a9203d1c63ff0feba74f0c940623a558511be06d7d85e361c` |
| `docker.io/kopia/kopia:0.23.1` | `sha256:83d8bc7bce313dd8b2c17771b5f3f804e18e79ad7d7cc6bc994ff5872374794d` |

The full `RepoDigests` values are recorded in the JSON evidence. A subsequent
implementation should pin the selected proxy digest in Prepare; this spike
does not change the offline bundle.

## Reproduce the spike

Use only a disposable dev host, with rootless Podman, XFS-backed named volumes,
the three images already present, and a dedicated local SFTP account/path.
Copy [the harness](../tests/spikes/kopia_m0.py) and
[its proxy configuration](../tests/spikes/kopia-m0-proxy.cfg) together.
The SSH key's `.pub` file must exist, and the known-hosts file must contain the
independently obtained host keys under `host.containers.internal`.

The command executed on the dev VM was:

```sh
python3 ~/kopia_m0.py --dev-only \
  --sftp-root /srv/sophon-m0/sftp \
  --key "$HOME/.ssh/m0-sftp" \
  --known-hosts "$HOME/.ssh/m0-sftp-known-hosts" \
  --output "$HOME/m0-results.json" > "$HOME/m0-run.stdout"
```

The complete JSON output was retrieved separately as `kopia-m0-results.json`.
The following are excerpts from that measured run, not estimated performance.
Every created container and volume has a unique `sophon-m0-` prefix; the harness
removes only its own recorded names and records cleanup failures.

### Read-only volume reflink and sub-UID access

The harness runs this command inside each capability variant:

```sh
cp --archive --reflink=always /input/. /staging/
```

Its source is a named volume mounted `/input:ro`, and staging is a different
named volume mounted read/write on the same XFS filesystem. Containers use
`--user 0 --cap-drop ALL --read-only --network none`, with only the listed
capabilities added, `label=disable` and `no-new-privileges`.

```text
CAPS                           cp exit  Result
none                           1        Permission denied; ownership failed
DAC_READ_SEARCH                1        Ownership preservation failed
DAC_READ_SEARCH,CHOWN          1        Permission preservation failed
DAC_READ_SEARCH,CHOWN,FOWNER   0        Xattr missing (errno 61); integrity check failed
DAC_OVERRIDE,CHOWN             1        Permission preservation failed
DAC_OVERRIDE,FOWNER            1        Ownership preservation failed
DAC_OVERRIDE,CHOWN,FOWNER      0        All fixture checks passed
```

Successful measured copy: **0.002222780 s** for the 64 MiB fixture.
This is copy-command time, excluding container startup and subsequent hashing;
it is not a whole-workload pause or upload benchmark.

```text
source_write_errno=30
filesystem=["xfs","xfs"]
CapEff=000000000000000b
uid=1001 gid=1002 mode=0o400
hardlink=true symlink=state xattr=preserved
sha256 matched the source fixture
```

Because `--reflink=always` must fail when cloning cannot be performed, its
successful exit is the reflink control, not an inference from elapsed time.
Plain-copy fallback, cross-filesystem copying, filesystem snapshots and the
weekly coarse backup are not tested here.

### Socket-proxy exec stdout streaming

The pinned HAProxy image forwards to the dev user's Unix Podman socket.
The configuration permits only the named list, inspect, exec, stop and start
routes/methods, including `/v1.41` compatibility prefixes. The socket is not
mounted into the client or fixture workload.

The harness creates an exec through:

```text
POST /v1.41/containers/<fixture-id>/exec
POST /v1.41/exec/<exec-id>/start   {"Detach":false,"Tty":false}
GET  /v1.41/exec/<exec-id>/json
```

The command emits 16 one-MiB chunks separated by 100 ms sleeps and a separate
stderr control. The client incrementally decodes Docker's multiplex framing:

```text
HTTP=200
stdout bytes=16777216
first payload=0.028041063 s
complete stream=1.714925171 s
stdout SHA256=a06c26cbac8b80704f420222dae5658b88ff2da96702d12ef7a4223e9361f7c1
stderr="m0-stderr-control\n"
exec exit=0
```

The first payload precedes completion by more than 1.6 seconds, so the proxy
did not buffer the complete dump. This is a byte-stream fixture, not a measured
`pg_dumpall` or `slapcat` run.

Stop/start through the same proxy also ran:

```text
POST .../containers/<fixture-id>/stop?t=10 -> 204
stop elapsed=0.033074823 s, inspected exit=0
POST .../containers/<fixture-id>/start -> 204
inspected Running=true
```

The following negative controls each returned **403**:

```text
GET    /v1.41/images/json
GET    /v1.41/volumes
GET    /v1.41/containers/<fixture-id>/archive?path=/data
POST   /v1.41/containers/create
POST   /v1.41/containers/<fixture-id>/kill
DELETE /v1.41/containers/<fixture-id>
```

The prototype allows inspect responses because the target requires inspect;
this is not a promise that an inspect response cannot contain environment
variables. M1 must deliberately select manifest fields rather than save inspect
records. The proxy also cannot make arbitrary exec commands harmless; it
removes unneeded engine endpoints, not all authority of the permitted methods.

### Kopia 0.23.1 failure classification

Each case uses a fresh read-only-rootfs Kopia container with writable tmpfs
configuration/cache and only the dev SFTP key/pins mounted read-only. Passwords
are generated for the fixture and passed by environment name, not argv.

The actual commands are `kopia repository connect sftp ...` for missing,
existing, wrong-password and wrong-host-key cases, and the explicit
`kopia repository create sftp ...` for the newly created fixture repository.
Complete argument vectors are in the JSON evidence.

```text
Missing repository: exit 1
error connecting to repository: repository not initialized in the provided storage

Explicit dev repository creation: exit 0
Connected to repository.

Existing repository: exit 0
Connected to repository.

Wrong password: exit 1
failed to open repository: unable to create format manager: invalid repository password
error connecting to repository: unable to create format manager: invalid repository password

Wrong host key: exit 1
... ssh: handshake failed: knownhosts: key mismatch
```

Exit code **alone cannot distinguish these failures**. M3 should require the
specific missing-repository diagnostic plus explicit operator approval, not a
general connect-on-error fallback. The missing-repository probe itself created
only `.shards` in the empty destination; a probe is therefore not guaranteed
to leave the directory literally empty. No production repository was used.

## Validation and limitations

Commands executed:

```sh
python3 -m py_compile tests/spikes/kopia_m0.py
git diff --check
bash sophon-provision.sh test
```

The first two exited 0 with no output. The repository workflow reported:

```text
Ran 127 tests in 49.180s
FAILED (failures=1, skipped=3)
test_cached_prepare_never_invokes_network_tools:
'kopia_image' is undefined
```

That cached-Prepare fixture failure was previously reproduced on unchanged HEAD;
M0 changes no existing production/test logic and does not weaken or remove it.
The normal wrapper short-circuits after the unittest failure, so this invocation
did not run its subsequent deployment/step scripts.

Those checks were subsequently run separately:

```sh
docker run --rm -v "$PWD:/workspace" --entrypoint sh sophon-tools \
  -c 'python3 tests/test_deployment.py && python3 tests/test_steps.py'
```

```text
Deployment step checks passed
```

The command exited 0; the deployment script itself emits no success text.

The spike's final integrity/cleanup check returned:

```text
M0 evidence integrity and cleanup assertions: PASS
CLEANUP 10 operations; failures 0
```

The commands below produced no entries after the run:

```sh
podman --cgroup-manager=cgroupfs ps -a --filter name=sophon-m0- --format '{{.Names}}'
podman --cgroup-manager=cgroupfs volume ls --filter name=sophon-m0- --format '{{.Name}}'
```

The dev VM and its two dedicated disks/storage remain available for the next
approved milestone; the temporary SFTP repositories, proxy, compatibility-service
child, fixture containers and named volumes were removed.

Initial harness attempts failed on CLI flag placement and the stock Kopia
image's read-only `/app/cache`; the final run places leaf flags correctly and
sets all ephemeral Kopia paths explicitly. An initial Python PID-1 fixture
needed a SIGTERM handler to avoid forced shutdown; the recorded stop result is
the corrected, inspected exit-0 case.

Not tested: Fedora CoreOS/SELinux enforcement, other kernels or filesystems,
POSIX ACL/security-xattr preservation, real database exports/restores,
crash-recovery journals, application dependency ordering, multi-workload
consistency, the v3 collector/manifest, restore drill, full deployment or M4.
None of those is claimed complete by M0.
