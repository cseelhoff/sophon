#!/usr/bin/env python3
"""Render core IaC for the disposable, disconnected-integration M2 drill."""
import argparse
import hashlib
from pathlib import Path
import secrets
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_deployment import run_fixture


def main(output):
    variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
    roles = ("traefik", "openldap", "keycloak", "gitea", "coredns", "kopia")
    for role in roles:
        variables.update(yaml.safe_load((ROOT / f"roles/{role}/defaults/main.yml").read_text()))
    for name in variables:
        if any(part in name for part in ("password", "token", "secret", "private_key")):
            variables[name] = secrets.token_hex(18)
    digest = hashlib.sha256()
    for role in roles:
        for path in sorted((ROOT / "roles" / role).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    variables.update(domain_name="dev.invalid", infravm_ip="192.0.2.3", vnet_dns="1.1.1.1",
                     coredns_cf_token="", coredns_cf_account_id="", coredns_cf_zone_id="",
                     coredns_cf_tunnel_id="", traefik_acme_email="admin@dev.invalid",
                     traefik_acme_server="https://acme.invalid/directory", gitea_actions_enabled=False,
                     gitea_oauth_enabled=False, gitea_admin_user="drill-admin",
                     gitea_admin_email="drill@dev.invalid",
                     sophon_version="m2-dev-source:" + digest.hexdigest(),
                     kopia_volume_names=["SOPHON_M2_DATA", "SOPHON_M2_ACME"],
                     kopia_image="localhost/sophon-kopia:m2-dev")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    tasks = [{"ansible.builtin.template": {
        "src": str(ROOT / f"roles/{role}/templates/docker-compose.yml.j2"),
        "dest": str(output / f"{role}.yml"), "mode": "0600"}, "vars": variables}
        for role in ("kopia", "traefik", "gitea", "openldap", "keycloak")]
    result = run_fixture(output, tasks)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    (output / "engine-proxy.cfg").write_bytes((ROOT / "roles/kopia/files/engine-proxy.cfg").read_bytes())
    print("Rendered dev.invalid core fixtures: " + variables["sophon_version"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    main(parser.parse_args().output.resolve())
