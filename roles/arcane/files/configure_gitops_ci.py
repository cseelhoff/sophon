#!/usr/bin/env python3
"""Idempotent configurator for Sophon Stage-2 Gitea Actions GitOps CI.

Reads a JSON payload from stdin and reconciles over HTTPS REST APIs:
  1. Keycloak confidential service-account client (`ops-gitops-ci`) with
     least-privilege `realm-management` client/group roles (`manage-clients`,
     `manage-users`, `view-realm`, `query-groups`, `query-clients`,
     `query-users`).
  2. Gitea `ops` organization, `ops/workloads` repository (if absent), and
     org-level Actions runner registration token.
  3. Arcane `ops-workloads` GitRepository, least-privilege scoped API key
     (`ops-gitops-ci`), and bootstrap `gitea-runner` GitOpsSync so a fresh push
     to `ops/workloads` brings up the socketless runner automatically.
  4. Encrypted Gitea `ops` organization Actions secrets (`ARCANE_API_KEY`,
     `KEYCLOAK_CI_CLIENT_ID`, `KEYCLOAK_CI_CLIENT_SECRET`), plus idempotent
     `main` branch protection on `ops/workloads` once commits exist.

Never prints secret values to stdout or stderr. Emits JSON `{"changed": bool, "actions": [...]}`.
"""

from __future__ import annotations

import base64
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ARCANE_GLOBAL_PERMISSIONS = (
    "environments:list",
    "environments:read",
    "git-repositories:list",
    "git-repositories:read",
    "git-repositories:create",
    "git-repositories:update",
    "git-repositories:test",
    "git-repositories:sync",
)

ARCANE_ENV_PERMISSIONS = (
    "gitops:list",
    "gitops:read",
    "gitops:create",
    "gitops:update",
    "gitops:delete",
    "gitops:sync",
    "gitops:lifecycle",
    "projects:list",
    "projects:read",
    "projects:create",
    "projects:update",
    "projects:deploy",
    "projects:restart",
    "projects:down",
    "images:list",
    "images:read",
    "images:pull",
    "activities:list",
    "activities:read",
)

KEYCLOAK_REALM_MGMT_ROLES = (
    "manage-clients",
    "manage-users",
    "view-realm",
    "query-groups",
    "query-clients",
    "query-users",
)


def make_ssl_context(verify_tls: bool) -> ssl.SSLContext:
    if verify_tls:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_json(
    method: str,
    url: str,
    *,
    ctx: ssl.SSLContext,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form_body: dict[str, str] | None = None,
    expected: tuple[int, ...] = (200, 201, 204),
) -> tuple[int, Any]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        if status not in expected:
            raise RuntimeError(f"{method} {url} failed with HTTP {status}: {raw[:400]}") from exc

    if status not in expected:
        raise RuntimeError(f"{method} {url} returned unexpected HTTP {status}: {raw[:400]}")

    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def parse_env_lines(env_content: str) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    values: dict[str, str] = {}
    for raw_line in (env_content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k not in values:
            order.append(k)
        values[k] = v
    return order, values


def render_env_lines(order: list[str], values: dict[str, str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for k in order:
        if k in values and k not in seen:
            out.append(f"{k}={values[k]}")
            seen.add(k)
    for k in sorted(values):
        if k not in seen:
            out.append(f"{k}={values[k]}")
    return "\n".join(out)


def reconcile_keycloak(cfg: dict[str, Any], ctx: ssl.SSLContext, actions: list[str]) -> str:
    kc_base = cfg["keycloak_url"].rstrip("/")
    realm = cfg["keycloak_realm"]
    ci_client_id = cfg["ci_client_id"]

    _, tok_resp = http_json(
        "POST",
        f"{kc_base}/realms/master/protocol/openid-connect/token",
        ctx=ctx,
        form_body={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": cfg["keycloak_admin_user"],
            "password": cfg["keycloak_admin_password"],
        },
    )
    kc_headers = {"Authorization": f"Bearer {tok_resp['access_token']}"}

    _, clients = http_json(
        "GET",
        f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients?clientId={urllib.parse.quote(ci_client_id)}",
        ctx=ctx,
        headers=kc_headers,
    )
    exact = [c for c in (clients or []) if c.get("clientId") == ci_client_id]
    desired_client = {
        "clientId": ci_client_id,
        "name": "Sophon GitOps CI Reconciler",
        "description": "Least-privilege service account used by ops/workloads Gitea Actions pipeline",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
    }
    if not exact:
        http_json(
            "POST",
            f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients",
            ctx=ctx,
            headers=kc_headers,
            json_body=desired_client,
        )
        actions.append(f"keycloak:created_client:{ci_client_id}")
        _, clients = http_json(
            "GET",
            f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients?clientId={urllib.parse.quote(ci_client_id)}",
            ctx=ctx,
            headers=kc_headers,
        )
        exact = [c for c in (clients or []) if c.get("clientId") == ci_client_id]

    client_obj = exact[0]
    client_uuid = client_obj["id"]
    needs_update = any(client_obj.get(k) != v for k, v in desired_client.items() if k in client_obj)
    if needs_update:
        merged = dict(client_obj)
        merged.update(desired_client)
        http_json(
            "PUT",
            f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients/{client_uuid}",
            ctx=ctx,
            headers=kc_headers,
            json_body=merged,
        )
        actions.append(f"keycloak:updated_client:{ci_client_id}")

    _, sa_user = http_json(
        "GET",
        f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients/{client_uuid}/service-account-user",
        ctx=ctx,
        headers=kc_headers,
    )
    sa_user_id = sa_user["id"]

    _, rm_clients = http_json(
        "GET",
        f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients?clientId=realm-management",
        ctx=ctx,
        headers=kc_headers,
    )
    rm_uuid = [c["id"] for c in rm_clients if c.get("clientId") == "realm-management"][0]

    _, assigned_roles = http_json(
        "GET",
        f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/users/{sa_user_id}/role-mappings/clients/{rm_uuid}",
        ctx=ctx,
        headers=kc_headers,
    )
    assigned_names = {r["name"] for r in (assigned_roles or [])}
    missing_names = [r for r in KEYCLOAK_REALM_MGMT_ROLES if r not in assigned_names]
    if missing_names:
        _, available_roles = http_json(
            "GET",
            f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients/{rm_uuid}/roles",
            ctx=ctx,
            headers=kc_headers,
        )
        by_name = {r["name"]: r for r in (available_roles or [])}
        to_assign = [by_name[name] for name in missing_names if name in by_name]
        if to_assign:
            http_json(
                "POST",
                f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/users/{sa_user_id}/role-mappings/clients/{rm_uuid}",
                ctx=ctx,
                headers=kc_headers,
                json_body=to_assign,
            )
            actions.append(f"keycloak:granted_roles:{','.join(r['name'] for r in to_assign)}")

    _, sec_resp = http_json(
        "GET",
        f"{kc_base}/admin/realms/{urllib.parse.quote(realm)}/clients/{client_uuid}/client-secret",
        ctx=ctx,
        headers=kc_headers,
    )
    client_secret = sec_resp["value"]

    # Smoke-verify client_credentials token grant works immediately.
    http_json(
        "POST",
        f"{kc_base}/realms/{urllib.parse.quote(realm)}/protocol/openid-connect/token",
        ctx=ctx,
        form_body={
            "grant_type": "client_credentials",
            "client_id": ci_client_id,
            "client_secret": client_secret,
        },
    )
    return client_secret


def gitea_basic_headers(username: str, password: str) -> dict[str, str]:
    b64 = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {b64}"}


def reconcile_gitea_org_and_token(
    cfg: dict[str, Any], ctx: ssl.SSLContext, actions: list[str]
) -> tuple[str, set[str]]:
    gt_base = cfg["gitea_url"].rstrip("/")
    org = cfg["gitea_org"]
    repo = cfg["gitea_repo"]
    headers = gitea_basic_headers(cfg["gitea_admin_user"], cfg["gitea_admin_password"])

    status, org_obj = http_json(
        "GET",
        f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}",
        ctx=ctx,
        headers=headers,
        expected=(200, 404),
    )
    if status == 404:
        http_json(
            "POST",
            f"{gt_base}/api/v1/orgs",
            ctx=ctx,
            headers=headers,
            json_body={
                "username": org,
                "full_name": "Sophon Operations",
                "visibility": "private",
                "repo_admin_change_team_access": False,
            },
        )
        actions.append(f"gitea:created_org:{org}")
    elif (org_obj or {}).get("visibility") != "private" or (org_obj or {}).get(
        "repo_admin_change_team_access"
    ) is True:
        http_json(
            "PATCH",
            f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}",
            ctx=ctx,
            headers=headers,
            json_body={
                "visibility": "private",
                "repo_admin_change_team_access": False,
            },
        )
        actions.append(f"gitea:locked_private_org:{org}")

    r_status, repo_obj = http_json(
        "GET",
        f"{gt_base}/api/v1/repos/{urllib.parse.quote(org)}/{urllib.parse.quote(repo)}",
        ctx=ctx,
        headers=headers,
        expected=(200, 404),
    )
    repo_empty = True
    if r_status == 404:
        http_json(
            "POST",
            f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}/repos",
            ctx=ctx,
            headers=headers,
            json_body={
                "name": repo,
                "description": "Sophon Stage-2 GitOps Compose workloads",
                "private": True,
                "auto_init": False,
                "default_branch": "main",
            },
        )
        actions.append(f"gitea:created_repo:{org}/{repo}")
    else:
        repo_empty = bool((repo_obj or {}).get("empty", False))
        if (repo_obj or {}).get("private") is False:
            http_json(
                "PATCH",
                f"{gt_base}/api/v1/repos/{urllib.parse.quote(org)}/{urllib.parse.quote(repo)}",
                ctx=ctx,
                headers=headers,
                json_body={"private": True},
            )
            actions.append(f"gitea:locked_private_repo:{org}/{repo}")

    reconcile_gitea_ops_admin_ownership(cfg, ctx, headers, actions)

    if not repo_empty:
        reconcile_gitea_branch_protection(cfg, ctx, headers, actions)

    _, tok_resp = http_json(
        "POST",
        f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}/actions/runners/registration-token",
        ctx=ctx,
        headers=headers,
    )
    reg_token = tok_resp["token"]

    _, secrets_resp = http_json(
        "GET",
        f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}/actions/secrets?limit=100",
        ctx=ctx,
        headers=headers,
    )
    items = secrets_resp if isinstance(secrets_resp, list) else (secrets_resp or {}).get("data", [])
    existing_secret_names = {s["name"].upper() for s in items if isinstance(s, dict) and s.get("name")}
    return reg_token, existing_secret_names


GITEA_MAIN_REQUIRED_CHECKS = [
    "Reconcile Workloads / Validate & Reconcile Compose Workloads (pull_request)"
]


def reconcile_gitea_ops_admin_ownership(
    cfg: dict[str, Any],
    ctx: ssl.SSLContext,
    headers: dict[str, str],
    actions: list[str],
) -> None:
    """Restrict `ops` and `ops/workloads` strictly to Gitea administrators.

    Standard SSO users can sign into Gitea for regular repositories, while
    `ops` (`visibility=private`) and `ops/workloads` (`private=True`) have only
    the `Owners` team populated strictly with active Gitea admins (`is_admin=True`)
    and zero direct repo collaborators or non-Owners teams.
    """
    gt_base = cfg["gitea_url"].rstrip("/")
    org = cfg["gitea_org"]
    repo = cfg["gitea_repo"]

    _, users = http_json(
        "GET",
        f"{gt_base}/api/v1/admin/users?limit=200",
        ctx=ctx,
        headers=headers,
    )
    admin_logins = {
        u["login"]
        for u in (users or [])
        if isinstance(u, dict) and u.get("login") and u.get("is_admin") and u.get("active", True)
    }
    admin_logins.add(cfg["gitea_admin_user"])

    _, teams = http_json(
        "GET",
        f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}/teams?limit=100",
        ctx=ctx,
        headers=headers,
    )
    owners_team_id: int | None = None
    for team in teams or []:
        if not isinstance(team, dict) or "id" not in team:
            continue
        t_id = int(team["id"])
        t_name = str(team.get("name") or "")
        if t_name == "Owners":
            owners_team_id = t_id
        else:
            http_json(
                "DELETE",
                f"{gt_base}/api/v1/teams/{t_id}",
                ctx=ctx,
                headers=headers,
                expected=(204, 404),
            )
            actions.append(f"gitea:deleted_non_owner_team:{org}/{t_name}")

    if owners_team_id is not None:
        _, members = http_json(
            "GET",
            f"{gt_base}/api/v1/teams/{owners_team_id}/members?limit=200",
            ctx=ctx,
            headers=headers,
        )
        current_owners = {
            m["login"] for m in (members or []) if isinstance(m, dict) and m.get("login")
        }
        for login in sorted(admin_logins - current_owners):
            http_json(
                "PUT",
                f"{gt_base}/api/v1/teams/{owners_team_id}/members/{urllib.parse.quote(login)}",
                ctx=ctx,
                headers=headers,
                expected=(204,),
            )
            actions.append(f"gitea:added_ops_owner:{login}")
        for login in sorted(current_owners - admin_logins):
            http_json(
                "DELETE",
                f"{gt_base}/api/v1/teams/{owners_team_id}/members/{urllib.parse.quote(login)}",
                ctx=ctx,
                headers=headers,
                expected=(204, 404),
            )
            actions.append(f"gitea:removed_non_admin_ops_owner:{login}")

    _, collabs = http_json(
        "GET",
        f"{gt_base}/api/v1/repos/{urllib.parse.quote(org)}/{urllib.parse.quote(repo)}/collaborators?limit=200",
        ctx=ctx,
        headers=headers,
    )
    for collab in collabs or []:
        if isinstance(collab, dict) and collab.get("login"):
            c_login = collab["login"]
            http_json(
                "DELETE",
                f"{gt_base}/api/v1/repos/{urllib.parse.quote(org)}/{urllib.parse.quote(repo)}/collaborators/{urllib.parse.quote(c_login)}",
                ctx=ctx,
                headers=headers,
                expected=(204, 404),
            )
            actions.append(f"gitea:removed_direct_collaborator:{org}/{repo}:{c_login}")


def reconcile_gitea_branch_protection(
    cfg: dict[str, Any],
    ctx: ssl.SSLContext,
    headers: dict[str, str],
    actions: list[str],
) -> None:
    gt_base = cfg["gitea_url"].rstrip("/")
    org = cfg["gitea_org"]
    repo = cfg["gitea_repo"]
    desired_bp: dict[str, Any] = {
        "branch_name": "main",
        "rule_name": "main",
        "enable_push": False,
        "enable_push_whitelist": False,
        "block_admin_merge_override": False,
        "enable_merge_whitelist": False,
        "required_approvals": 1,
        "enable_approvals_whitelist": True,
        "approvals_whitelist_teams": ["Owners"],
        "block_on_rejected_reviews": True,
        "block_on_official_review_requests": True,
        "block_on_outdated_branch": False,
        "dismiss_stale_approvals": True,
        "require_signed_commits": False,
        "enable_status_check": True,
        "status_check_contexts": GITEA_MAIN_REQUIRED_CHECKS,
    }
    bp_url = (
        f"{gt_base}/api/v1/repos/{urllib.parse.quote(org)}"
        f"/{urllib.parse.quote(repo)}/branch_protections"
    )
    bp_status, bp_obj = http_json(
        "GET",
        f"{bp_url}/main",
        ctx=ctx,
        headers=headers,
        expected=(200, 404),
    )
    if bp_status == 404:
        http_json("POST", bp_url, ctx=ctx, headers=headers, json_body=desired_bp)
        actions.append(f"gitea:created_branch_protection:{org}/{repo}:main")
        return

    check_keys = (
        "enable_push",
        "required_approvals",
        "enable_approvals_whitelist",
        "approvals_whitelist_teams",
        "block_on_rejected_reviews",
        "dismiss_stale_approvals",
        "enable_status_check",
        "status_check_contexts",
    )
    if any((bp_obj or {}).get(k) != desired_bp[k] for k in check_keys):
        http_json("PATCH", f"{bp_url}/main", ctx=ctx, headers=headers, json_body=desired_bp)
        actions.append(f"gitea:updated_branch_protection:{org}/{repo}:main")


def put_gitea_org_secret(
    cfg: dict[str, Any],
    ctx: ssl.SSLContext,
    name: str,
    value: str,
    existing_names: set[str],
    actions: list[str],
    *,
    mark_changed_when_created_only: bool = True,
) -> None:
    gt_base = cfg["gitea_url"].rstrip("/")
    org = cfg["gitea_org"]
    headers = gitea_basic_headers(cfg["gitea_admin_user"], cfg["gitea_admin_password"])
    status, _ = http_json(
        "PUT",
        f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}/actions/secrets/{urllib.parse.quote(name)}",
        ctx=ctx,
        headers=headers,
        json_body={"data": value},
        expected=(201, 204),
    )
    if status == 201 or (name.upper() not in existing_names) or not mark_changed_when_created_only:
        actions.append(f"gitea:set_org_secret:{name}")


def reconcile_arcane(
    cfg: dict[str, Any],
    ctx: ssl.SSLContext,
    reg_token: str,
    existing_gitea_secrets: set[str],
    actions: list[str],
) -> str | None:
    arc_base = cfg["arcane_url"].rstrip("/")
    _, login_resp = http_json(
        "POST",
        f"{arc_base}/api/auth/login",
        ctx=ctx,
        json_body={
            "username": cfg["arcane_admin_user"],
            "password": cfg["arcane_admin_password"],
        },
    )
    jwt = (login_resp.get("data") or login_resp)["token"]
    arc_headers = {"Authorization": f"Bearer {jwt}"}

    # 1. Ensure ops-workloads GitRepository exists.
    repo_url = f"{cfg['gitea_url'].rstrip('/')}/{cfg['gitea_org']}/{cfg['gitea_repo']}.git"
    _, repos_resp = http_json(
        "GET",
        f"{arc_base}/api/gitops/repositories?limit=100",
        ctx=ctx,
        headers=arc_headers,
    )
    repos = repos_resp.get("data") or []
    repo_obj = next(
        (r for r in repos if r.get("name") == "ops-workloads" or r.get("url") == repo_url),
        None,
    )
    if repo_obj is None:
        _, created = http_json(
            "POST",
            f"{arc_base}/api/gitops/repositories",
            ctx=ctx,
            headers=arc_headers,
            json_body={
                "name": "ops-workloads",
                "description": "Sophon Stage-2 Pure GitOps Compose stacks",
                "url": repo_url,
                "provider": "gitea",
                "authType": "http",
                "username": cfg["gitea_admin_user"],
                "token": cfg["gitea_admin_password"],
                "commitAuthorName": "Arcane GitOps",
                "commitAuthorEmail": f"{cfg['gitea_admin_user']}@{cfg['domain_name']}",
                "enabled": True,
            },
        )
        repo_obj = created.get("data") or created
        actions.append("arcane:created_repository:ops-workloads")
    repo_id = repo_obj["id"]

    # 2. Ensure least-privilege scoped API key `ops-gitops-ci` exists.
    key_name = cfg["ci_client_id"]
    _, keys_resp = http_json(
        "GET",
        f"{arc_base}/api/apikeys?limit=100",
        ctx=ctx,
        headers=arc_headers,
    )
    keys = keys_resp.get("data") or []
    matching_keys = [k for k in keys if k.get("name") == key_name]

    desired_grants = [{"permission": p} for p in ARCANE_GLOBAL_PERMISSIONS] + [
        {"environmentId": "0", "permission": p} for p in ARCANE_ENV_PERMISSIONS
    ]

    new_api_key: str | None = None
    need_mint = (
        bool(cfg.get("rotate_arcane_key", False))
        or not matching_keys
        or "ARCANE_API_KEY" not in existing_gitea_secrets
    )
    if need_mint:
        for stale in matching_keys:
            http_json(
                "DELETE",
                f"{arc_base}/api/apikeys/{stale['id']}",
                ctx=ctx,
                headers=arc_headers,
                expected=(200, 204),
            )
        _, created_key = http_json(
            "POST",
            f"{arc_base}/api/apikeys",
            ctx=ctx,
            headers=arc_headers,
            json_body={
                "name": key_name,
                "description": "Least-privilege scoped key for ops/workloads Gitea Actions reconciler",
                "permissions": desired_grants,
            },
        )
        key_dto = created_key.get("data") or created_key
        new_api_key = key_dto["key"]
        # Verify the newly minted scoped key works via X-API-Key header.
        http_json(
            "GET",
            f"{arc_base}/api/gitops/repositories?limit=5",
            ctx=ctx,
            headers={"X-API-Key": new_api_key},
        )
        actions.append(f"arcane:minted_scoped_apikey:{key_name}")
    else:
        existing_key = matching_keys[0]
        cur_perms = {
            (g.get("environmentId", ""), g.get("permission", ""))
            for g in (existing_key.get("permissions") or [])
        }
        want_perms = {
            (g.get("environmentId", ""), g.get("permission", ""))
            for g in desired_grants
        }
        if cur_perms != want_perms:
            http_json(
                "PATCH",
                f"{arc_base}/api/apikeys/{existing_key['id']}",
                ctx=ctx,
                headers=arc_headers,
                json_body={
                    "name": key_name,
                    "description": "Least-privilege scoped key for ops/workloads Gitea Actions reconciler",
                    "permissions": desired_grants,
                },
            )
            actions.append(f"arcane:updated_scoped_apikey_permissions:{key_name}")

    # 3. Ensure bootstrap `gitea-runner` GitOpsSync exists with registration token in envContent.
    _, syncs_resp = http_json(
        "GET",
        f"{arc_base}/api/agents/0/gitops/syncs?limit=100",
        ctx=ctx,
        headers=arc_headers,
    )
    syncs = syncs_resp.get("data") or []
    runner_sync = next(
        (
            s
            for s in syncs
            if s.get("name") == "gitea-runner"
            or s.get("projectName") == "gitea-runner"
            or s.get("composePath") == "gitea-runner/compose.yaml"
        ),
        None,
    )

    desired_env_updates = {
        "SOPHON_DOMAIN": cfg["domain_name"],
        "GITEA_INSTANCE_URL": cfg["gitea_url"].rstrip("/"),
        "ACT_RUNNER_REGISTRATION_TOKEN": reg_token,
    }
    if runner_sync is None:
        env_text = render_env_lines(list(desired_env_updates.keys()), desired_env_updates)
        http_json(
            "POST",
            f"{arc_base}/api/agents/0/gitops/syncs",
            ctx=ctx,
            headers=arc_headers,
            json_body={
                "name": "gitea-runner",
                "projectName": "gitea-runner",
                "repositoryId": repo_id,
                "branch": "main",
                "composePath": "gitea-runner/compose.yaml",
                "targetType": "project",
                "mode": "deploy",
                "syncInterval": 5,
                "autoSync": True,
                "syncDirectory": True,
                "pullImageAfterSync": False,
                "redeployAfterSync": True,
                "envContent": env_text,
            },
        )
        actions.append("arcane:created_gitops_sync:gitea-runner")
    else:
        order, cur_env = parse_env_lines(runner_sync.get("envContent") or "")
        env_changed = False
        for k, v in desired_env_updates.items():
            if cur_env.get(k) != v:
                if k not in cur_env:
                    order.append(k)
                cur_env[k] = v
                env_changed = True
        if env_changed or runner_sync.get("repositoryId") != repo_id:
            http_json(
                "PATCH",
                f"{arc_base}/api/agents/0/gitops/syncs/{runner_sync['id']}",
                ctx=ctx,
                headers=arc_headers,
                json_body={
                    "name": runner_sync["name"],
                    "projectName": runner_sync.get("projectName") or "gitea-runner",
                    "repositoryId": repo_id,
                    "branch": runner_sync.get("branch") or "main",
                    "composePath": "gitea-runner/compose.yaml",
                    "targetType": runner_sync.get("targetType") or "project",
                    "mode": runner_sync.get("mode") or "deploy",
                    "syncInterval": runner_sync.get("syncInterval") or 5,
                    "autoSync": bool(runner_sync.get("autoSync", True)),
                    "syncDirectory": bool(runner_sync.get("syncDirectory", True)),
                    "pullImageAfterSync": bool(runner_sync.get("pullImageAfterSync", False)),
                    "redeployAfterSync": bool(runner_sync.get("redeployAfterSync", True)),
                    "envContent": render_env_lines(order, cur_env),
                },
            )
            actions.append("arcane:updated_gitops_sync:gitea-runner")

    return new_api_key


def main() -> int:
    cfg = json.load(sys.stdin)
    ctx = make_ssl_context(bool(cfg.get("verify_tls", False)))
    actions: list[str] = []

    kc_secret = reconcile_keycloak(cfg, ctx, actions)
    reg_token, existing_secrets = reconcile_gitea_org_and_token(cfg, ctx, actions)
    new_arcane_key = reconcile_arcane(cfg, ctx, reg_token, existing_secrets, actions)

    put_gitea_org_secret(
        cfg, ctx, "KEYCLOAK_CI_CLIENT_ID", cfg["ci_client_id"], existing_secrets, actions
    )
    put_gitea_org_secret(
        cfg, ctx, "KEYCLOAK_CI_CLIENT_SECRET", kc_secret, existing_secrets, actions
    )
    if "ACT_RUNNER_REGISTRATION_TOKEN" in existing_secrets:
        gt_base = cfg["gitea_url"].rstrip("/")
        org = cfg["gitea_org"]
        headers = gitea_basic_headers(cfg["gitea_admin_user"], cfg["gitea_admin_password"])
        http_json(
            "DELETE",
            f"{gt_base}/api/v1/orgs/{urllib.parse.quote(org)}/actions/secrets/ACT_RUNNER_REGISTRATION_TOKEN",
            ctx=ctx,
            headers=headers,
            expected=(204, 404),
        )
        actions.append("gitea:deleted_org_secret:ACT_RUNNER_REGISTRATION_TOKEN")
    if new_arcane_key is not None:
        put_gitea_org_secret(
            cfg,
            ctx,
            "ARCANE_API_KEY",
            new_arcane_key,
            existing_secrets,
            actions,
            mark_changed_when_created_only=False,
        )

    print(json.dumps({"changed": bool(actions), "actions": actions}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
