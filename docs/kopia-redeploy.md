# Fresh dev redeployment: 2026-11-16

## Scope and limits

The operator explicitly authorized replacing **dev VM 107 only**. The old dev
VM/disks were deleted, final M4 installation media were uploaded to dedicated
dev storage, and a blank 64 GiB VM was installed through the real CoreOS installer.
Neither production VM nor the production backup repository was accessed.

The new guest is `10.0.60.66`, Fedora CoreOS `44.20260928.3.2`, kernel
`7.2.7-200.fc44.x86_64`, with XFS `/var` and rootless Podman.
Provisioning used the scoped dev Proxmox API token; the shared console supplied
first-login/bootstrap access because the guest agent was unavailable.

Dev-only exceptions:

- Reserved `dev.invalid` domain, synthetic CA/wildcard certificate and a guest-only
  template overlay for trusted TLS. No Cloudflare tunnel/token, ACME resolver,
  certificate issuance or Gitea actions/runners.
- Confined SFTP account on the dev guest, outside Podman's volumes tree. This
  exercises actual SSH/SFTP but is **not offsite protection**.
- After discovering the activation bug below, the corrected collector archive,
  installed console entry point and explicit source-version suffix were deployed
  into this fresh installation. The entire VM was not reinstalled a second time.
  Corrected offline media were also rebuilt.

Private input/key files and raw Configure logs are not published: existing
deployment debug output includes generated dev passwords.

## Configure and the integration bug

The first real console Configure completed:

```text
localhost : ok=180 changed=9 unreachable=0 failed=0 skipped=59
```

Enabling coarse during a second real Configure reproduced a defect missed by
idle-engine spikes. The immediate coarse attempt stopped Configure's own
`--rm` runner, `sweet_hodgkin`. Configure exited **137**. Recovery restarted all
persistent application IDs but correctly retained a hold for the now-deleted
runner ID. Podman events proved that the sole remaining ID belonged to that
temporary runner. With the coordinator stopped, that one reference was explicitly
acknowledged/removed; normal recovery cleared the journal/hold.

This directly established the cause without speculative instrumentation.
The fix labels the supported Configure runner `sophon.deployment=configure`.
Scheduled coarse capture defers while such a runner is active without advancing
its due time; manual capture refuses before any stop. Deleted workload IDs still
fail recovery: no missing-ID exception or name-based replacement was added.
Concurrent deployment after capture begins remains unsupported.

The regression was run before the fix:

```sh
docker run --rm -v /home/caleb/sophon:/workspace --entrypoint python3 \
  sophon-tools -m unittest discover -s tests -p test_coarse.py
```

```text
FAIL: test_configure_in_progress_is_refused_before_any_container_stop
AssertionError: RuntimeError not raised
Ran 6 tests
FAILED (failures=1)
```

The same command after the fix:

```text
Ran 6 tests in 0.201s
OK
```

The real dev command was rerun with coarse enabled and its idle first-attempt
schedule deliberately reset:

```sh
~/redeploy-dev-configure.sh >~/sophon-dev-configure-fixed.log 2>&1
```

```text
Fixed real Configure exit: 0
localhost : ok=168 changed=8 unreachable=0 failed=0 skipped=71 rescued=0 ignored=0
```

The running coarse image was
`d85834368e799f17324c5de647c480c00decb18ed525b8dc68a6bd1dc1c767df`,
with restart policy `unless-stopped`. Its first actual whole-volume snapshot
completed as `a3e300a3032b706e31b0850504a4b1aa`.

## Forced native overlap and post-backup checks

To force overlap independently of startup timing, the dev helper
`bash ~/redeploy-dev-coarse-proof.sh` started a network-isolated, no-volume
Configure-labeled placeholder, reset the idle weekly schedule, and restarted the
coarse coordinator. It compared every original service's start timestamp before
and during deferral, then removed the placeholder:

```text
Coarse backup deferred while Configure is running
Native Configure overlap: deferred; due unchanged; original service start times unchanged
{"status":"complete","source":"backup@sophon-full-dev:/sophon-coarse","snapshot":"936b7d02a929a660895f2047d7c25648"}
```

The helper's immediate final running-state assertion exited 1 while Keycloak
performed an ordinary post-restart reconnection/restart. It was not reported as
an entirely passing script. Follow-up assertions after settling verified every
original ID running, no coarse journal/pending/staging, and no primary backup,
restore or coarse hold. No startup dependency ordering was introduced.

The final functional command used the actual dev environment file and public CA:

```sh
podman run --rm --network host --pull never --security-opt label=disable \
  --read-only --tmpfs /tmp --cap-drop=ALL \
  --add-host auth.dev.invalid:10.0.60.66 \
  --add-host git.dev.invalid:10.0.60.66 \
  --add-host portainer.dev.invalid:10.0.60.66 \
  -v /var/lib/sophon/project:/workspace:ro --workdir /workspace \
  --env-file ~/sophon-dev-configure.env \
  sophon:latest python3 imports/redeploy-dev-verify.py
```

```json
{
  "coredns_dev_record": "10.0.60.66",
  "verified_dev_tls_services": ["auth", "git", "portainer"],
  "keycloak_ldap_password_grant": "token issued",
  "gitea_commit": "fede9c68b1ac81aff6e6d9e42cc7bb82777e2d14",
  "gitea_protected_settings_path": "/user/settings",
  "gitea_oidc_ldap_login": "john.doe",
  "portainer_oidc_ldap_admin": {"username": "john.doe.admin", "role": 1},
  "status": "complete"
}
```

An additional `jq -e` assertion required that exact original commit SHA, not just
any successful Gitea response.

## Primary backup and explicit restoration

The first real primary snapshot was `1d51c80a8fc3bd88489c257bb3927b99`.
Only the newly created dev validation repository was then deleted. The installed
`sophon-restore` plan/apply commands restored Gitea with Portainer stopped; the
original commit was recovered. Decrypted restore working storage was removed.
Configure did not invoke restoration.

The persisted result was checked on the actual new guest:

```sh
jq -r .phase ~/sophon-dev-restore-result.json
```

```text
complete
```

Final primary report assertions required `status=complete`,
`upload_status=complete`, no failures, and no coordination holds:

```json
{"primary_status":"complete","primary_upload_status":"complete","primary_failures":[],"snapshot":"776ed33a860d413f81748dd2a924b3ad","coordination_holds":"clear"}
```

This fresh deployment repeated Gitea restoration, not the complete four-application
M2 drill. Earlier drill evidence remains in [M2](kopia-m2.md); it is not presented
as rerun here.

## Repository and media checks

```sh
docker run --rm -v /home/caleb/sophon:/workspace --entrypoint python3 \
  sophon-tools -m unittest discover -s tests
```

```text
Ran 158 tests in 87.760s
OK (skipped=3)
```

```sh
docker run --rm -v /home/caleb/sophon:/workspace --entrypoint bash sophon-tools \
  -lc 'bash -n guest/sophon-deploy && python3 tests/test_steps.py'
bash sophon-provision.sh prepare
```

```text
Deployment step checks passed
localhost : ok=18 changed=2 unreachable=0 failed=0 skipped=7 rescued=0 ignored=0
```

Direct review against `9fff9b0` found no standards or spec findings in the
integration fix. No legacy tag was pushed, and no existing milestone PR was merged.
