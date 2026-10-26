"""Offline Controller fixtures: python3 tests/test_controller.py (stdlib + PyYAML)."""
import contextlib
import ast
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import uuid
from urllib.parse import parse_qs, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/controller"
FILES = ROLE / "files"
IMPORT = (FILES / "sophon-import").read_text()
MARKER = "sophon-controller-v1"
spec = importlib.util.spec_from_file_location("controller_upload", FILES / "upload.py")
UPLOAD = importlib.util.module_from_spec(spec)
with patch.object(sys, "dont_write_bytecode", True):
    spec.loader.exec_module(UPLOAD)


@contextlib.contextmanager
def fixture():
    directory = ROOT / "tests" / (".controller-fixture-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def tasks(document):
    for task in document:
        yield task
        for key in ("block", "rescue", "always"):
            yield from tasks(task.get(key, []))


def verify_bundle(directory):
    code = IMPORT.split('python3 - "$media" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
    with patch.object(sys, "argv", ["verify", str(directory)]):
        exec(compile(code, str(FILES / "sophon-import"), "exec"), {})


def bundle(directory):
    project = directory / "project"
    (project / "artifacts/containers").mkdir(parents=True)
    for path, content in {
        "runner.tar": b"runner archive",
        "project/deployment.py": b"print('fixture')",
        "project/site.yml": b"---\n[]",
        "project/site.json": b'{"SOPHON_NFS_IP":"10.0.60.2"}',
        "project/artifacts/containers/portainer.tar": b"portainer archive",
    }.items():
        (directory / path).write_bytes(content)
    manifest(directory)


def manifest(directory):
    paths = [directory / "runner.tar"] + sorted((directory / "project").rglob("*"))
    (directory / "SHA256SUMS").write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  ./" +
        path.relative_to(directory).as_posix() + "\n"
        for path in paths if path.is_file()))


class ControllerTests(unittest.TestCase):
    def test_role_defaults_and_api_only_contract(self):
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertEqual(defaults["controller_vm_name"], "sophon-controller")
        self.assertEqual(defaults["controller_marker"], MARKER)
        self.assertEqual(defaults["controller_data_disk_size"], "100G")
        self.assertEqual(defaults["controller_os_disk_size"], "20G")
        self.assertEqual(defaults["controller_cores"], 4)
        self.assertEqual(defaults["controller_memory"], 4096)
        self.assertIn("SOPHON_CONTROLLER_SSH_PUBLIC_KEY", defaults["controller_ssh_public_key"])
        self.assertIn("SOPHON_CONTROLLER_PASSWORD", defaults["controller_console_password"])
        document = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        flattened = list(tasks(document))
        for task in flattened:
            self.assertNotIn("proxmox_shell", task)
            self.assertNotIn("ansible.builtin.shell", task)
            self.assertNotIn("community.proxmox.proxmox_kvm", task)
            include = task.get("ansible.builtin.include_role", {})
            self.assertNotEqual(include.get("name"), "nfs")
            uri = task.get("ansible.builtin.uri", {})
            if uri:
                self.assertTrue(uri["url"].startswith("{{ proxmox_api }}"))
                self.assertIn("validate_certs", uri)
        names = [task["name"] for task in flattened]
        legacy = names.index("Refuse legacy deployments and ambiguous Controller ownership before any mutation")
        create = names.index("Create marked Controller with dedicated blank data disk and offline ISO")
        configure = names.index("Configure operator login and persistent network through QGA only")
        self.assertLess(legacy, create)
        self.assertLess(legacy, configure)
        self.assertLess(names.index("Require matching Controller guest marker"), configure)
        login = flattened[configure]
        self.assertTrue(login["no_log"])
        self.assertTrue(login["ansible.builtin.include_role"]["apply"]["no_log"])
        self.assertEqual(login["ansible.builtin.include_role"]["tasks_from"], "qga_exec")
        create_body = flattened[create]["ansible.builtin.uri"]["body"]
        self.assertIn("model=virtio,bridge=", create_body["net0"])
        self.assertIn("if ", create_body["net0"])
        self.assertEqual(create_body["description"], "{{ controller_marker }}")
        self.assertNotIn("password", json.dumps(create_body))
        self.assertIn("media=cdrom", create_body["ide2"])
        upload = yaml.safe_load((ROLE / "tasks/upload.yml").read_text())
        self.assertTrue(upload[0]["no_log"])
        self.assertIn("controller_upload_source", upload[0]["ansible.builtin.command"]["stdin"])
        self.assertIn("controller_upload_filename", upload[0]["ansible.builtin.command"]["stdin"])
        self.assertEqual(upload[1]["ansible.builtin.include_tasks"], "wait_task.yml")
        wait = yaml.safe_load((ROLE / "tasks/wait_task.yml").read_text())
        self.assertIn("exitstatus == 'OK'", wait[-1]["ansible.builtin.assert"]["that"])

    def test_upload_uses_stdin_validates_json_and_upid(self):
        config = {
            "url": "https://pve:8006/api2/json/nodes/pve/storage/local/upload",
            "ticket": "TICKET-secret", "csrf": "CSRF-secret",
            "path": '/home/operator/image space;"quoted".qcow2',
            "filename": "controller.qcow2.iso", "validate_certs": True,
        }
        with patch.object(UPLOAD.subprocess, "run") as run:
            run.return_value.stdout = '{"data":"UPID:pve:001:upload:"}'
            self.assertEqual(UPLOAD.upload(config), "UPID:pve:001:upload:")
            args, kwargs = run.call_args
            self.assertNotIn(config["ticket"], repr(args))
            self.assertNotIn(config["csrf"], repr(args))
            self.assertIn("--config", args[0])
            self.assertIn("Cookie: PVEAuthCookie=TICKET-secret", kwargs["input"])
            self.assertIn("CSRFPreventionToken: CSRF-secret", kwargs["input"])
            self.assertNotIn("\ninsecure", kwargs["input"])
            self.assertIn('form = "filename=@', kwargs["input"])
            self.assertTrue(kwargs["check"])
            for invalid in ("<html>failure</html>", '{"data":null}', '{"data":"not-upid"}', "[]"):
                run.return_value.stdout = invalid
                with self.assertRaises(ValueError):
                    UPLOAD.upload(config)
            run.return_value.stdout = '{"data":"UPID:pve:001:upload:"}'
            UPLOAD.upload(dict(config, validate_certs=False))
            self.assertIn("\ninsecure\n", run.call_args.kwargs["input"])
        for invalid in ("bad\npath", "bad\rpath", "bad\0path"):
            with self.assertRaises(ValueError):
                UPLOAD.upload(dict(config, path=invalid))
        with self.assertRaises(ValueError):
            UPLOAD.upload(dict(config, filename="../escape.iso"))

    def test_manifest_checks_full_bundle_and_paths(self):
        with fixture() as directory:
            bundle(directory)
            verify_bundle(directory)
            (directory / "runner.tar").write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                verify_bundle(directory)
            manifest(directory)
            (directory / "project/unchecked").write_text("unchecked")
            with self.assertRaisesRegex(ValueError, "entire bundle"):
                verify_bundle(directory)
            manifest(directory)
            lines = (directory / "SHA256SUMS").read_text()
            for invalid in ("../outside", "/etc/passwd", "project/../../outside"):
                (directory / "SHA256SUMS").write_text(lines + "0" * 64 + "  " + invalid + "\n")
                with self.assertRaisesRegex(ValueError, "Unsafe checksum path"):
                    verify_bundle(directory)
            (directory / "SHA256SUMS").write_text(lines + lines.splitlines()[0] + "\n")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                verify_bundle(directory)
            (directory / "SHA256SUMS").write_text(lines)
            (directory / "project/link").symlink_to("site.json")
            with self.assertRaisesRegex(ValueError, "Unsupported bundle entry"):
                verify_bundle(directory)
            (directory / "project/link").unlink()
            (directory / "runner.tar").unlink()
            (directory / "runner.tar").symlink_to("project/site.json")
            with self.assertRaisesRegex(ValueError, "Unsupported bundle entry"):
                verify_bundle(directory)
            (directory / "runner.tar").unlink()
            (directory / "runner.tar").write_bytes(b"runner")
            (directory / "project/artifacts/kopia").mkdir()
            (directory / "project/artifacts/kopia/id_ed25519").write_text("private key")
            manifest(directory)
            with self.assertRaisesRegex(ValueError, "must never be distributed"):
                verify_bundle(directory)

    def test_shell_syntax_and_proot_setup_contract(self):
        for path in [FILES / name for name in (
            "controller-setup.sh", "sophon-controller", "sophon-import",
            "sophon-import.initd", "sophon-podman.initd")]:
            for shell in ("sh", "bash"):
                result = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        setup = (FILES / "controller-setup.sh").read_text()
        self.assertNotIn("rc-service ", setup)
        self.assertNotIn("mkfs", setup)
        self.assertIn("passwd -l root", setup)
        self.assertIn("/etc/doas.conf", setup)
        self.assertIn("cmd /usr/local/bin/sophon-controller", setup)
        self.assertIn("permit nopass setenv { ", setup)
        self.assertNotIn("keepenv", setup)
        fields = next(node.value for node in ast.parse((ROOT / "deployment.py").read_text()).body
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == "FIELDS" for target in node.targets))
        fields = ast.literal_eval(fields)
        expected = {name for name, field in fields.items()
                    if field[1] not in ("Proxmox", "Network", "VM")}
        expected.update((
            "SOPHON_VNET_GATEWAY", "SOPHON_VNET_CIDR", "SOPHON_NFS_IP",
            "SOPHON_KOPIA_ADMIN_PASSWORD", "SOPHON_KOPIA_REPOSITORY_PASSWORD",
        ))
        allowed = (FILES / "service-env").read_text().split()
        self.assertEqual(set(allowed), expected)
        self.assertEqual(len(allowed), len(set(allowed)))
        self.assertIn("rc-update add cgroups boot", setup)
        self.assertIn('graphroot = "/export/containers-storage"', setup)
        self.assertIn("ForceCommand internal-sftp", setup)
        for yaml_path in (ROLE / "tasks").glob("*.yml"):
            yaml.safe_load(yaml_path.read_text())

    def test_configure_template_credential_transport_and_fresh_only_permission(self):
        template = (ROLE / "templates/configure.sh.j2").read_text()
        self.assertIn("controller_console_password", template)
        self.assertIn("| b64encode | quote", template)
        self.assertNotIn("proxmox_password", template)
        self.assertNotIn("root:", template)
        grant = template.split("{% if controller_existing | length == 0 %}", 1)[1].split("{% endif %}", 1)[0]
        self.assertIn("allow-data-format", grant)
        self.assertEqual(template.count("allow-data-format"), 1)
        self.assertIn("rc-service sophon-import restart", template)
        # Render the template's finite set of shell expressions without requiring
        # Jinja/Ansible. Deliberately hostile credentials must remain data.
        import base64
        import shlex
        password = "p'a$$; $(touch never) `id` ü"
        public = "ssh-ed25519 AAAAC3Nzfixture operator's key"
        replacements = {
            "controller_marker | quote": shlex.quote(MARKER),
            "controller_ssh_public_key | b64encode | quote":
                shlex.quote(base64.b64encode(public.encode()).decode()),
            "('operator:' ~ controller_console_password ~ '\\n') | b64encode | quote":
                shlex.quote(base64.b64encode(("operator:" + password + "\n").encode()).decode()),
            "nfs_ip | quote": shlex.quote("10.0.60.2"),
            "(nfs_ip ~ '/' ~ (vnet_cidr | string)) | quote": shlex.quote("10.0.60.2/24"),
            "portainer_image_tarname | quote": shlex.quote("portainer.tar"),
            "('  address ' ~ nfs_ip ~ '/' ~ (vnet_cidr | string)) | quote":
                shlex.quote("  address 10.0.60.2/24"),
            "('  gateway ' ~ vnet_gateway) | quote": shlex.quote("  gateway 10.0.60.1"),
            "('nameserver ' ~ controller_dns) | quote": shlex.quote("nameserver 10.0.60.1"),
            "vnet_gateway | quote": shlex.quote("10.0.60.1"),
        }
        for existing in (False, True):
            rendered = re.sub(
                r"{% if controller_existing \| length == 0 %}(.*?){% endif %}",
                lambda match: "" if existing else match[1], template, flags=re.S)
            for expression, value in replacements.items():
                rendered = rendered.replace("{{ " + expression + " }}", value)
            self.assertNotIn("{{", rendered)
            self.assertNotIn(password, rendered)
            self.assertTrue(rendered.isascii())
            self.assertEqual("allow-data-format" in rendered, not existing)
            result = subprocess.run(["sh", "-n"], input=rendered, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_runner_wrapper_default_cli_and_environment(self):
        with fixture() as directory:
            bindir = directory / "bin"
            bindir.mkdir()
            output = directory / "output.json"
            for name, code in {
                "id": 'print("0")',
                "mountpoint": "pass",
                "podman": "import json, os, sys\n" +
                    f"open({str(output)!r}, 'w').write(json.dumps([sys.argv[1:], dict(os.environ)]))",
            }.items():
                path = bindir / name
                path.write_text(f"#!{sys.executable}\n{code}\n")
                path.chmod(0o755)
            ready = directory / "controller-ready"
            ready.touch()
            site = directory / "site.json"
            site.write_text(json.dumps({"SOPHON_INFRAVM_IP": "10.0.60.3"}))
            script = (FILES / "sophon-controller").read_text()
            script = script.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", "PATH=" + shlex.quote(str(bindir) + ":" + os.environ["PATH"]))
            script = script.replace("/var/lib/sophon/controller-ready", str(ready))
            script = script.replace("/export/sophon/site.json", str(site))
            script = script.replace("/usr/local/share/sophon-controller/service-env", str(FILES / "service-env"))
            wrapper = directory / "wrapper"
            wrapper.write_text(script)
            for extra in ([], ["--help"], ["--phase", "services", "--", "site.yml"]):
                result = subprocess.run(
                    ["sh", str(wrapper)] + extra, capture_output=True, text=True,
                    env=dict(os.environ, SOPHON_PROXMOX_PASSWORD="must-not-inherit",
                             SOPHON_CONTROLLER_PASSWORD="not-allowed", UNRELATED_SECRET="no",
                             SOPHON_CF_TOKEN="fixture cloudflare ' token",
                             SOPHON_KOPIA_ADMIN_PASSWORD="", SOPHON_DEPLOYMENT_PHASE="bootstrap"))
                self.assertEqual(result.returncode, 0, result.stderr)
                args, environment = json.loads(output.read_text())
                self.assertIn("--pull", args)
                self.assertEqual(args[args.index("--pull") + 1], "never")
                self.assertEqual(args[args.index("--network") + 1], "host")
                self.assertEqual(args[args.index("--volume") + 1], "/export/sophon:/workspace")
                self.assertIn("SOPHON_DEPLOYMENT_PHASE=services", args)
                self.assertFalse(any(arg.startswith("--add-host") for arg in args))
                self.assertNotIn("--privileged", args)
                self.assertNotIn("SOPHON_PROXMOX_PASSWORD", environment)
                self.assertNotIn("SOPHON_CONTROLLER_PASSWORD", environment)
                self.assertNotIn("SOPHON_DEPLOYMENT_PHASE", environment)
                self.assertNotIn("UNRELATED_SECRET", environment)
                self.assertEqual(environment["SOPHON_CF_TOKEN"], "fixture cloudflare ' token")
                self.assertEqual(environment["SOPHON_KOPIA_ADMIN_PASSWORD"], "")
                self.assertIn("SOPHON_CF_TOKEN", args)
                self.assertNotIn("fixture cloudflare ' token", repr(args))
                self.assertNotIn("SOPHON_KEYCLOAK_ADMIN_PASSWORD", environment)
                self.assertNotIn("SOPHON_KEYCLOAK_ADMIN_PASSWORD", args)
                expected = extra or ["--phase", "services", "--serve", "--port", "41327"]
                self.assertEqual(args[-len(expected):], expected)
            supplied = {name: "fixture ' $() " + name for name in (FILES / "service-env").read_text().split()}
            supplied.update(SOPHON_DOMAIN="override.test", SOPHON_INFRAVM_IP="10.0.60.9")
            result = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True,
                env=dict(os.environ, **supplied))
            self.assertEqual(result.returncode, 0, result.stderr)
            args, environment = json.loads(output.read_text())
            self.assertFalse(any(arg.startswith("--add-host") for arg in args))
            self.assertEqual(environment["SOPHON_DOMAIN"], "override.test")
            for name, value in supplied.items():
                self.assertEqual(environment[name], value)
                self.assertIn(name, args)
                self.assertNotIn(value, args)

    def disk_fixture(self, directory, *, data=b"\0" * 1024, signature="", permission=True,
                     recorded_uuid="", actual_uuid="", mounted_uuid=None):
        """Run the real disk gate in a file-backed guest, stubbing only device tools."""
        state = directory / "state"
        state.mkdir(exist_ok=True)
        export = directory / "export"
        export.mkdir(exist_ok=True)
        etc = directory / "etc"
        etc.mkdir(exist_ok=True)
        (etc / "sophon").mkdir(exist_ok=True)
        (etc / "sophon-controller-marker").write_text(MARKER)
        (etc / "sophon/controller-ip").write_text("10.0.60.2")
        (etc / "sophon/controller-network").write_text("10.0.60.2/24")
        (etc / "sophon/portainer-tar").write_text("portainer.tar")
        (etc / "fstab").touch()
        disk = directory / "disk"
        disk.write_bytes(data)
        if permission:
            (state / "allow-data-format").write_text(MARKER)
        if recorded_uuid:
            (state / "data-uuid").write_text(recorded_uuid)
        model = directory / "model.json"
        model.write_text(json.dumps({
            "signature": signature, "uuid": actual_uuid, "mounted": False,
            "mounted_uuid": mounted_uuid, "formatted": 0,
        }))
        bindir = directory / "bin"
        bindir.mkdir(exist_ok=True)
        stub = f"""#!{sys.executable}
import json, os, pathlib, sys
model = pathlib.Path({str(model)!r})
m = json.loads(model.read_text())
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
result = 0
if name == 'wipefs':
    print(m['signature'])
elif name == 'blkid':
    if not m['uuid']:
        result = 2
    else:
        print('ext4' if 'TYPE' in args else m['uuid'])
elif name == 'blockdev':
    print({len(data)})
elif name == 'mkfs.ext4':
    m['formatted'] += 1
    m['uuid'] = args[args.index('-U') + 1]
    m['signature'] = 'ext4'
elif name == 'mountpoint':
    result = 0 if args[-1] == {str(export)!r} and m['mounted'] else 1
elif name == 'mount':
    m['mounted'] = True
elif name == 'findmnt':
    print(m['mounted_uuid'] if m['mounted_uuid'] is not None else m['uuid'])
model.write_text(json.dumps(m))
sys.exit(result)
"""
        for name in ("wipefs", "blkid", "blockdev", "mkfs.ext4", "mountpoint", "mount", "findmnt", "flock", "chown"):
            path = bindir / name
            path.write_text(stub)
            path.chmod(0o755)
        prefix = IMPORT.split("rc-service sophon-podman start", 1)[0]
        prefix = prefix.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", "PATH=" + shlex.quote(str(bindir) + ":" + os.environ["PATH"]))
        for source, target in (
            ("/var/lib/sophon", state), ("/var/log/sophon-import.log", directory / "import.log"),
            ("/etc", etc), ("/media/sophon-deploy", directory / "media"), ("/dev/vdb", disk),
            ("/export", export),
        ):
            prefix = prefix.replace(source, str(target))
        prefix = prefix.replace(f"test -b {disk}", f"test -f {disk}")
        # The fixture's fstab must still use its remapped export path.
        result = subprocess.run(["sh"], input=prefix, text=True, capture_output=True)
        log = directory / "import.log"
        return result, json.loads(model.read_text()), log.read_text() if log.exists() else result.stderr

    def test_fresh_blank_disk_is_formatted_and_persistently_owned(self):
        with fixture() as directory:
            result, model, log = self.disk_fixture(directory)
            self.assertEqual(result.returncode, 0, log + result.stderr)
            self.assertEqual(model["formatted"], 1)
            recorded = (directory / "state/data-uuid").read_text().strip()
            self.assertEqual(recorded, model["uuid"])
            self.assertEqual((directory / "export/.sophon-controller-data").read_text().strip(), MARKER)
            self.assertFalse((directory / "state/allow-data-format").exists())
            self.assertIn("UUID=" + recorded, (directory / "etc/fstab").read_text())
            self.assertEqual((directory / "import.log").stat().st_mode & 0o777, 0o640)
            self.assertIn("chown root:operator /var/log/sophon-import.log", IMPORT)

    def test_existing_or_unrecognized_data_never_formats(self):
        scenarios = [
            {"signature": "ext4", "actual_uuid": "foreign"},
            {"signature": "gpt"},
            {"data": b"unrecognized irreplaceable data"},
            {"permission": False},
            {"recorded_uuid": "owned", "actual_uuid": "foreign", "signature": "ext4"},
            {"recorded_uuid": "owned", "actual_uuid": "owned", "signature": "ext4", "mounted_uuid": "foreign"},
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario), fixture() as directory:
                result, model, log = self.disk_fixture(directory, **scenario)
                self.assertNotEqual(result.returncode, 0, log)
                self.assertEqual(model["formatted"], 0, log)
                self.assertFalse((directory / "state/controller-ready").exists())
                self.assertTrue((directory / "state/controller-failed").exists())

    def test_owned_data_resumes_without_format_permission_or_reformatting(self):
        with fixture() as directory:
            (directory / "export").mkdir()
            (directory / "export/.sophon-controller-data").write_text(MARKER)
            result, model, log = self.disk_fixture(
                directory, permission=False, recorded_uuid="owned",
                actual_uuid="owned", signature="ext4")
            self.assertEqual(result.returncode, 0, log + result.stderr)
            self.assertEqual(model["formatted"], 0)

    def test_import_readiness_follows_kopia_and_nfs_and_atomic_project_commit(self):
        self.assertLess(IMPORT.index("sha256sum") if "sha256sum" in IMPORT else IMPORT.index("hashlib"),
                        IMPORT.index('cp -a "$media/project/."'))
        self.assertIn("test ! -e /export/sophon", IMPORT)
        self.assertIn("mv /export/.sophon-import-project /export/sophon", IMPORT)
        self.assertLess(IMPORT.index("ssh-keygen -q -t ed25519"), IMPORT.index('> "$state/controller-ready.new"'))
        self.assertLess(IMPORT.index("exportfs -ra"), IMPORT.index('> "$state/controller-ready.new"'))
        self.assertIn('mv "$state/controller-ready.new" "$state/controller-ready"', IMPORT)
        self.assertNotIn("mkfs.ext4 -F", IMPORT)
        self.assertIn('cmp -n "$(blockdev --getsize64 /dev/vdb)" /dev/vdb /dev/zero', IMPORT)

    @unittest.skipUnless(shutil.which("ansible-playbook"), "optional Ansible API/render integration fixture")
    def test_ansible_api_fixtures_fresh_resume_and_fail_closed(self):
        """All HTTP is loopback-only; QGA scripts are captured, never executed."""
        for scenario in ("fresh", "legacy", "foreign-description", "foreign-guest", "owned"):
            with self.subTest(scenario=scenario), fixture() as directory:
                requests = []
                uploads = []
                commands = []
                guest_results = {}
                owner = self

                class API(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass

                    def reply(self, data):
                        encoded = json.dumps({"data": data}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(encoded)))
                        self.end_headers()
                        self.wfile.write(encoded)

                    def do_GET(self):
                        path = urlsplit(self.path).path
                        requests.append(("GET", path))
                        if path.endswith("/cluster/resources"):
                            self.reply([] if scenario == "fresh" else [{
                                "name": "sophon-nfs" if scenario == "legacy" else "sophon-controller",
                                "vmid": 101, "node": "pve", "status": "running", "type": "qemu",
                            }])
                        elif path.endswith("/config"):
                            self.reply({
                                "description": "foreign" if scenario == "foreign-description" else MARKER,
                                "virtio1": "local-lvm:vm-101-disk-1",
                            })
                        elif path.endswith("/content"):
                            self.reply([{"volid": "local:iso/" + name} for name in uploads])
                        elif "/tasks/" in path:
                            self.reply({"status": "stopped", "exitstatus": "OK"})
                        elif path.endswith("/cluster/nextid"):
                            self.reply("101")
                        elif path.endswith("/agent/exec-status"):
                            pid = parse_qs(urlsplit(self.path).query)["pid"][0]
                            self.reply({
                                "exited": 1, "exitcode": guest_results[pid],
                                "out-data": "", "err-data": "",
                            })
                        elif path.endswith("/storage"):
                            self.reply([])
                        else:
                            self.send_error(404)

                    def do_POST(self):
                        path = urlsplit(self.path).path
                        data = self.rfile.read(int(self.headers["Content-Length"]))
                        requests.append(("POST", path))
                        if path.endswith("/upload"):
                            match = re.search(rb'filename="([^"]+)"', data)
                            owner.assertIsNotNone(match)
                            uploads.append(match[1].decode())
                            self.reply("UPID:pve:0001:upload:")
                        elif path.endswith("/agent/exec"):
                            command = parse_qs(data.decode())["input-data"][0]
                            commands.append(command)
                            pid = str(len(commands))
                            guest_results[pid] = 1 if scenario == "foreign-guest" else 0
                            self.reply({"pid": int(pid)})
                        elif path.endswith("/agent/ping") or path.endswith("/storage"):
                            self.reply(None)
                        elif path.endswith("/qemu") or path.endswith("/status/start"):
                            self.reply("UPID:pve:0002:create:")
                        else:
                            self.send_error(404)

                server = ThreadingHTTPServer(("127.0.0.1", 0), API)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    image = directory / "image.qcow2"
                    iso = directory / "deployment.iso"
                    image.write_bytes(b"fixture image")
                    iso.write_bytes(b"fixture iso")
                    variables = {
                        "proxmox_api": f"http://127.0.0.1:{server.server_port}/api2/json",
                        "proxmox_ticket": "fixture-ticket", "proxmox_csrf": "fixture-csrf",
                        "proxmox_node": "pve", "proxmox_validate_certs": False,
                        "proxmox_iso_storage_id": "local", "proxmox_iso_storage_path": "/iso",
                        "controller_image_path": str(image), "controller_iso_path": str(iso),
                        "controller_ssh_public_key": "ssh-ed25519 AAAAC3Nzfixture operator's fixture",
                        "controller_console_password": "fixture ' $() console ü",
                        "nfs_ip": "10.0.60.2", "vnet_gateway": "10.0.60.1", "vnet_cidr": 24,
                        "vnet_dns": "10.0.60.1", "vnet_interface": "testnet", "vnet_vlan": "",
                        "portainer_image_tarname": "portainer.tar",
                        "controller_ready_retries": 1, "controller_ready_delay": 1,
                    }
                    playbook = directory / "fixture.yml"
                    playbook.write_text(yaml.safe_dump([{
                        "hosts": "localhost", "gather_facts": False, "vars": variables,
                        "tasks": [{"ansible.builtin.include_role": {"name": "controller"}}],
                    }]))
                    result = subprocess.run(
                        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
                        env=dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "roles"),
                                 ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1"),
                        capture_output=True, text=True, timeout=90,
                    )
                finally:
                    server.shutdown()
                    thread.join()
                    server.server_close()
                successful = scenario in ("fresh", "owned")
                self.assertEqual(result.returncode == 0, successful, result.stdout + result.stderr)
                if scenario in ("legacy", "foreign-description"):
                    self.assertFalse([r for r in requests if r[0] == "POST"])
                elif scenario == "foreign-guest":
                    self.assertEqual(len(commands), 1)
                    self.assertNotIn("chpasswd", commands[0])
                    self.assertFalse(uploads)
                else:
                    configure = next(command for command in commands if "chpasswd" in command)
                    rendered = subprocess.run(["sh", "-n"], input=configure, capture_output=True, text=True)
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
                    self.assertTrue(configure.isascii())
                    self.assertEqual("allow-data-format" in configure, scenario == "fresh")
                    self.assertEqual(len(uploads), 2 if scenario == "fresh" else 0)
                    self.assertNotIn(variables["controller_console_password"], result.stdout)
                    self.assertNotIn(variables["controller_console_password"], result.stderr)


if __name__ == "__main__":
    unittest.main()
