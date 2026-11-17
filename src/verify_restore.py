"""Automated weekly disaster-recovery restore drill for Proxmox (`verify-restore`).

Creates or recreates an isolated drill InfraVM on Proxmox (`sophon-restore-drill`
by default), drives CoreOS installation, first-boot password setup, and login
unattended over `serial0` (`termproxy` WebSocket), stages step 4 with
`sophon-deploy --prepare-restore --run` (Cloudflare tunnel disabled, Kopia in
read-only standby against `sophon-restore`), restores the latest (or specified)
production snapshot via `sophon-restore --yes`, verifies restored TLS/OIDC/Git/
Arcane services both inside the guest and via direct-IP SNI, writes a JSON
verification report, and stops or destroys the drill VM cleanly.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import textwrap
import time

from cryptography import x509
from cryptography.x509.oid import NameOID

from deployment import FIELDS, step_fields
from serial_console import ProxmoxClient

SECRET_NAMES = frozenset(name for name, field in FIELDS.items() if field[2] == "password")


def as_bool(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")

INFRAVM_MARKER = "sophon-infravm-two-iso-v1"
PRODUCTION_VM_NAME = "sophon-infravm"
DEFAULT_DRILL_VM_NAME = "sophon-restore-drill"
SHELL_PROMPT_LITERAL = "__SOPHON_PROMPT__$ "
SHELL_PROMPT_PATTERN = r"__SOPHON_PROMPT__\$ "

REQUIRED_RESTORE_SECRETS = (
    "SOPHON_KEYCLOAK_ADMIN_PASSWORD",
    "SOPHON_KEYCLOAK_DB_PASSWORD",
    "SOPHON_KEYCLOAK_ARCANE_CLIENT_SECRET",
    "SOPHON_GITEA_ADMIN_PASSWORD",
    "SOPHON_OPENLDAP_ADMIN_PASSWORD",
    "SOPHON_OPENLDAP_EXAMPLE_USER_PASSWORD",
    "SOPHON_OPENLDAP_EXAMPLE_ADMIN_PASSWORD",
    "SOPHON_ARCANE_ADMIN_PASSWORD",
    "SOPHON_KOPIA_PASSWORD",
)


def parse_args(argv=None, environ=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="verify-restore",
        description="Provision an isolated Proxmox drill VM, restore from Kopia over serial0, and verify services.",
    )
    parser.add_argument(
        "--vm-name",
        default=env.get("SOPHON_VERIFY_RESTORE_VM_NAME") or DEFAULT_DRILL_VM_NAME,
        help=f"Proxmox VM name for the drill instance (default: {DEFAULT_DRILL_VM_NAME}; never {PRODUCTION_VM_NAME}).",
    )
    parser.add_argument(
        "--snapshot",
        default=env.get("SOPHON_VERIFY_RESTORE_SNAPSHOT") or "latest",
        help="Kopia snapshot ID to restore, or 'latest' (default: latest).",
    )
    parser.add_argument(
        "--teardown",
        choices=("stop", "destroy", "keep"),
        default=(env.get("SOPHON_VERIFY_RESTORE_TEARDOWN") or "stop").strip().lower(),
        help="Action after a passing restore drill: stop (default), destroy, or keep.",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Reuse an existing drill VM instead of destroying and recreating it before provisioning.",
    )
    parser.add_argument(
        "--skip-provision",
        action="store_true",
        help="Attach to an already-provisioned drill VM instead of running 2-provision.yml.",
    )
    parser.add_argument(
        "--skip-host-probes",
        action="store_true",
        default=as_bool(env.get("SOPHON_VERIFY_RESTORE_SKIP_HOST_PROBES", "false")),
        help="Skip direct-IP HTTPS SNI probes from the workstation (use when the drill VLAN is not host-routable).",
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=int(env.get("SOPHON_VERIFY_RESTORE_HTTPS_PORT") or "443"),
        help="HTTPS port for host-side SNI verification probes (default: 443).",
    )
    parser.add_argument(
        "--ca-file",
        default=env.get("SOPHON_VERIFY_RESTORE_CA_FILE") or "",
        help="Optional CA bundle PEM for host-side HTTPS certificate verification.",
    )
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        default=as_bool(env.get("SOPHON_VERIFY_RESTORE_INSECURE_TLS", "false")),
        help="Skip CA chain trust on host-side SNI probes (still rejects TRAEFIK DEFAULT CERT).",
    )
    parser.add_argument(
        "--network-cidr",
        default=env.get("SOPHON_VERIFY_RESTORE_NETWORK_CIDR") or "",
        help="Optional static IPv4/prefix for the drill VM (default: accept DHCP).",
    )
    parser.add_argument(
        "--gateway",
        default=env.get("SOPHON_VERIFY_RESTORE_GATEWAY") or "",
        help="Default gateway when --network-cidr is used.",
    )
    parser.add_argument(
        "--dns",
        default=env.get("SOPHON_VERIFY_RESTORE_DNS") or "",
        help="Upstream DNS server(s) when --network-cidr is used.",
    )
    parser.add_argument(
        "--report-file",
        default=env.get("SOPHON_VERIFY_RESTORE_REPORT_FILE") or "artifacts/verify-restore/latest.json",
        help="Write JSON verification report to this path (empty string disables file output).",
    )
    parser.add_argument(
        "--allow-readwrite-sftp-user",
        action="store_true",
        help="Override the safety check requiring SOPHON_KOPIA_SFTP_USERNAME=sophon-restore.",
    )
    return parser.parse_args(argv)


def _read_b64_file_if_needed(env: dict[str, str], b64_var: str, file_var: str) -> str:
    direct = (env.get(b64_var) or "").strip()
    if direct:
        return direct
    file_path = (env.get(file_var) or "").strip()
    if file_path:
        raw = Path(file_path).expanduser().read_bytes()
        return base64.b64encode(raw).decode("ascii")
    return ""


def build_config(argv=None, environ=None) -> dict:
    source_env = dict(os.environ if environ is None else environ)
    args = parse_args(argv, environ=source_env)

    vm_name = args.vm_name.strip()
    if not vm_name or vm_name == PRODUCTION_VM_NAME:
        raise ValueError(
            f"Refusing to run verify-restore against VM name {vm_name!r}; "
            f"{PRODUCTION_VM_NAME!r} is reserved for production."
        )

    pve_host = (source_env.get("SOPHON_PROXMOX_HOST") or "").strip()
    pve_port = int((source_env.get("SOPHON_PROXMOX_PORT") or FIELDS["SOPHON_PROXMOX_PORT"][3]).strip())
    pve_node = (source_env.get("SOPHON_PROXMOX_NODE") or "").strip()
    pve_password = source_env.get("SOPHON_PROXMOX_PASSWORD") or ""
    pve_user = (source_env.get("SOPHON_PROXMOX_USER") or "root@pam").strip()
    pve_validate_certs = as_bool(source_env.get("SOPHON_PROXMOX_VALIDATE_CERTS", "false"))
    missing_pve = [
        name
        for name, val in (
            ("SOPHON_PROXMOX_HOST", pve_host),
            ("SOPHON_PROXMOX_NODE", pve_node),
            ("SOPHON_PROXMOX_PASSWORD", pve_password),
        )
        if not val
    ]
    if missing_pve:
        raise ValueError(f"Missing required Proxmox settings for verify-restore: {', '.join(missing_pve)}")

    domain = (source_env.get("SOPHON_DOMAIN") or "").strip()
    if not domain:
        raise ValueError("SOPHON_DOMAIN is required for verify-restore.")

    sftp_user = (source_env.get("SOPHON_KOPIA_SFTP_USERNAME") or "sophon-restore").strip()
    if sftp_user != "sophon-restore" and not args.allow_readwrite_sftp_user:
        raise ValueError(
            f"Refusing SOPHON_KOPIA_SFTP_USERNAME={sftp_user!r}: weekly restore drills must use "
            "the confined read-only 'sophon-restore' account (pass --allow-readwrite-sftp-user only to override)."
        )

    sftp_host = (source_env.get("SOPHON_KOPIA_SFTP_HOST") or "").strip()
    if not sftp_host:
        raise ValueError("SOPHON_KOPIA_SFTP_HOST is required for verify-restore.")

    sftp_key_b64 = _read_b64_file_if_needed(
        source_env, "SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64", "SOPHON_VERIFY_RESTORE_SFTP_PRIVATE_KEY_FILE"
    )
    if not sftp_key_b64:
        raise ValueError(
            "SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64 (or SOPHON_VERIFY_RESTORE_SFTP_PRIVATE_KEY_FILE) is required."
        )
    sftp_known_hosts_b64 = _read_b64_file_if_needed(
        source_env, "SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64", "SOPHON_VERIFY_RESTORE_SFTP_KNOWN_HOSTS_FILE"
    )
    if not sftp_known_hosts_b64:
        raise ValueError(
            "SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64 (or SOPHON_VERIFY_RESTORE_SFTP_KNOWN_HOSTS_FILE) is required."
        )

    missing_secrets = [name for name in REQUIRED_RESTORE_SECRETS if not (source_env.get(name) or "").strip()]
    if missing_secrets:
        raise ValueError(
            "Missing required service/repository secrets needed to boot restored databases: "
            + ", ".join(missing_secrets)
        )

    if args.network_cidr and not args.gateway:
        raise ValueError("--gateway is required when --network-cidr is specified.")

    guest_env = {}
    for key in step_fields("configure"):
        val = source_env.get(key)
        if val is not None and val != "":
            guest_env[key] = val

    # Fill ephemeral Kopia UI password if unset; Gitea DB password falls back to Arcane admin password in group_vars.
    if not guest_env.get("SOPHON_KOPIA_SERVER_PASSWORD"):
        guest_env["SOPHON_KOPIA_SERVER_PASSWORD"] = secrets.token_urlsafe(24)

    # Hard safety overrides: never touch Cloudflare tunnels/DNS or mutate production Kopia backups.
    guest_env["SOPHON_DOMAIN"] = domain
    guest_env["SOPHON_CF_TOKEN"] = ""
    guest_env["SOPHON_CF_TUNNEL_ENABLED"] = "false"
    guest_env["SOPHON_CF_TUNNEL_NAME"] = ""
    guest_env["SOPHON_KOPIA_SFTP_HOST"] = sftp_host
    guest_env["SOPHON_KOPIA_SFTP_USERNAME"] = sftp_user
    guest_env["SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64"] = sftp_key_b64
    guest_env["SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64"] = sftp_known_hosts_b64
    guest_env["SOPHON_KOPIA_BACKUPS_ENABLED"] = "false"
    guest_env["SOPHON_KOPIA_COARSE_ENABLED"] = "false"
    guest_env["SOPHON_KOPIA_CREATE_REPOSITORY"] = "false"

    provision_env = dict(source_env)
    provision_env["SOPHON_HYPERVISOR"] = "proxmox"
    provision_env["SOPHON_INFRAVM_NAME"] = vm_name
    provision_env["SOPHON_PROXMOX_ONBOOT"] = "false"
    if source_env.get("SOPHON_VERIFY_RESTORE_VNET_INTERFACE"):
        provision_env["SOPHON_VNET_INTERFACE"] = source_env["SOPHON_VERIFY_RESTORE_VNET_INTERFACE"]
    if source_env.get("SOPHON_VERIFY_RESTORE_VNET_VLAN") is not None and source_env.get("SOPHON_VERIFY_RESTORE_VNET_VLAN") != "":
        provision_env["SOPHON_VNET_VLAN"] = source_env["SOPHON_VERIFY_RESTORE_VNET_VLAN"]

    core_password = source_env.get("SOPHON_VERIFY_RESTORE_CORE_PASSWORD") or (
        "Dr1ll-" + secrets.token_urlsafe(18)
    )

    return {
        "vm_name": vm_name,
        "snapshot": args.snapshot,
        "teardown": args.teardown,
        "recreate": not args.no_recreate,
        "skip_provision": args.skip_provision,
        "skip_host_probes": args.skip_host_probes,
        "https_port": args.https_port,
        "ca_file": args.ca_file or None,
        "validate_tls": not args.insecure_tls,
        "network_cidr": args.network_cidr,
        "gateway": args.gateway,
        "dns": args.dns,
        "report_file": args.report_file,
        "core_password": core_password,
        "proxmox": {
            "host": pve_host,
            "port": pve_port,
            "node": pve_node,
            "username": pve_user,
            "password": pve_password,
            "validate_certs": pve_validate_certs,
        },
        "domain": domain,
        "realm": guest_env.get("SOPHON_KEYCLOAK_REALM") or FIELDS["SOPHON_KEYCLOAK_REALM"][3],
        "guest_env": guest_env,
        "provision_env": provision_env,
    }


def run_provision_playbook(provision_env: dict[str, str]):
    cmd = [sys.executable, "src/deployment.py", "--step", "provision", "--", "2-provision.yml"]
    result = subprocess.run(cmd, env=provision_env, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Provisioning drill VM failed with exit code {result.returncode}.")


def _drive_boot_and_login(session, core_password: str, *, boot_timeout: float = 1200.0):
    """Drive CoreOS live install -> reboot -> first-boot password -> login -> clean bash prompt."""
    session.register_secret(core_password)
    deadline = time.monotonic() + boot_timeout
    password_entered = False

    while True:
        remaining = max(10.0, deadline - time.monotonic())
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for drill VM console login ({session.recent_output()!r})")

        idx, _, _ = session.expect(
            [
                r"Rebooting to the installed disk\.",
                r"New password for core:\s*",
                r"Confirm password:\s*",
                r"[Ll]ogin:\s*",
                r"Password:\s*",
                re.escape(SHELL_PROMPT_LITERAL),
                r"\[core@[^\]]+\]\$\s*",
            ],
            timeout=min(remaining, 600.0),
            reconnect=True,
            nudge_interval=15.0,
        )
        if idx == 0:
            # Installer finished; wait briefly for QEMU reboot / termproxy cycle, then continue expecting.
            time.sleep(2.0)
            continue
        if idx == 1:
            session.send_line(core_password, secret=True)
            continue
        if idx == 2:
            session.send_line(core_password, secret=True)
            password_entered = True
            continue
        if idx == 3:
            session.send_line("core")
            continue
        if idx == 4:
            session.send_line(core_password, secret=True)
            continue
        if idx == 5:
            return
        if idx == 6:
            session.send_line(
                f"HISTFILE=/dev/null; set +o history; stty -echo cols 240 rows 60 2>/dev/null || true; "
                f"export PS1={shlex.quote(SHELL_PROMPT_LITERAL)}"
            )
            session.expect([SHELL_PROMPT_PATTERN], timeout=30.0)
            return
        if password_entered:
            continue


def _build_deploy_command(cfg: dict, session) -> str:
    lines = []
    for key in sorted(cfg["guest_env"]):
        val = cfg["guest_env"][key]
        lines.append(f"{key}={shlex.quote(val)}")
        if key in SECRET_NAMES and val:
            session.register_secret(val)

    env_blob = ("\n".join(lines) + "\n").encode("utf-8")
    b64_text = base64.b64encode(env_blob).decode("ascii")
    session.register_secret(b64_text)
    wrapped_b64 = "\n".join(textwrap.wrap(b64_text, 76))
    for line in wrapped_b64.splitlines():
        session.register_secret(line)

    net_flags = ["--accept-dhcp"]
    if cfg["network_cidr"]:
        net_flags = ["--network-cidr", shlex.quote(cfg["network_cidr"]), "--gateway", shlex.quote(cfg["gateway"])]
        if cfg["dns"]:
            net_flags.extend(["--dns", shlex.quote(cfg["dns"])])

    deploy_cmd = f"sophon-deploy {' '.join(net_flags)} --prepare-restore --run"
    return (
        "(\n"
        "umask 077\n"
        "base64 -d > /run/user/1000/sophon-verify.env <<'__SOPHON_ENV_EOF__'\n"
        f"{wrapped_b64}\n"
        "__SOPHON_ENV_EOF__\n"
        "set -a\n"
        ". /run/user/1000/sophon-verify.env\n"
        "set +a\n"
        "rm -f /run/user/1000/sophon-verify.env\n"
        f"{deploy_cmd}\n"
        ")"
    )


def _guest_smoke_script(infravm_ip: str, domain: str, realm: str) -> str:
    return textwrap.dedent(
        f"""\
        python3 - <<'__SOPHON_SMOKE__'
        # sophon_smoke_check
        import http.client, json, socket, ssl, subprocess, sys

        ip = {infravm_ip!r}
        domain = {domain!r}
        realm = {realm!r}
        expected = ["coredns", "traefik", "ldap", "keycloak-db", "keycloak", "gitea-db", "gitea", "arcane", "kopia"]

        cf = subprocess.run(["podman", "--remote=false", "container", "exists", "cloudflared"])
        if cf.returncode == 0:
            raise SystemExit("cloudflared container must NOT exist on a verify-restore instance")

        for name in expected:
            out = subprocess.check_output(
                ["podman", "--remote=false", "inspect", "-f", "{{{{.State.Status}}}}", name],
                text=True,
            ).strip()
            if out != "running":
                raise SystemExit(f"container {{name}} is {{out!r}}, expected 'running'")

        kopia_env = json.loads(
            subprocess.check_output(
                ["podman", "--remote=false", "inspect", "-f", "{{{{json .Config.Env}}}}", "kopia"],
                text=True,
            )
        )
        if "KOPIA_READONLY=true" not in kopia_env or "KOPIA_BACKUPS_ENABLED=false" not in kopia_env:
            raise SystemExit(f"kopia container is not in read-only standby: {{kopia_env!r}}")

        def get_sni(host, path):
            ctx = ssl._create_unverified_context()
            raw = socket.create_connection((ip, 443), timeout=15)
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                if b"TRAEFIK DEFAULT CERT" in der or b"SOPHON PRE-RESTORE BOOTSTRAP" in der:
                    raise SystemExit(f"{{host}} served temporary/default certificate instead of restored certificate")
                conn = http.client.HTTPConnection(host, 443, timeout=15)
                conn.sock = tls
                conn.request("GET", path, headers={{"Host": host, "Connection": "close"}})
                resp = conn.getresponse()
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, body

        kc_status, kc_body = get_sni(f"auth.{{domain}}", f"/realms/{{realm}}/.well-known/openid-configuration")
        if kc_status != 200:
            raise SystemExit(f"Keycloak OIDC discovery returned HTTP {{kc_status}}")
        oidc = json.loads(kc_body)

        gt_status, gt_body = get_sni(f"git.{{domain}}", "/api/v1/version")
        if gt_status != 200:
            raise SystemExit(f"Gitea version API returned HTTP {{gt_status}}")
        gitea = json.loads(gt_body)

        ar_status, _ = get_sni(f"arcane.{{domain}}", "/")
        if ar_status not in (200, 301, 302, 303, 307, 308):
            raise SystemExit(f"Arcane root returned unexpected HTTP {{ar_status}}")

        print(json.dumps({{
            "containers_ok": True,
            "cloudflared_absent": True,
            "kopia_readonly": True,
            "oidc_issuer": oidc.get("issuer"),
            "gitea_version": gitea.get("version"),
            "arcane_status": ar_status,
        }}))
        __SOPHON_SMOKE__"""
    )


def probe_https_sni(
    ip: str,
    hostname: str,
    path: str,
    *,
    port: int = 443,
    validate_certs: bool = True,
    ca_file: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    """Connect directly to `(ip, port)` with TLS SNI `hostname` and return `(status, body, cert_cn)`."""
    if validate_certs:
        ctx = ssl.create_default_context(cafile=ca_file)
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    raw = socket.create_connection((ip, port), timeout=timeout)
    with ctx.wrap_socket(raw, server_hostname=hostname) as tls_sock:
        der_bytes = tls_sock.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der_bytes)
        cns = [attr.value for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
        issuer_cns = [attr.value for attr in cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)]
        cn_summary = ", ".join(cns)
        combined_upper = ", ".join(cns + issuer_cns).upper()
        if "TRAEFIK DEFAULT CERT" in combined_upper or "SOPHON PRE-RESTORE BOOTSTRAP" in combined_upper:
            raise RuntimeError(
                f"{hostname} ({ip}:{port}) presented {cn_summary!r} instead of the restored site certificate."
            )
        conn = http.client.HTTPConnection(hostname, port, timeout=timeout)
        conn.sock = tls_sock
        conn.request("GET", path, headers={"Host": hostname, "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body, cn_summary


def run_host_sni_probes(
    infravm_ip: str,
    domain: str,
    realm: str,
    *,
    port: int = 443,
    validate_certs: bool = True,
    ca_file: str | None = None,
) -> dict:
    kc_host = f"auth.{domain}"
    kc_status, kc_body, cert_cn = probe_https_sni(
        infravm_ip,
        kc_host,
        f"/realms/{realm}/.well-known/openid-configuration",
        port=port,
        validate_certs=validate_certs,
        ca_file=ca_file,
    )
    if kc_status != 200:
        raise RuntimeError(f"Host SNI probe to {kc_host} returned HTTP {kc_status}")
    kc_issuer = json.loads(kc_body).get("issuer")
    expected_issuer = f"https://{kc_host}/realms/{realm}"
    if kc_issuer != expected_issuer:
        raise RuntimeError(f"Unexpected Keycloak issuer {kc_issuer!r} (expected {expected_issuer!r})")

    gt_host = f"git.{domain}"
    gt_status, gt_body, _ = probe_https_sni(
        infravm_ip,
        gt_host,
        "/api/v1/version",
        port=port,
        validate_certs=validate_certs,
        ca_file=ca_file,
    )
    if gt_status != 200:
        raise RuntimeError(f"Host SNI probe to {gt_host} returned HTTP {gt_status}")
    gt_version = json.loads(gt_body).get("version")

    ar_host = f"arcane.{domain}"
    ar_status, _, _ = probe_https_sni(
        infravm_ip,
        ar_host,
        "/",
        port=port,
        validate_certs=validate_certs,
        ca_file=ca_file,
    )
    if ar_status not in (200, 301, 302, 303, 307, 308):
        raise RuntimeError(f"Host SNI probe to {ar_host} returned unexpected HTTP {ar_status}")

    return {
        "certificate_cn": cert_cn,
        "keycloak_issuer": kc_issuer,
        "gitea_version": gt_version,
        "arcane_status": ar_status,
    }


def _parse_restored_snapshot(restore_output: str, fallback: str) -> str:
    match = re.search(r"^Snapshot\s*:\s*(\S+)", restore_output, re.MULTILINE)
    return match.group(1) if match else fallback


def run_drill(cfg: dict) -> dict:
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pve = ProxmoxClient(
        cfg["proxmox"]["host"],
        port=cfg["proxmox"]["port"],
        user=cfg["proxmox"]["username"],
        password=cfg["proxmox"]["password"],
        validate_certs=cfg["proxmox"]["validate_certs"],
    )
    pve.authenticate()
    node = cfg["proxmox"]["node"]
    vm_name = cfg["vm_name"]

    existing = pve.find_vm(node, vm_name)
    if existing and cfg["recreate"] and not cfg["skip_provision"]:
        print(f"[verify-restore] Destroying previous drill VM {vm_name} (vmid={existing['vmid']})...")
        pve.destroy_vm(node, int(existing["vmid"]), expected_marker=INFRAVM_MARKER)

    if not cfg["skip_provision"]:
        print(f"[verify-restore] Provisioning fresh drill VM {vm_name} on Proxmox node {node}...")
        run_provision_playbook(cfg["provision_env"])

    created = pve.find_vm(node, vm_name)
    if not created:
        raise RuntimeError(f"Drill VM {vm_name!r} was not found on Proxmox node {node!r} after provisioning.")
    vmid = int(created["vmid"])

    print(f"[verify-restore] Attaching to serial0 on {vm_name} (vmid={vmid})...")
    with pve.open_serial(node, vmid) as session:
        _drive_boot_and_login(session, cfg["core_password"])
        print("[verify-restore] Running sophon-deploy --prepare-restore --run over serial0...")
        deploy_cmd = _build_deploy_command(cfg, session)
        session.run_checked(deploy_cmd, timeout=2400.0, prompt_pattern=SHELL_PROMPT_PATTERN)

        infravm_ip = session.run_checked(
            "jq -r .SOPHON_INFRAVM_IP /var/lib/sophon/project/site.json",
            timeout=30.0,
            prompt_pattern=SHELL_PROMPT_PATTERN,
        ).splitlines()[-1].strip()

        print(f"[verify-restore] Running sophon-restore --snapshot {cfg['snapshot']} --yes (IP {infravm_ip})...")
        restore_out = session.run_checked(
            f"sophon-restore --snapshot {shlex.quote(cfg['snapshot'])} --yes",
            timeout=2400.0,
            prompt_pattern=SHELL_PROMPT_PATTERN,
        )
        restored_snapshot = _parse_restored_snapshot(restore_out, cfg["snapshot"])

        print("[verify-restore] Running in-guest container, standby, and TLS/OIDC smoke checks...")
        guest_raw = session.run_checked(
            _guest_smoke_script(infravm_ip, cfg["domain"], cfg["realm"]),
            timeout=180.0,
            prompt_pattern=SHELL_PROMPT_PATTERN,
        )
        guest_checks = json.loads(guest_raw.splitlines()[-1])

    host_checks = None
    if not cfg["skip_host_probes"]:
        print(f"[verify-restore] Running host-side direct-IP SNI probes against {infravm_ip}:{cfg['https_port']}...")
        host_checks = run_host_sni_probes(
            infravm_ip,
            cfg["domain"],
            cfg["realm"],
            port=cfg["https_port"],
            validate_certs=cfg["validate_tls"],
            ca_file=cfg["ca_file"],
        )

    if cfg["teardown"] == "stop":
        print(f"[verify-restore] Disabling onboot and stopping verified drill VM {vm_name} (vmid={vmid})...")
        pve.set_onboot(node, vmid, False, expected_marker=INFRAVM_MARKER)
        pve.stop_vm(node, vmid, expected_marker=INFRAVM_MARKER)
    elif cfg["teardown"] == "destroy":
        print(f"[verify-restore] Destroying verified drill VM {vm_name} (vmid={vmid})...")
        pve.destroy_vm(node, vmid, expected_marker=INFRAVM_MARKER)

    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        "status": "verified",
        "started_at": started_at,
        "finished_at": finished_at,
        "vm_name": vm_name,
        "vmid": vmid,
        "node": node,
        "infravm_ip": infravm_ip,
        "domain": cfg["domain"],
        "snapshot": restored_snapshot,
        "cloudflare_tunnel_enabled": False,
        "kopia_sftp_username": cfg["guest_env"]["SOPHON_KOPIA_SFTP_USERNAME"],
        "guest_checks": guest_checks,
        "host_checks": host_checks,
        "teardown": cfg["teardown"],
    }

    if cfg["report_file"]:
        report_path = Path(cfg["report_file"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"[verify-restore] Restore verification PASSED for snapshot {restored_snapshot} on {vm_name} ({infravm_ip}).")
    return report


def main(argv=None) -> int:
    try:
        cfg = build_config(argv)
        run_drill(cfg)
    except Exception as exc:
        print(f"verify-restore: ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
