from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from test_deployment import ROOT, run_fixture
from test_console import service_values


class KopiaRoleTests(unittest.TestCase):
    def test_collector_regressions(self):
        result = subprocess.run([sys.executable, str(ROOT / "tests/backup_checks.py")],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_kopia_runs_after_all_source_volumes_have_been_created(self):
        play = yaml.safe_load((ROOT / "4-configure.yml").read_text())[0]
        roles = [task["ansible.builtin.include_role"]["name"] for task in play["tasks"]
                 if "ansible.builtin.include_role" in task]
        self.assertEqual(roles[-1], "kopia")
        for name in ("traefik", "gitea", "openldap", "keycloak"):
            self.assertLess(roles.index(name), roles.index("kopia"))

    def test_every_core_template_uses_v3_and_databases_only_native_exports(self):
        variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
        roles = ("traefik", "openldap", "keycloak", "gitea", "coredns", "cloudflared", "kopia")
        for role in roles:
            variables.update(yaml.safe_load((ROOT / f"roles/{role}/defaults/main.yml").read_text()))
        variables.update(domain_name="dev.invalid", infravm_ip="192.0.2.3", vnet_dns="1.1.1.1",
                         coredns_dns_port=53, coredns_cf_account_id="", coredns_cf_zone_id="",
                         coredns_cf_tunnel_id="", cloudflared_token="dev-only",
                         sophon_version="fixture", kopia_volume_names=["gitea_gitea_data", "traefik_traefik_data"])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tasks = [{"ansible.builtin.template": {
                "src": str(ROOT / f"roles/{role}/templates/docker-compose.yml.j2"),
                "dest": str(directory / f"{role}.yml")}, "vars": variables} for role in roles]
            result = run_fixture(directory, tasks)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for role in roles:
                compose = yaml.safe_load((directory / f"{role}.yml").read_text())
                for name, service in compose["services"].items():
                    labels = service.get("labels", {})
                    if isinstance(labels, list):
                        labels = dict(label.split("=", 1) for label in labels)
                    self.assertEqual(labels["sophon.backup.version"], "3", name)
                    self.assertFalse(any(key.startswith(("sophon.restore.", "sophon.backup.covers.",
                                                        "sophon.backup.archive-")) for key in labels))
                    if name in ("gitea-db", "keycloak-db", "ldap"):
                        self.assertFalse(any(key.startswith("sophon.backup.paths.") for key in labels))
                        self.assertTrue(any(key.startswith("sophon.backup.dumps.") for key in labels))

    def test_deployment_never_creates_a_collector_key_and_mounts_only_declared_volumes(self):
        requests, stack = [], None
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(("GET", self.path))
                if self.path == "/api/stacks":
                    self.respond([stack] if stack else [])
                elif self.path == "/api/endpoints":
                    self.respond([{"Id": 1, "Type": 1}])
                elif self.path.endswith("/containers/kopia/json"):
                    self.respond({"State": {"Health": {"Status": "healthy"}}})
                else:
                    self.send_error(404)

            def do_POST(self):
                nonlocal stack
                requests.append(("POST", self.path))
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/auth":
                    self.respond({"jwt": "FIXTURE_SECRET"})
                elif self.path.endswith("/images/load"):
                    self.respond({})
                elif "/stacks/create/" in self.path:
                    body = json.loads(data)
                    self.check(body)
                    stack = {"Id": 3, "Name": "kopia", "Env": body["env"]}
                    self.respond(stack)
                else:
                    self.send_error(404)

            def do_PUT(self):
                nonlocal stack
                requests.append(("PUT", self.path))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.check(body)
                stack["Env"] = body["env"]
                self.respond(stack)

            def check(self, body):
                env = {item["name"]: item["value"] for item in body["env"]}
                test.assertFalse(any("PORTAINER" in key or "API_KEY" in key for key in env))
                test.assertNotIn("UI_ONLY", env)
                compose = yaml.safe_load(body["stackFileContent"])
                kopia = compose["services"]["kopia"]
                proxy = compose["services"]["engine-proxy"]
                test.assertEqual(set(kopia["cap_add"]), {"DAC_OVERRIDE", "CHOWN", "FOWNER"})
                test.assertEqual(kopia["user"], "0:0")
                test.assertTrue(kopia["read_only"])
                test.assertFalse(any("podman.sock" in mount for mount in kopia["volumes"]))
                test.assertFalse(proxy.get("ports"))
                test.assertTrue(compose["networks"]["backup-engine"]["internal"])
                mounts = json.loads(kopia["environment"]["BACKUP_VOLUMES"])
                test.assertIn("gitea_gitea_data", mounts)
                test.assertIn("traefik_traefik_data", mounts)
                for name in mounts:
                    test.assertIn("/volumes/" + name + ":ro", "\n".join(kopia["volumes"]))

            def respond(self, value):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def log_message(self, *_):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                archive = directory / "image.tar"
                archive.write_bytes(b"fixture")
                variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
                variables.update({name.removeprefix("SOPHON_").lower(): value
                                  for name, value in service_values().items() if name.startswith("SOPHON_KOPIA_")})
                variables.update(kopia_extra_volumes=[], sophon_version="fixture", domain_name="dev.invalid",
                                 portainer_admin_password="FIXTURE_SECRET", portainer_stack_api_retries=1,
                                 portainer_stack_api_delay=0,
                                 portainer_stack_endpoint_id=1, portainer_stack_auth_token="FIXTURE_SECRET",
                                 infravm_portainer_url=f"http://127.0.0.1:{server.server_port}",
                                 kopia_image_tars=[str(archive), str(archive)])
                for attempt in range(2):
                    if attempt:
                        variables["kopia_extra_volumes"] = ["site-workload-data"]
                        stack["Env"].extend([{"name": "BACKUP_PORTAINER_API_KEY", "value": "LEGACY_FIXTURE_KEY"},
                                             {"name": "UI_ONLY", "value": "must-live-in-IaC"}])
                    with patch.dict(os.environ, {"ANSIBLE_ROLES_PATH": str(ROOT / "roles")}):
                        result = run_fixture(directory, [{"ansible.builtin.include_role": {
                            "name": str(ROOT / "roles/kopia")}, "vars": variables}])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("FIXTURE_SECRET", result.stdout + result.stderr)
                self.assertFalse(any("/tokens" in path or "/api/users" in path for _, path in requests))
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    unittest.main()
