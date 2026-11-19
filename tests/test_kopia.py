from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import struct
import unittest
from unittest.mock import patch

import yaml

from test_deployment import ROOT, run_fixture
from test_console import service_values


class KopiaRoleTests(unittest.TestCase):
    def test_optional_coarse_stack_has_no_engine_socket_and_readonly_btrfs_source(self):
        variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
        variables.update(yaml.safe_load((ROOT / "roles/kopia/defaults/main.yml").read_text()))
        variables.update(domain_name="dev.invalid", sophon_version="fixture", kopia_volume_names=[],
                         kopia_coarse_volumes_dir="/srv/podman/volumes", kopia_coarse_work_dir="/srv/coarse")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for enabled in (False, True):
                variables["kopia_coarse_enabled"] = enabled
                result = run_fixture(directory, [{"ansible.builtin.template": {
                    "src": str(ROOT / "roles/kopia/templates/docker-compose.yml.j2"),
                    "dest": str(directory / "compose.yml")}, "vars": variables}])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                compose = yaml.safe_load((directory / "compose.yml").read_text())
                services = compose["services"]
                self.assertNotIn("coarse-proxy", services)
                self.assertNotIn("coarse-engine", compose.get("networks", {}))
                if not enabled:
                    self.assertNotIn("coarse", services)
                    continue
                worker = services["coarse"]
                self.assertEqual(worker["image"], services["kopia"]["image"])
                self.assertEqual(worker["networks"], ["proxy"])
                self.assertEqual(worker["cap_add"], ["DAC_OVERRIDE"])
                self.assertNotIn("ENGINE_URL", worker["environment"])
                self.assertEqual(worker["volumes"][0], {
                    "type": "bind", "source": "/srv/podman/volumes", "target": "/volumes", "read_only": True})
                self.assertEqual(worker["volumes"][1]["source"], "/srv/coarse")
                self.assertEqual(worker["volumes"][2], "source-v3:/source:ro")
                self.assertFalse(any("podman.sock" in str(mount) for mount in worker["volumes"]))

    def test_configure_refuses_to_deploy_without_console_preflight(self):
        guard = yaml.safe_load((ROOT / "4-configure.yml").read_text())[0]["pre_tasks"][0]
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {"SOPHON_DEPLOYMENT_STEP": "configure", "SOPHON_PROXMOX_PASSWORD": "",
                             "SOPHON_VCENTER_PASSWORD": "", "SOPHON_KOPIA_PREFLIGHT_COMPLETE": ""}):
            result = run_fixture(Path(temporary), [guard])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete SFTP preflight before stack deployment", result.stdout + result.stderr)

    def test_collector_regressions(self):
        result = subprocess.run([sys.executable, str(ROOT / "tests/backup_checks.py")],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_kopia_runs_after_all_source_volumes_have_been_created(self):
        play = yaml.safe_load((ROOT / "4-configure.yml").read_text())[0]
        roles = [task["ansible.builtin.include_role"]["name"] for task in play["tasks"]
                 if "ansible.builtin.include_role" in task]
        self.assertEqual(roles[-1], "kopia")
        for name in ("traefik", "gitea", "openldap", "keycloak", "arcane"):
            self.assertLess(roles.index(name), roles.index("kopia"))

    def test_core_templates_use_concise_hot_backup_labels_without_stopping_containers(self):
        variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
        roles = ("traefik", "openldap", "keycloak", "gitea", "coredns", "cloudflared", "arcane", "kopia")
        for role in roles:
            variables.update(yaml.safe_load((ROOT / f"roles/{role}/defaults/main.yml").read_text()))
        variables.update(domain_name="dev.invalid", infravm_ip="192.0.2.3", vnet_dns="1.1.1.1",
                         coredns_dns_port=53, coredns_cf_account_id="", coredns_cf_zone_id="",
                         coredns_cf_tunnel_id="", cloudflared_token="dev-only", sophon_version="fixture")
        expected_enabled = {"traefik", "ldap", "keycloak-db", "gitea", "gitea-db"}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tasks = [{"ansible.builtin.template": {
                "src": str(ROOT / f"roles/{role}/templates/docker-compose.yml.j2"),
                "dest": str(directory / f"{role}.yml")}, "vars": variables} for role in roles]
            result = run_fixture(directory, tasks)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            seen_enabled = set()
            for role in roles:
                compose = yaml.safe_load((directory / f"{role}.yml").read_text())
                for name, service in compose["services"].items():
                    labels = service.get("labels", {})
                    if isinstance(labels, list):
                        labels = dict(label.split("=", 1) for label in labels)
                    self.assertNotIn("sophon.backup.stop", labels, name)
                    self.assertNotIn("sophon.backup.version", labels, name)
                    self.assertFalse(any(key.startswith(("sophon.restore.", "sophon.backup.covers.",
                                                        "sophon.backup.archive-", "sophon.backup.paths.")) for key in labels))
                    if labels.get("sophon.backup.enable") == "true":
                        seen_enabled.add(name)
                    else:
                        self.assertFalse(any(key.startswith("sophon.backup.") for key in labels), name)
                    if name in ("gitea-db", "keycloak-db", "ldap"):
                        self.assertNotIn("sophon.backup.paths", labels)
                        self.assertNotIn("sophon.backup.volumes", labels)
                        self.assertIn("sophon.backup.dump", labels)
                    if name == "gitea":
                        self.assertEqual(labels["sophon.backup.volumes"], "/data")
                        self.assertIn("git/lfs/tmp/", labels["sophon.backup.exclude"])
                        self.assertIn("/data/gitea/conf/app.ini:", labels["sophon.backup.ini"])
            self.assertEqual(seen_enabled, expected_enabled)

    def test_deployment_never_creates_a_collector_key_and_uses_locked_down_dump_proxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stacks = directory / "stacks"
            archive = directory / "image.tar"
            archive.write_bytes(b"fixture")
            calls = directory / "docker-calls.jsonl"
            state = directory / "unhealthy"
            docker = directory / "bin/docker"
            docker.parent.mkdir()
            docker.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                f"with open({str(calls)!r}, 'a') as out:\n"
                "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "args = sys.argv[1:]\n"
                "if 'inspect' in args:\n"
                f"    print('unhealthy' if pathlib.Path({str(state)!r}).exists() else 'healthy')\n"
                "elif 'exec' in args:\n"
                "    print(json.dumps({'status': 'complete', 'upload_status': 'complete', 'failures': []}))\n"
                "elif 'logs' in args:\n"
                "    sys.stderr.write('KOPIA_STARTUP_ERROR_JSON=' + json.dumps({\n"
                "        'message': 'invalid repository password FIXTURE_SECRET_VALUE fixture-private-key'}) + '\\n')\n"
            )
            docker.chmod(0o755)
            variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
            variables.update({name.removeprefix("SOPHON_").lower(): value
                              for name, value in service_values().items() if name.startswith("SOPHON_KOPIA_")})
            variables.update(sophon_version="fixture", domain_name="dev.invalid",
                             compose_stacks_dir=str(stacks),
                             compose_engine_host="unix:///fixture/podman.sock",
                             kopia_image_tars=[str(archive)])
            env = dict(os.environ, PATH=str(docker.parent) + os.pathsep + os.environ["PATH"],
                       ANSIBLE_ROLES_PATH=str(ROOT / "roles"))
            for attempt in range(2):
                if attempt:
                    (stacks / "kopia/.env").write_text("BACKUP_PORTAINER_API_KEY=LEGACY_FIXTURE_KEY\nUI_ONLY=bad\n")
                with patch.dict(os.environ, env, clear=True):
                    result = run_fixture(directory, [{"ansible.builtin.include_role": {
                        "name": str(ROOT / "roles/kopia")}, "vars": variables}])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("FIXTURE_SECRET", result.stdout + result.stderr)

                dotenv = (stacks / "kopia/.env").read_text()
                self.assertNotIn("PORTAINER", dotenv)
                self.assertNotIn("API_KEY", dotenv)
                self.assertNotIn("UI_ONLY", dotenv)
                compose = yaml.safe_load((stacks / "kopia/compose.yaml").read_text())
                kopia = compose["services"]["kopia"]
                proxy = compose["services"]["dump-proxy"]
                self.assertEqual(set(kopia["cap_add"]), {"DAC_OVERRIDE", "CHOWN", "FOWNER"})
                self.assertEqual(kopia["user"], "0:0")
                self.assertTrue(kopia["read_only"])
                self.assertFalse(any("podman.sock" in str(mount) for mount in kopia["volumes"]))
                self.assertIn({
                    "type": "bind",
                    "source": "/var/home/core/.local/share/containers/storage/volumes",
                    "target": "/volumes",
                    "read_only": True,
                }, kopia["volumes"])
                self.assertEqual(proxy["entrypoint"], ["python3", "/app/dump_proxy.py"])
                self.assertFalse(proxy.get("ports"))
                self.assertTrue(compose["networks"]["backup-engine"]["internal"])

            state.write_text("1")
            with patch.dict(os.environ, env, clear=True):
                result = run_fixture(directory, [{"ansible.builtin.include_role": {
                    "name": str(ROOT / "roles/kopia")}, "vars": variables}])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid repository password", result.stdout + result.stderr)
            self.assertNotIn("FIXTURE_SECRET", result.stdout + result.stderr)
            self.assertNotIn("fixture-private-key", result.stdout + result.stderr)

    def test_prepare_restore_and_tunnel_disable_templates_isolate_production_dns_and_certs(self):
        variables = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
        for role in ("coredns", "gitea", "arcane", "kopia"):
            variables.update(yaml.safe_load((ROOT / f"roles/{role}/defaults/main.yml").read_text()))
        variables.update(
            domain_name="dev.invalid",
            infravm_ip="192.0.2.3",
            vnet_dns="1.1.1.1",
            coredns_dns_port=53,
            coredns_cf_token="live-token-for-dns01-only",
            coredns_cf_account_id="acct-123",
            coredns_cf_zone_id="zone-123",
            coredns_cf_tunnel_id="tun-123",
            cf_tunnel_enabled=False,
            prepare_restore=True,
            kopia_readonly=True,
            sophon_version="fixture",
            kopia_volume_names=["gitea_gitea_data", "traefik_traefik_data"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tasks = [
                {"ansible.builtin.template": {
                    "src": str(ROOT / f"roles/{role}/templates/docker-compose.yml.j2"),
                    "dest": str(directory / f"{role}.yml")}, "vars": variables}
                for role in ("coredns", "gitea", "arcane", "kopia")
            ]
            # Verify cloudflared role skips completely when cf_tunnel_enabled is false.
            tasks.append({"ansible.builtin.include_role": {"name": str(ROOT / "roles/cloudflared")}, "vars": variables})
            # Verify traefik certificate validation guard skips during prepare_restore even with empty token.
            traefik_guard = next(
                task for task in yaml.safe_load((ROOT / "roles/traefik/tasks/main.yml").read_text())
                if task.get("name") == "Validate imported certificates"
            )
            tasks.append(dict(traefik_guard, vars=dict(variables, coredns_cf_token="", traefik_acme_replace_existing=False)))
            env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "roles"))
            with patch.dict(os.environ, env, clear=True):
                result = run_fixture(directory, tasks)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            coredns = yaml.safe_load((directory / "coredns.yml").read_text())["services"]["coredns"]
            coredns_env = dict(entry.split("=", 1) for entry in coredns["environment"])
            self.assertEqual(coredns_env["CF_TOKEN"], "")
            self.assertEqual(coredns_env["CF_ACCOUNT_ID"], "")
            self.assertEqual(coredns_env["CF_TUNNEL_ID"], "")
            self.assertEqual(coredns_env["CF_ZONE_ID"], "")

            gitea_services = yaml.safe_load((directory / "gitea.yml").read_text())["services"]
            self.assertEqual(set(gitea_services), {"gitea", "gitea-db", "gitea-init"})
            self.assertEqual(gitea_services["gitea"]["extra_hosts"], ["auth.dev.invalid:192.0.2.3"])
            self.assertEqual(gitea_services["gitea-init"]["extra_hosts"], ["auth.dev.invalid:192.0.2.3"])
            self.assertNotIn("gitea admin auth", "\n".join(gitea_services["gitea-init"]["command"]))

            arcane = yaml.safe_load((directory / "arcane.yml").read_text())["services"]["arcane"]
            self.assertEqual(arcane["extra_hosts"], ["auth.dev.invalid:192.0.2.3"])

            kopia = yaml.safe_load((directory / "kopia.yml").read_text())["services"]["kopia"]
            self.assertEqual(kopia["environment"]["KOPIA_READONLY"], "${KOPIA_READONLY:-false}")


if __name__ == "__main__":
    unittest.main()
