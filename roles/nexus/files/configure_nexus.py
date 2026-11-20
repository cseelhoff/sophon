#!/usr/bin/env python3
"""Idempotent Day-1 & reconciliation configurator for Sonatype Nexus 3 Community Edition.

Reads configuration JSON from stdin (so credentials never appear in process argv) and
converges admin credentials, CE EULA, anonymous policy, SSL truststore, OpenLDAP server,
RBAC roles, active realms, `sophon-pull` reader user, `hosted` / `scratch` / `proxy-cache`
blob stores, and hosted/proxy/group repositories across Docker, PyPI, npm, APT, Maven, Cargo,
and Raw (`raw-vault`, `raw-hosted`, `raw-scratch`, `raw-group`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


# ---------------------------------------------------------------------------
# Pure-Python RFC 4880 OpenPGP v4 RSA Signing Key Generator (for apt-hosted)
# ---------------------------------------------------------------------------

def _mpi(value: int) -> bytes:
    if value == 0:
        return b"\x00\x00"
    bit_len = value.bit_length()
    byte_len = (bit_len + 7) // 8
    return struct.pack(">H", bit_len) + value.to_bytes(byte_len, "big")


def _pgp_new_packet(tag: int, body: bytes) -> bytes:
    header = bytes([0xC0 | tag])
    n = len(body)
    if n < 192:
        length = bytes([n])
    elif n < 8384:
        n2 = n - 192
        length = bytes([(n2 >> 8) + 192, n2 & 0xFF])
    else:
        length = b"\xff" + struct.pack(">I", n)
    return header + length + body


def _pgp_subpacket(sub_type: int, data: bytes) -> bytes:
    payload = bytes([sub_type]) + data
    n = len(payload)
    if n < 192:
        return bytes([n]) + payload
    n2 = n - 192
    return bytes([(n2 >> 8) + 192, n2 & 0xFF]) + payload


def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def _armor(block_type: str, raw: bytes) -> str:
    b64 = base64.b64encode(raw).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    crc_bytes = _crc24(raw).to_bytes(3, "big")
    crc_b64 = "=" + base64.b64encode(crc_bytes).decode("ascii")
    return "\n".join(
        [
            f"-----BEGIN {block_type}-----",
            "",
            *lines,
            crc_b64,
            f"-----END {block_type}-----",
            "",
        ]
    )


def generate_openpgp_rsa_keypair(uid: str, *, key_size: int = 3072, created_at: int | None = None) -> tuple[str, str]:
    """Return `(armored_private_key, armored_public_key)` RFC 4880 v4 RSA signing keypair."""
    timestamp = int(time.time()) if created_at is None else int(created_at)
    priv = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    numbers = priv.private_numbers()
    pub_numbers = numbers.public_numbers
    n = pub_numbers.n
    e = pub_numbers.e
    d = numbers.d
    # RFC 4880 §5.5.3 requires p < q and u = p^-1 mod q.
    p, q = (numbers.p, numbers.q) if numbers.p < numbers.q else (numbers.q, numbers.p)
    u = pow(p, -1, q)

    pub_body = struct.pack(">BIB", 4, timestamp, 1) + _mpi(n) + _mpi(e)
    fp_prefix = b"\x99" + struct.pack(">H", len(pub_body)) + pub_body
    fingerprint = hashlib.sha1(fp_prefix).digest()  # noqa: S324 - required by OpenPGP v4 spec
    key_id = fingerprint[-8:]

    secret_mpis = _mpi(d) + _mpi(p) + _mpi(q) + _mpi(u)
    checksum = sum(secret_mpis) & 0xFFFF
    sec_body = pub_body + b"\x00" + secret_mpis + struct.pack(">H", checksum)

    uid_bytes = uid.encode("utf-8")

    hashed_subs = b"".join(
        [
            _pgp_subpacket(33, b"\x04" + fingerprint),  # Issuer Fingerprint
            _pgp_subpacket(2, struct.pack(">I", timestamp)),  # Signature Creation Time
            _pgp_subpacket(27, b"\x03"),  # Key Flags: Certify (0x01) | Sign (0x02)
            _pgp_subpacket(21, b"\x08"),  # Preferred Hash: SHA-256 (8)
        ]
    )
    unhashed_subs = _pgp_subpacket(16, key_id)  # Issuer Key ID

    sig_header = struct.pack(">BBBBH", 4, 0x13, 1, 8, len(hashed_subs)) + hashed_subs
    trailer = b"\x04\xff" + struct.pack(">I", len(sig_header))
    uid_prefix = b"\xb4" + struct.pack(">I", len(uid_bytes)) + uid_bytes
    to_hash = fp_prefix + uid_prefix + sig_header + trailer
    digest = hashlib.sha256(to_hash).digest()

    sig_int = int.from_bytes(
        priv.sign(to_hash, padding.PKCS1v15(), hashes.SHA256()),
        "big",
    )
    sig_body = (
        sig_header
        + struct.pack(">H", len(unhashed_subs))
        + unhashed_subs
        + digest[:2]
        + _mpi(sig_int)
    )

    sec_ring = (
        _pgp_new_packet(5, sec_body)
        + _pgp_new_packet(13, uid_bytes)
        + _pgp_new_packet(2, sig_body)
    )
    pub_ring = (
        _pgp_new_packet(6, pub_body)
        + _pgp_new_packet(13, uid_bytes)
        + _pgp_new_packet(2, sig_body)
    )
    return (
        _armor("PGP PRIVATE KEY BLOCK", sec_ring),
        _armor("PGP PUBLIC KEY BLOCK", pub_ring),
    )


def extract_pem_certificates(pem_text: str) -> list[str]:
    """Split a PEM bundle into individual `-----BEGIN CERTIFICATE-----` blocks."""
    blocks: list[str] = []
    marker_begin = "-----BEGIN CERTIFICATE-----"
    marker_end = "-----END CERTIFICATE-----"
    pos = 0
    while True:
        start = pem_text.find(marker_begin, pos)
        if start == -1:
            break
        end = pem_text.find(marker_end, start)
        if end == -1:
            break
        end += len(marker_end)
        blocks.append(pem_text[start:end].strip() + "\n")
        pos = end
    return blocks


# ---------------------------------------------------------------------------
# Nexus REST API Client
# ---------------------------------------------------------------------------

class NexusApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"Nexus {method} {path} failed with HTTP {status}: {body[:400]}")


class NexusClient:
    def __init__(self, base_url: str, username: str = "admin", password: str = "", *, verify_tls: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.ssl_ctx = ssl.create_default_context()
        if not verify_tls:
            self.ssl_ctx.check_hostname = False
            self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        content_type: str = "application/json",
        auth: tuple[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        user, pw = auth if auth is not None else (self.username, self.password)
        if user and pw:
            token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"

        raw_body: bytes | None = None
        if body is not None:
            headers["Content-Type"] = content_type
            if content_type == "application/json":
                raw_body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                raw_body = body.encode("utf-8")
            elif isinstance(body, bytes):
                raw_body = body
            else:
                raise TypeError(f"Unsupported body type: {type(body)!r}")

        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=45) as resp:
                status = resp.status
                resp_bytes = resp.read()
        except urllib.error.HTTPError as err:
            with err:
                status = err.code
                resp_bytes = err.read()
        if status not in expected:
            raise NexusApiError(method, path, status, resp_bytes.decode("utf-8", errors="replace"))
        if not resp_bytes:
            return status, None
        text = resp_bytes.decode("utf-8", errors="replace")
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text


# ---------------------------------------------------------------------------
# Repository Payload Builders
# ---------------------------------------------------------------------------

def _proxy_common(remote_url: str, blob_store: str = "proxy-cache") -> dict[str, Any]:
    return {
        "online": True,
        "storage": {
            "blobStoreName": blob_store,
            "strictContentTypeValidation": True,
        },
        "proxy": {
            "remoteUrl": remote_url,
            "contentMaxAge": 1440,
            "metadataMaxAge": 1440,
        },
        "negativeCache": {
            "enabled": True,
            "timeToLive": 1440,
        },
        "httpClient": {
            "blocked": False,
            "autoBlock": True,
        },
    }


def _hosted_common(blob_store: str = "hosted", write_policy: str = "ALLOW") -> dict[str, Any]:
    return {
        "online": True,
        "storage": {
            "blobStoreName": blob_store,
            "strictContentTypeValidation": True,
            "writePolicy": write_policy,
        },
    }


def _group_common(members: list[str], blob_store: str = "hosted") -> dict[str, Any]:
    return {
        "online": True,
        "storage": {
            "blobStoreName": blob_store,
            "strictContentTypeValidation": True,
        },
        "group": {
            "memberNames": members,
        },
    }


def build_repository_specs(
    *,
    docker_group_port: int,
    docker_hosted_port: int,
    apt_signing_keypair: str,
) -> list[tuple[str, str, dict[str, Any], bool]]:
    """Return ordered `(api_format, repo_type, payload, optional_format)` definitions.

    Hosted and proxy repositories are ordered before the group repositories that reference them.
    """
    specs: list[tuple[str, str, dict[str, Any], bool]] = [
        # --- Docker / OCI ---
        (
            "docker",
            "hosted",
            {
                "name": "docker-hosted",
                **_hosted_common("hosted", "ALLOW"),
                "docker": {
                    "v1Enabled": False,
                    "forceBasicAuth": True,
                    "httpPort": int(docker_hosted_port),
                },
            },
            False,
        ),
        (
            "docker",
            "proxy",
            {
                "name": "docker-hub-proxy",
                **_proxy_common("https://registry-1.docker.io"),
                "docker": {"v1Enabled": False, "forceBasicAuth": True},
                "dockerProxy": {"indexType": "HUB", "cacheForeignLayers": True},
            },
            False,
        ),
        (
            "docker",
            "proxy",
            {
                "name": "ghcr-proxy",
                **_proxy_common("https://ghcr.io"),
                "docker": {"v1Enabled": False, "forceBasicAuth": True},
                "dockerProxy": {"indexType": "REGISTRY", "cacheForeignLayers": True},
            },
            False,
        ),
        (
            "docker",
            "proxy",
            {
                "name": "quay-proxy",
                **_proxy_common("https://quay.io"),
                "docker": {"v1Enabled": False, "forceBasicAuth": True},
                "dockerProxy": {"indexType": "REGISTRY", "cacheForeignLayers": True},
            },
            False,
        ),
        (
            "docker",
            "group",
            {
                "name": "docker-group",
                **_group_common(["docker-hosted", "docker-hub-proxy", "ghcr-proxy", "quay-proxy"]),
                "docker": {
                    "v1Enabled": False,
                    "forceBasicAuth": True,
                    "httpPort": int(docker_group_port),
                },
            },
            False,
        ),
        # --- Python (PyPI) ---
        (
            "pypi",
            "hosted",
            {"name": "pypi-hosted", **_hosted_common("hosted", "ALLOW")},
            False,
        ),
        (
            "pypi",
            "proxy",
            {"name": "pypi-proxy", **_proxy_common("https://pypi.org")},
            False,
        ),
        (
            "pypi",
            "group",
            {"name": "pypi-group", **_group_common(["pypi-hosted", "pypi-proxy"])},
            False,
        ),
        # --- Node.js (npm) ---
        (
            "npm",
            "hosted",
            {"name": "npm-hosted", **_hosted_common("hosted", "ALLOW")},
            False,
        ),
        (
            "npm",
            "proxy",
            {"name": "npm-proxy", **_proxy_common("https://registry.npmjs.org")},
            False,
        ),
        (
            "npm",
            "group",
            {"name": "npm-group", **_group_common(["npm-hosted", "npm-proxy"])},
            False,
        ),
        # --- Debian APT (hosted + upstream proxies) ---
        (
            "apt",
            "hosted",
            {
                "name": "apt-hosted",
                **_hosted_common("hosted", "ALLOW"),
                "apt": {"distribution": "sophon"},
                "aptSigning": {"keypair": apt_signing_keypair, "passphrase": ""},
            },
            False,
        ),
        (
            "apt",
            "proxy",
            {
                "name": "apt-debian-proxy",
                **_proxy_common("https://deb.debian.org/debian"),
                "apt": {"distribution": "trixie", "flat": False},
            },
            False,
        ),
        (
            "apt",
            "proxy",
            {
                "name": "apt-debian-security-proxy",
                **_proxy_common("https://security.debian.org/debian-security"),
                "apt": {"distribution": "trixie-security", "flat": False},
            },
            False,
        ),
        # --- Maven ---
        (
            "maven",
            "hosted",
            {
                "name": "maven-releases",
                **_hosted_common("hosted", "ALLOW_ONCE"),
                "maven": {"versionPolicy": "RELEASE", "layoutPolicy": "STRICT", "contentDisposition": "INLINE"},
            },
            False,
        ),
        (
            "maven",
            "hosted",
            {
                "name": "maven-snapshots",
                **_hosted_common("hosted", "ALLOW"),
                "maven": {"versionPolicy": "SNAPSHOT", "layoutPolicy": "STRICT", "contentDisposition": "INLINE"},
            },
            False,
        ),
        (
            "maven",
            "proxy",
            {
                "name": "maven-central",
                **_proxy_common("https://repo1.maven.org/maven2/"),
                "maven": {"versionPolicy": "RELEASE", "layoutPolicy": "PERMISSIVE"},
            },
            False,
        ),
        (
            "maven",
            "group",
            {
                "name": "maven-public",
                **_group_common(["maven-releases", "maven-snapshots", "maven-central"]),
            },
            False,
        ),
        # --- Rust (Cargo) ---
        (
            "cargo",
            "hosted",
            {"name": "cargo-hosted", **_hosted_common("hosted", "ALLOW")},
            True,
        ),
        (
            "cargo",
            "proxy",
            {"name": "crates-io-proxy", **_proxy_common("https://index.crates.io/")},
            True,
        ),
        (
            "cargo",
            "group",
            {"name": "cargo-group", **_group_common(["cargo-hosted", "crates-io-proxy"])},
            True,
        ),
        # --- Generic / Raw ---
        (
            "raw",
            "hosted",
            {
                "name": "raw-vault",
                **_hosted_common("hosted", "ALLOW_ONCE"),
                "raw": {"contentDisposition": "ATTACHMENT"},
            },
            False,
        ),
        (
            "raw",
            "hosted",
            {
                "name": "raw-hosted",
                **_hosted_common("hosted", "ALLOW"),
                "raw": {"contentDisposition": "ATTACHMENT"},
            },
            False,
        ),
        (
            "raw",
            "hosted",
            {
                "name": "raw-scratch",
                **_hosted_common("scratch", "ALLOW"),
                "raw": {"contentDisposition": "ATTACHMENT"},
            },
            False,
        ),
        (
            "raw",
            "group",
            {
                "name": "raw-group",
                **_group_common(["raw-vault", "raw-hosted", "raw-scratch"]),
                "raw": {"contentDisposition": "ATTACHMENT"},
            },
            False,
        ),
    ]
    return specs


# ---------------------------------------------------------------------------
# Convergence Steps
# ---------------------------------------------------------------------------

def wait_until_ready(client: NexusClient, timeout_sec: int = 300) -> None:
    deadline = time.monotonic() + timeout_sec
    last_err = "no response"
    while time.monotonic() < deadline:
        try:
            status, _ = client.request("GET", "/service/rest/v1/status", auth=("", ""), expected=(200,))
            if status == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(3)
    raise TimeoutError(f"Nexus REST API did not become ready within {timeout_sec}s: {last_err}")


def ensure_admin_password(
    client: NexusClient,
    admin_user: str,
    desired_password: str,
    initial_password: str = "",
) -> bool:
    """Ensure `admin_user` authenticates with `desired_password`. Returns True if rotated."""
    status, _ = client.request(
        "GET",
        f"/service/rest/v1/security/users?userId={urllib.parse.quote(admin_user)}",
        auth=(admin_user, desired_password),
        expected=(200, 401, 403),
    )
    if status == 200:
        client.username = admin_user
        client.password = desired_password
        return False

    candidates = [pw for pw in (initial_password.strip(), "admin123") if pw]
    for candidate in candidates:
        st, _ = client.request(
            "GET",
            f"/service/rest/v1/security/users?userId={urllib.parse.quote(admin_user)}",
            auth=(admin_user, candidate),
            expected=(200, 401, 403),
        )
        if st == 200:
            client.request(
                "PUT",
                f"/service/rest/v1/security/users/{urllib.parse.quote(admin_user)}/change-password",
                body=desired_password,
                content_type="text/plain",
                auth=(admin_user, candidate),
                expected=(200, 204),
            )
            client.username = admin_user
            client.password = desired_password
            return True

    raise RuntimeError(
        "Unable to authenticate to Nexus with either configured SOPHON_NEXUS_ADMIN_PASSWORD "
        "or /nexus-data/admin.password."
    )


def ensure_eula_accepted(client: NexusClient) -> bool:
    """Accept the Nexus 3.77+ Community Edition EULA if present and not yet accepted."""
    status, payload = client.request("GET", "/service/rest/v1/system/eula", expected=(200, 404))
    if status == 404 or not isinstance(payload, dict):
        return False
    if payload.get("accepted") is True:
        return False
    disclaimer = payload.get("disclaimer")
    if not isinstance(disclaimer, str) or not disclaimer:
        raise RuntimeError(f"Unexpected EULA payload from Nexus: {payload!r}")
    client.request(
        "POST",
        "/service/rest/v1/system/eula",
        body={"accepted": True, "disclaimer": disclaimer},
        expected=(200, 204),
    )
    return True


def ensure_anonymous_setting(client: NexusClient, enabled: bool) -> bool:
    _, current = client.request("GET", "/service/rest/v1/security/anonymous", expected=(200,))
    if isinstance(current, dict) and bool(current.get("enabled")) == bool(enabled):
        return False
    client.request(
        "PUT",
        "/service/rest/v1/security/anonymous",
        body={
            "enabled": bool(enabled),
            "userId": (current or {}).get("userId", "anonymous"),
            "realmName": (current or {}).get("realmName", "NexusAuthorizingRealm"),
        },
        expected=(200,),
    )
    return True


def ensure_truststore_certificates(client: NexusClient, pem_bundle: str) -> int:
    certs = extract_pem_certificates(pem_bundle)
    if not certs:
        return 0
    _, existing = client.request("GET", "/service/rest/v1/security/ssl/truststore", expected=(200,))
    existing_pems = {
        (item.get("pem") or "").strip()
        for item in (existing or [])
        if isinstance(item, dict)
    }
    added = 0
    for pem in certs:
        if pem.strip() in existing_pems:
            continue
        status, _ = client.request(
            "POST",
            "/service/rest/v1/security/ssl/truststore",
            body=pem,
            content_type="text/plain",
            expected=(200, 201, 400),
        )
        if status in (200, 201):
            added += 1
    return added


def ensure_ldap_server(client: NexusClient, ldap_cfg: dict[str, Any]) -> bool:
    name = ldap_cfg["name"]
    payload = {
        "name": name,
        "protocol": ldap_cfg.get("protocol", "ldaps"),
        "useTrustStore": bool(ldap_cfg.get("use_trust_store", True)),
        "host": ldap_cfg["host"],
        "port": int(ldap_cfg.get("port", 636)),
        "searchBase": ldap_cfg["search_base"],
        "authScheme": "SIMPLE",
        "authUsername": ldap_cfg["bind_dn"],
        "authPassword": ldap_cfg["bind_password"],
        "connectionTimeoutSeconds": 30,
        "connectionRetryDelaySeconds": 300,
        "maxIncidentsCount": 3,
        "userBaseDn": "ou=people",
        "userSubtree": True,
        "userObjectClass": "inetOrgPerson",
        "userLdapFilter": "",
        "userIdAttribute": "uid",
        "userRealNameAttribute": "cn",
        "userEmailAddressAttribute": "mail",
        "userPasswordAttribute": "",
        "ldapGroupsAsRoles": True,
        "groupType": "static",
        "groupBaseDn": "ou=groups",
        "groupSubtree": True,
        "groupObjectClass": "groupOfNames",
        "groupIdAttribute": "cn",
        "groupMemberAttribute": "member",
        "groupMemberFormat": "${dn}",
    }
    _, servers = client.request("GET", "/service/rest/v1/security/ldap", expected=(200,))
    existing = next((s for s in (servers or []) if isinstance(s, dict) and s.get("name") == name), None)
    if existing is None:
        client.request("POST", "/service/rest/v1/security/ldap", body=payload, expected=(201,))
        return True
    update_payload = dict(payload, id=existing["id"])
    client.request(
        "PUT",
        f"/service/rest/v1/security/ldap/{urllib.parse.quote(name)}",
        body=update_payload,
        expected=(204,),
    )
    return False


def ensure_roles_and_pull_user(
    client: NexusClient,
    *,
    pull_user: str,
    pull_password: str,
    domain: str,
) -> None:
    roles = [
        {
            "id": "nexus-admins",
            "name": "Sophon LDAP Nexus Admins",
            "description": "Maps OpenLDAP cn=nexus-admins group to Nexus administrator privileges",
            "privileges": [],
            "roles": ["nx-admin"],
        },
        {
            "id": "nexus-developers",
            "name": "Sophon LDAP Nexus Developers",
            "description": "Maps OpenLDAP cn=nexus-developers group to read/publish repository access",
            "privileges": [
                "nx-repository-view-*-*-browse",
                "nx-repository-view-*-*-read",
                "nx-repository-view-*-*-add",
                "nx-repository-view-*-*-edit",
                "nx-search-read",
                "nx-apikey-all",
            ],
            "roles": [],
        },
        {
            "id": "sophon-reader",
            "name": "Sophon Registry Pull Reader",
            "description": "Least-privilege read-only pull role for InfraVM Podman and Arcane",
            "privileges": [
                "nx-repository-view-*-*-browse",
                "nx-repository-view-*-*-read",
                "nx-search-read",
            ],
            "roles": [],
        },
    ]
    for role in roles:
        role_id = urllib.parse.quote(role["id"])
        status, _ = client.request(
            "GET",
            f"/service/rest/v1/security/roles/{role_id}?source=default",
            expected=(200, 404),
        )
        if status == 404:
            client.request("POST", "/service/rest/v1/security/roles", body=role, expected=(200, 201))
        else:
            client.request("PUT", f"/service/rest/v1/security/roles/{role_id}", body=role, expected=(204,))

    _, users = client.request(
        "GET",
        f"/service/rest/v1/security/users?source=default&userId={urllib.parse.quote(pull_user)}",
        expected=(200,),
    )
    existing_user = next(
        (u for u in (users or []) if isinstance(u, dict) and u.get("userId") == pull_user),
        None,
    )
    if existing_user is None:
        client.request(
            "POST",
            "/service/rest/v1/security/users",
            body={
                "userId": pull_user,
                "firstName": "Sophon",
                "lastName": "Pull",
                "emailAddress": f"{pull_user}@{domain}",
                "password": pull_password,
                "status": "active",
                "roles": ["sophon-reader"],
            },
            expected=(200, 201),
        )
    else:
        updated_user = dict(existing_user)
        updated_user["status"] = "active"
        updated_user["roles"] = ["sophon-reader"]
        updated_user["source"] = "default"
        client.request(
            "PUT",
            f"/service/rest/v1/security/users/{urllib.parse.quote(pull_user)}",
            body=updated_user,
            expected=(204,),
        )
        client.request(
            "PUT",
            f"/service/rest/v1/security/users/{urllib.parse.quote(pull_user)}/change-password",
            body=pull_password,
            content_type="text/plain",
            expected=(200, 204),
        )


def ensure_realms(client: NexusClient) -> bool:
    _, available_raw = client.request("GET", "/service/rest/v1/security/realms/available", expected=(200,))
    available_ids = {
        item["id"] for item in (available_raw or []) if isinstance(item, dict) and "id" in item
    }
    desired_order = [
        realm_id
        for realm_id in (
            "NexusAuthenticatingRealm",
            "NexusAuthorizingRealm",
            "LdapRealm",
            "DockerToken",
            "NpmToken",
        )
        if realm_id in available_ids
    ]
    _, current = client.request("GET", "/service/rest/v1/security/realms/active", expected=(200,))
    if list(current or []) == desired_order:
        return False
    client.request("PUT", "/service/rest/v1/security/realms/active", body=desired_order, expected=(204,))
    return True


def ensure_blobstores_and_purge_default(client: NexusClient) -> None:
    _, stores = client.request("GET", "/service/rest/v1/blobstores", expected=(200,))
    existing_names = {s.get("name") for s in (stores or []) if isinstance(s, dict)}
    for name, path in (
        ("hosted", "/nexus-data/blobs/hosted"),
        ("scratch", "/nexus-data/blobs/scratch"),
        ("proxy-cache", "/nexus-data/blobs/proxy-cache"),
    ):
        if name not in existing_names:
            client.request(
                "POST",
                "/service/rest/v1/blobstores/file",
                body={"name": name, "path": path},
                expected=(201, 204),
            )

    # Remove stock out-of-the-box repositories that write to the unbacked-up `default` blobstore
    # (groups first so member deletion is never blocked by group references).
    status, repos = client.request("GET", "/service/rest/v1/repositorySettings", expected=(200, 404))
    if status != 200 or not isinstance(repos, list):
        _, repos = client.request("GET", "/service/rest/v1/repositories", expected=(200,))
    default_repos = [
        r for r in (repos or [])
        if isinstance(r, dict) and (r.get("storage") or {}).get("blobStoreName") == "default"
    ]
    for target_type in ("group", "proxy", "hosted"):
        for repo in default_repos:
            if repo.get("type") == target_type:
                client.request(
                    "DELETE",
                    f"/service/rest/v1/repositories/{urllib.parse.quote(repo['name'])}",
                    expected=(204, 404),
                )

    if "default" in existing_names:
        client.request("DELETE", "/service/rest/v1/blobstores/default", expected=(204, 400, 404))


def ensure_repositories(
    client: NexusClient,
    specs: list[tuple[str, str, dict[str, Any], bool]],
) -> dict[str, str]:
    _, existing_raw = client.request("GET", "/service/rest/v1/repositories", expected=(200,))
    existing = {
        r["name"]: r for r in (existing_raw or []) if isinstance(r, dict) and "name" in r
    }
    outcomes: dict[str, str] = {}
    skipped_optional: set[str] = set()

    for api_format, repo_type, payload, optional_format in specs:
        name = payload["name"]
        if repo_type == "group":
            members = [m for m in payload["group"]["memberNames"] if m not in skipped_optional]
            if not members and optional_format:
                skipped_optional.add(name)
                outcomes[name] = "skipped"
                continue
            payload = dict(payload, group={"memberNames": members})

        base_path = f"/service/rest/v1/repositories/{api_format}/{repo_type}"
        if name not in existing:
            expected = (201, 400, 404) if optional_format else (201,)
            status, _ = client.request("POST", base_path, body=payload, expected=expected)
            if status == 201:
                outcomes[name] = "created"
            else:
                skipped_optional.add(name)
                outcomes[name] = "skipped"
        else:
            expected = (204, 400, 404) if optional_format else (204,)
            status, _ = client.request(
                "PUT",
                f"{base_path}/{urllib.parse.quote(name)}",
                body=payload,
                expected=expected,
            )
            outcomes[name] = "updated" if status == 204 else "skipped"

    return outcomes


def configure_all(cfg: dict[str, Any]) -> dict[str, Any]:
    client = NexusClient(cfg["base_url"], verify_tls=bool(cfg.get("verify_tls", False)))
    wait_until_ready(client, timeout_sec=int(cfg.get("timeout_sec", 300)))

    rotated_admin = ensure_admin_password(
        client,
        cfg.get("admin_user", "admin"),
        cfg["admin_password"],
        cfg.get("initial_admin_password", ""),
    )
    accepted_eula = ensure_eula_accepted(client)
    changed_anon = ensure_anonymous_setting(client, bool(cfg.get("anonymous_enabled", False)))
    imported_certs = ensure_truststore_certificates(client, cfg.get("ldap_ca_pem", ""))
    created_ldap = ensure_ldap_server(client, cfg["ldap"])
    ensure_roles_and_pull_user(
        client,
        pull_user=cfg.get("pull_user", "sophon-pull"),
        pull_password=cfg["pull_password"],
        domain=cfg["domain"],
    )
    changed_realms = ensure_realms(client)
    ensure_blobstores_and_purge_default(client)

    apt_keypair = cfg.get("apt_signing_keypair") or ""
    apt_public = cfg.get("apt_signing_public_key") or ""
    generated_apt_key = False
    if not apt_keypair.strip():
        apt_keypair, apt_public = generate_openpgp_rsa_keypair(f"Sophon APT Signing Key <apt@{cfg['domain']}>")
        generated_apt_key = True

    specs = build_repository_specs(
        docker_group_port=int(cfg.get("docker_group_port", 5000)),
        docker_hosted_port=int(cfg.get("docker_hosted_port", 5001)),
        apt_signing_keypair=apt_keypair,
    )
    repo_outcomes = ensure_repositories(client, specs)

    return {
        "rotated_admin": rotated_admin,
        "accepted_eula": accepted_eula,
        "changed_anonymous": changed_anon,
        "imported_certs": imported_certs,
        "created_ldap": created_ldap,
        "changed_realms": changed_realms,
        "generated_apt_key": generated_apt_key,
        "apt_signing_keypair": apt_keypair if generated_apt_key else "",
        "apt_signing_public_key": apt_public if generated_apt_key else "",
        "repositories": repo_outcomes,
    }


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("Expected JSON configuration on stdin.", file=sys.stderr)
        return 2
    cfg = json.loads(raw)
    result = configure_all(cfg)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
