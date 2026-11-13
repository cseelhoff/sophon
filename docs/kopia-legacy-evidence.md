# Historical Kopia evidence (legacy design)

Archived verbatim from the old role README. These observations and commands refer to
`kopia-legacy`, not the current v3 collector. They are not current operator instructions
or claims of v3 recovery coverage. Never run these historical procedures against production.

## Verified installation (2026-11-01)

Snapshot `c14508652bf9581d3c630b347e429482` contains 740,857,363 bytes
and no failed files. Its isolated restore passed validation for all 36
artifacts; all 43 attached persistent volumes had coverage or explicit exclusions.
Native isolated PostgreSQL 18 clusters, MongoDB/oplog, LDAP and Portainer
restores were exercised. Restored n8n, Appsmith and Nexus accepted their original
accounts; Arcane passed SQLite integrity/owner checks and service startup.
AdGuard reached readiness with recovered configuration/work data and preserved
ownership. Its isolated clone needed different bind addresses, not production
DNS changes. Nextcloud's database and complete file artifacts were verified;
a complete restored Nextcloud/SSO application cutover was not tested.

VM backups are enabled with a native 21,600-second interval, essential
prepare/cleanup actions and stable `backup@sophon` identity.
The x870 controller's six-hour systemd timer is enabled. Its recovery snapshot
`00837017f172b887ed425a32c184b263` was isolated-restored, and all six files
(SSH key, SFTP unit, backup script, protected environment, controller unit/timer)
matched the originals byte for byte. Its other sources protect SFTP bootstrap,
client/host keys and controller ACME material.
Manual Kopia deployment run 8 passed after scheduling was enabled, preserving
all stack secrets and the native interval across redeployment. Native quick
maintenance completed successfully against the actual backed-up SFTP repository.

**Remaining host action:** x870's runtime firewall opening works, but the
persistent NixOS rule has not been saved. Allow only InfraVM `10.1.1.3` to
`10.1.1.129:2222` on `enp8s0`. A reboot/firewall restart can otherwise interrupt
VM backups. Keep the encryption password independently recoverable.

## Verified split-label rollout (2026-11-01)

All 33 live participants now use v2 split labels; none retains
`sophon.backup.spec`. Their decoded recipes exactly match the original v1
recipes. All 43 attached persistent volumes remain covered or explicitly
excluded, and protected mounts, deployment secrets, commands and original
running/stopped states were verified through container recreation.
Core templates, host-bootstrap and authoritative Gitea Compose sources were
updated. Strict v1 parsing remains available for old snapshot metadata.

Encrypted snapshot `f5223eea8f545efffd12af956efe321a` reports 769,724,822 bytes
and zero failed files. Its isolated restore validated every hash/archive and
restore recipe for all 36 artifacts. No recovery journal or successful staging
remained; native workload health and published application endpoints passed
afterward. The original six-hour VM schedule is enabled again. Snapshot
manifests and recovery journals still use version 1.

Validation passed: 48 collector tests, plus 142 repository tests and 56
subtests (six skips). The repository run used pytest without stdin capture
for its existing mocked interactive console tests.
Actual manual deployment run 10 then passed using the migrated runner, keeping
all v2 participants, original secrets and enabled scheduling. The connected
server reported `/source` as `IDLE` with a 21,600-second interval.

## Verified application-clone recovery (2026-11-01)

The same snapshot `f5223eea8f545efffd12af956efe321a` was restored through a
separate read-only Kopia client with snapshot actions disabled. All 36 artifacts
were reverified. Application/database clones used new session-owned volumes
and an internal network without a default route, public ports, production
mounts or Docker sockets. Original FQDNs resolved to a private file-provider-only
Traefik proxy using the recovered wildcard certificate, never production.

| Workload | Actual recovery evidence |
| --- | --- |
| Keycloak/PostgreSQL | Original administrator login; two users, master/Sophon realms and restored OIDC clients. |
| Gitea/PostgreSQL | Original account, ten repositories, and full Git object integrity for `siteadmin/kopia`. |
| Nextcloud/PostgreSQL | Installed 35.0.2 application, original local admin/WebDAV, and an actual recovered file with exact SHA-256. |
| LDAP -> Keycloak -> Nextcloud | Real authorization-code login through the restored provider and strict LDAPS, using a temporary clone-only LDAP user. This is separate from original-account recovery. |
| LDAP | Native offline imports, normal image startup, strict TLS/root bind and all 13 original active entries. |
| Rocket.Chat/MongoDB | Complete users/oplog import followed by normal authenticated MongoDB startup; three users; original Rocket.Chat admin, one channel and one recovered message. |
| Arcane | Original user/token and authenticated environment endpoint; both SQLite databases passed integrity and the owner record survived. No Docker-management operations were attempted. |
| n8n | Original owner login and SQLite integrity; one owner, zero workflows/credentials in this snapshot. Fresh Python/JavaScript launchers registered with the cloned broker; no workflow execution was claimed. |
| Appsmith | Original owner session/user endpoint and one recovered workspace. |
| Nexus | Original administrator and seven restored repository definitions. No artifact download/upload was claimed. |
| Homepage | Restored configuration and HTTP application readiness. Production widgets/external services were not contacted. |
| Portainer | Native setup-token restore, isolated restart, original admin and all 24 stack definitions. No deployment operations were attempted. |
| AdGuard | Native inner archives/ownership, original admin and recovered configuration; real loopback DNS answer from a clone-only reserved-domain fixture. Production DNS and original client restrictions stayed unchanged. |
| Ten Gitea runners | Registration-file SHA-256 and required identity fields; normal entrypoints never started. |
| CyberChef | Fresh stateless image reconstruction and actual application HTTP response. |

This is restoration testing, not a production cutover or exhaustive feature
test. The original existing SSO user's password was not reset or assumed;
the federation test used a synthetic account only in the lab. Original images,
secrets and databases were retained. Restore resources are disposable and
must be removed after validation; the encrypted repository must be retained.
The x870 persistent-firewall limitation remains unchanged.

Cleanup passed: all 39 lab containers, 37 lab volumes, the internal network and
39 private remote environment files were removed. No owner-labelled test
container/volume remained. All 40 production containers retained their original
images, environments, labels, commands, protected mounts and running/stopped
states. Post-cleanup strict inventory found 33 v2 participants and all 43
production volumes covered or explicitly excluded. The connected production
repository's `/source` was IDLE with scheduling enabled at 21,600 seconds.

## Checks

From the Sophon root, `python3 tests/backup_checks.py` runs the 48 dependency-free collector tests;
`python3 tests/restore_checks.py` runs the eight recovery tests, including approval,
hash/profile/dependency/mount validation, isolated execution, exact-resource
resume, dirty-import rebuild, snapshot/state separation and foreign-resource
cleanup refusal.

`tests/test_kopia.py` also exercises normal core ordering/offline packaging,
required/private Configure inputs and real Ansible-to-HTTP deployment fixtures:
fresh collector-key creation, key reuse/environment preservation on rerun,
and explicit refusal of a revoked key. The regular Sophon suite includes these
checks; the build image contains no tests or Gitea deployment workflow.

### Verified automation (2026-11-01)

Real snapshot `80f493f79da23e8fb8dc2985940b1e4a` contains 776,173,048 bytes,
36 artifacts and zero failed files. It was independently restored read-only
with actions disabled. Gitea deployment runs 11 and 12 passed; run 12 packages
the final recovery tool. Its bytes were matched against the published source,
then the packaged tool performed a genuinely fresh automatic recovery:
two users, both master/Sophon realms and original master-administrator login
passed. The bootstrap system role was verified renamed, login-disabled and
password-cleared while retaining PostgreSQL's mandatory `SUPERUSER` attribute.

Rerunning the same command/state retained exact container/volume/network IDs
and revalidated credentials/data. Both target containers had no default route,
host binds or published ports. The real cleanup command removed its own two
containers, two volumes and internal network. Earlier real attempts exposed
the PostgreSQL system-role restriction and Podman's start-time `HOSTNAME`
injection; both have regression coverage rather than bypassed guards.
The source Keycloak backup recipes, image IDs, secrets, commands and volumes
were preserved through label rollout, and the VM's deployment template was
updated so a subsequent core configure retains the approved labels.

The full core repository suite also passed: 142 tests, six skips and 56
subtests. No production cutover or automated LDAP recovery is claimed; only
the PostgreSQL/Keycloak group is automated in profile version 1.

All executor targets and the independent client's container, three volumes
and protected remote environment file were removed. Final strict inventory
still reports 33 production backup participants and 43 covered/excluded
volumes. All 40 production containers preserve recorded commands, secrets,
protected mounts and running/stopped states; only the planned Kopia image
changed. Production application HTTPS checks passed, `/source` is IDLE with no
unresolved journal, and enabled native scheduling retains its 21,600-second
interval. Decrypted operational scratch is disposable, not a retained backup.

### Executable n8n and Nexus artifact follow-up (2026-11-01)

Newest encrypted snapshot `d330628cf5ddea255eae0ffb70a6dbbc` reports
772,975,868 bytes and zero failed files. A separate read-only client with
actions disabled restored it; all 36 artifacts verified. Internal clones again
used only fresh owned volumes, no published ports or production mounts, and
no default route. A real connection attempt to production Portainer was blocked.

The production n8n snapshot had no workflows/credentials, so a synthetic active
workflow and encrypted header credential were created only in the first clone.
The webhook executed JavaScript and Python through fresh external runners
(each returned `42`), then authenticated to a private fixture service using the
stored credential. After graceful shutdown, the same labelled filesystem
boundary was cold-archived, SHA-256 checked, and restored into a second new
volume. Original owner login, recovered webhook/workflow execution, both task
runners, encrypted-credential authentication and persisted successful execution
all passed without recreating the workflow or credential. Recovered SQLite
integrity was `ok`, with one workflow and one non-plaintext credential row.
Preserve the source encryption key and matching runner image/authentication;
restore ownership `1000:1000`.

The first Nexus clone accepted the original administrator and seven original
repository definitions. A synthetic raw hosted repository accepted an
authenticated 1,048,611-byte upload and exact download. Graceful shutdown,
full `/nexus-data` cold archive, SHA-256 verification and restoration into a
second fresh volume preserved its repository, asset metadata and artifact.
Authenticated download and Nexus's own asset SHA-256 both matched:
`70d4557262912f4c5f143a04710775fc27b6988f4bbdb67e10e78fc4c88c43df`.
Preserve H2 and blobs together, the source image, ownership `200:200`, and
the 120-second shutdown allowance.

These are real native fixture round trips after recovering the encrypted
production snapshot, **not** a new encrypted snapshot containing the fixtures.
They do not prove recovery of pre-existing production n8n workflows or every
Nexus repository format. Neither workload has an automated recovery profile.
All 15 lab containers, seven volumes, one internal network and eight protected
remote environment files were removed. All 40 production containers retained
their exact original configurations, images, mounts and running/stopped states.
Blank-host follow-up is recorded below; no production cutover was attempted.

### Offline blank-host recovery follow-up (2026-11-01)

With explicit approval, disposable Proxmox VM 101 was created with 12 GiB RAM,
four vCPUs and a fresh 100 GiB disk. It was **not a clone of InfraVM**. The normal
Sophon installer installed Fedora CoreOS onto the blank disk using existing
offline deployment media and a test-only bootstrap. The VM had no virtual NIC,
only loopback, and no default route. SELinux was verified `Enforcing`.
Production VM 100's configuration was unchanged before and after the drill.

The encrypted repository was independently connected read-only with actions
disabled and synchronized into an offline filesystem mirror. Only the exact
source Kopia, Portainer, PostgreSQL and Keycloak images were exported for this
test. Checksummed media crossed the pinned-TLS hypervisor management channel;
repository passwords/API credentials were delivered separately, not embedded
in the data ISO. The fresh VM verified all media file hashes, loaded the images,
opened the encrypted filesystem repository read-only with actions disabled,
and restored snapshot `d330628cf5ddea255eae0ffb70a6dbbc`.
All 36 artifacts verified again, including 775,272,853 native payload bytes.

| Fresh-host check | Verified result |
|---|---|
| Portainer native archive restore | Recovered TLS material, original administrator login and all 24 original stack definitions |
| Target discrimination | Restored manager used only the new VM's rootless Podman socket; engine identity differed from production |
| Actual management operation | Restored Portainer deployed a new harmless offline Compose stack, retained its exact Compose file, served the packaged recovery script with matching SHA-256, then deleted the stack/container |
| Original stack preservation | All 24 recovered definitions, endpoint references and environments remained unchanged; none was deployed |
| Packaged Keycloak recovery | Published source bytes matched; real PostgreSQL import recovered two users/two realms, original administrator login and the `sophon`/`master` realms |
| Resume and cleanup | Repeat retained exact resource IDs and repeated checks; real executor cleanup removed its owned resources |

The initial PostgreSQL system role was login-disabled, password-cleared and
renamed; its mandatory `SUPERUSER` attribute remains explicitly reported.
Recovery targets had an internal network, no published ports and no
application host binds. The management socket belonged only to the disposable
host, never to production.

#### Offline bootstrap prerequisites discovered

- Fedora CoreOS did not include `qemu-guest-agent`. This NIC-less drill needed
  an explicitly supplied guest-agent executable and matching shared libraries
  before native hypervisor guest execution worked.
- Agent media required the existing Fedora executable SELinux type
  `virt_qemu_ga_exec_t`, verified with `matchpathcon /usr/bin/qemu-ga`.
  Correct the service/media label rather than disabling SELinux. The installed
  test service was corrected during the drill; the first generated installer
  ISO alone is **not** a proven unattended bootstrap recipe.
- Create the filesystem destination before native `repository sync-to`.
  Verify checksums before image loading/restoration, preserve immutable source
  image IDs, and keep the repository password available outside the repository.
- A recovered Portainer archive restores definitions, not an already running
  site. Authenticate to the fresh manager, issue a fresh target-only API key,
  assert its engine identity, and verify isolation before deploying anything.
  Never blindly start the restored production stacks.

**This proves offline bootstrap plus Portainer and Keycloak/PostgreSQL recovery,
not a complete automatic full-site rebuild.** Only four application source
images were transferred. Other workload recovery profiles, all-workload
blank-host startup, external integrations and production cutover are not proved
by this drill. The n8n/Nexus second-generation fixture proofs above remain
separate native cold-archive tests.

After verification, VM 101 was gracefully shut down and removed, its dedicated
disk and all four test ISOs were deleted, and their absence was checked.
Original shared installer/deployment media and VM 100 were preserved.
Final production checks found all 40 containers, 33 v2 backup participants and
43 covered/excluded volumes, no recovery journal, a connected repository and
the unchanged 21,600-second interval. n8n, Nexus, Keycloak, Nextcloud, Gitea,
Rocket.Chat and Homepage all passed HTTPS checks. Temporary private recovery
scratch is disposable, not a retained backup.
