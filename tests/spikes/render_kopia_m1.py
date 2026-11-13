#!/usr/bin/env python3
"""Render non-secret M1 dev fixtures using the repository's Ansible toolchain."""
import argparse
import hashlib
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_deployment import run_fixture


def main(output):
    variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
    for role in ("traefik", "openldap", "keycloak", "gitea", "coredns", "cloudflared", "kopia"):
        variables.update(yaml.safe_load((ROOT / f"roles/{role}/defaults/main.yml").read_text()))
    for name in variables:
        if any(part in name for part in ("password", "token", "secret", "private_key")):
            variables[name] = "M1_FIXTURE_ONLY"
    version = hashlib.sha256((ROOT / "roles/kopia/files/backup.py").read_bytes() +
                             (ROOT / "roles/kopia/files/runtime.py").read_bytes()).hexdigest()
    variables.update(domain_name="dev.invalid", infravm_ip="192.0.2.3", vnet_dns="1.1.1.1",
                     coredns_dns_port=53, coredns_cf_account_id="", coredns_cf_zone_id="",
                     coredns_cf_tunnel_id="", coredns_cf_token="", cloudflared_token="",
                     traefik_acme_email="admin@dev.invalid", traefik_acme_server="https://acme.invalid/directory",
                     gitea_actions_enabled=False,
                     sophon_version="m1-dev-source:" + version,
                     kopia_volume_names=["SOPHON_M1_DATA", "SOPHON_M1_ACME"],
                     kopia_image="localhost/sophon-kopia:m1-dev")
    output.mkdir(parents=True, exist_ok=True)
    tasks = [{"ansible.builtin.template": {
        "src": str(ROOT / f"roles/{role}/templates/docker-compose.yml.j2"),
        "dest": str(output / f"{role}.yml")}, "vars": variables}
        for role in ("kopia", "traefik", "gitea", "openldap")]
    result = run_fixture(output, tasks)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    (output / "engine-proxy.cfg").write_text((ROOT / "roles/kopia/files/engine-proxy.cfg").read_text())
    print("Rendered dev-only M1 fixtures: m1-dev-source:" + version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    main(parser.parse_args().output.resolve())
