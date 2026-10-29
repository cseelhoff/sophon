"""Host-script fixtures only: python3 tests/test_installer.py (no devices or VMs)."""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from deployment import step_fields

SCRIPTS = ROOT / "guest"
INSTALL = SCRIPTS / "install.sh"
NETWORK = SCRIPTS / "sophon-network"



@contextlib.contextmanager
def fixture():
    directory = ROOT / "tests" / (".installer-fixture-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def shell(code, *args, env=None, script=INSTALL):
    return subprocess.run(
        ["bash", "-c", "source " + shlex.quote(str(script)) + "\n" + code, "fixture", *map(str, args)],
        text=True, capture_output=True, env=env,
    )


def manifest(media):
    (media / "SHA256SUMS").write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(media).as_posix() + "\n"
        for path in sorted(media.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"))


def bundle(media):
    for name, content in {
        "install.sh": INSTALL.read_text(),
        "runner.tar": "fixture runner",
        "tools/busybox": "fixture binary",
        "project/src/console.py": "print('console')",
        "project/site.json": '{"SOPHON_INFRAVM_IP":"10.0.60.2"}',
        "guest/sophon-deploy": (SCRIPTS / "sophon-deploy").read_text(),
        "guest/host-bootstrap.sh": (SCRIPTS / "host-bootstrap.sh").read_text(),
        "guest/sophon-network": NETWORK.read_text(),
        "guest/sophon-set-password": (SCRIPTS / "sophon-set-password").read_text(),
        "project/bootstrap/portainer-image": "docker.io/portainer/portainer-ee:2.33.3\n",
        "project/bootstrap/portainer-archive": "portainer.tar\n",
        "project/artifacts/containers/portainer.tar": "fixture portainer",
    }.items():
        path = media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    manifest(media)


DISK_STUBS = r'''
is_block_device() { return 0; }
lsblk() {
    case "$1 $2" in
        "-dnro TYPE") echo "${DISK_TYPE:-disk}";;
        "-dnro RO") echo "${DISK_RO:-0}";;
        "-nrpo NAME") printf '%s\n' /dev/fixture; [[ ${CHILDREN:-0} == 0 ]] || echo /dev/fixture1;;
        "-nro MOUNTPOINTS") printf '%s' "${MOUNTS:-}";;
        "-snrpo NAME") printf '%s\n' /dev/fixture1 /dev/fixture;;
        *) return 99;;
    esac
    return 0
}
findmnt() { printf '%s' "${MOUNT_SOURCE:-}"; }
wipefs() { printf '%s' "${SIGNATURES:-}"; }
blockdev() { echo 107374182400; }
readlink() { printf '%s\n' "${@: -1}"; }
'''


TOOL_STUB = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["STUB_ROOT"])
with (root / "calls").open("a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "id":
    print(os.environ.get("STUB_UID", "1000") if args == ["-u"] else "core")
elif name == "curl":
    sys.exit(int(os.environ.get("CURL_FAIL", "0")))
elif name == "podman":
    assert args.pop(0) == "--remote=false"
    statefile = root / "state"
    state = json.loads(statefile.read_text()) if statefile.exists() else {
        "images": [], "network": False, "container": False, "volume": False
    }
    status = 0
    if args[:2] == ["image", "exists"]:
        status = int(args[2] not in state["images"])
    elif args[0] == "load":
        image = ("sophon:latest" if args[-1].endswith("runner.tar")
                 else "docker.io/portainer/portainer-ee:2.33.3")
        state["images"].append(image)
    elif args[:2] == ["network", "exists"]:
        status = int(not state["network"])
    elif args[:2] == ["network", "create"]:
        state["network"] = True
    elif args[:2] == ["volume", "exists"]:
        status = int(not state["volume"])
    elif args[:2] == ["volume", "create"]:
        status = int(state["volume"]) * 125
        state["volume"] = True
    elif args[:2] == ["container", "exists"]:
        status = int(not state["container"])
    elif args[0] == "run":
        state["container"] = True
    elif args[0] == "inspect":
        print("true")
    statefile.write_text(json.dumps(state))
    sys.exit(status)
elif name == "df":
    print("Avail\n1099511627776")
'''


def tools(directory):
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    for name in ("id", "podman", "systemctl", "curl", "sleep", "chown", "sync", "df"):
        path = bin_dir / name
        path.write_text("#!" + sys.executable + "\n" + TOOL_STUB)
        path.chmod(0o755)
    return dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                STUB_ROOT=str(directory))


def calls(directory, name=None):
    path = directory / "calls"
    result = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [call for call in result if call[0] == name] if name else result


class InstallerTests(unittest.TestCase):
    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shell_syntax_and_no_python_on_host(self):
        scripts = [INSTALL, *(SCRIPTS / name for name in (
            "sophon-deploy", "host-bootstrap.sh", "sophon-network", "sophon-set-password", "sophon-autoinstall"))]
        for script in scripts:
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assert_success(result)
            self.assertTrue(os.access(script, os.X_OK))
            if script.name != "sophon-deploy":  # the controller starts the runner container, which is Python
                self.assertNotIn("python", script.read_text())

    def test_manifest_from_arbitrary_mountpoint(self):
        with fixture() as directory:
            media = directory / "artifact mount with spaces"
            bundle(media)
            self.assert_success(shell('verify_bundle "$1"', media))

    def test_corrupt_or_unlisted_bundle_refused(self):
        with fixture() as media:
            bundle(media)
            (media / "runner.tar").write_text("corrupt")
            self.assertNotEqual(shell('verify_bundle "$1"', media).returncode, 0)
            manifest(media)
            (media / "unlisted").write_text("unchecked")
            self.assertIn("missing from checksum", shell('verify_bundle "$1"', media).stderr)

    def test_missing_and_unsafe_manifest_paths_refused(self):
        with fixture() as media:
            bundle(media)
            original = (media / "SHA256SUMS").read_text()
            for invalid in ("../outside", "/etc/passwd", "project/../escape", "project/./src/console.py"):
                with self.subTest(path=invalid):
                    (media / "SHA256SUMS").write_text(original + "0" * 64 + "  " + invalid + "\n")
                    self.assertIn("Unsafe", shell('verify_bundle "$1"', media).stderr)
            (media / "SHA256SUMS").write_text(original + original.splitlines()[0] + "\n")
            self.assertIn("duplicate", shell('verify_bundle "$1"', media).stderr)
            (media / "SHA256SUMS").write_text("")
            self.assertNotEqual(shell('verify_bundle "$1"', media).returncode, 0)

    def test_bundle_symlink_is_not_followed(self):
        with fixture() as media:
            bundle(media)
            (media / "symlink").symlink_to("runner.tar")
            self.assertIn("regular files", shell('verify_bundle "$1"', media).stderr)

    def test_network_validation(self):
        for network in [
            ("10.0.60.2", "24", "10.0.60.1", "1.1.1.1"),
            ("192.168.1.2", "30", "192.168.1.1", "192.168.1.1"),
            ("172.16.0.2", "12", "172.16.0.1", "8.8.8.8"),
        ]:
            self.assert_success(shell('validate_network "$@"', *network, script=NETWORK))
        for network in [
            ("10.0.60.0", "24", "10.0.60.1", "1.1.1.1"),
            ("10.0.60.255", "24", "10.0.60.1", "1.1.1.1"),
            ("10.0.60.2", "24", "10.0.60.255", "1.1.1.1"),
            ("10.0.60.2", "24", "10.0.60.0", "1.1.1.1"),
            ("10.0.60.2", "24", "10.0.61.1", "1.1.1.1"),
            ("10.0.60.2", "24", "10.0.60.2", "1.1.1.1"),
            ("10.0.60.2", "31", "10.0.60.1", "1.1.1.1"),
            ("10.0.60.2", "0", "10.0.60.1", "1.1.1.1"),
            ("10.0.60.2", "024", "10.0.60.1", "1.1.1.1"),
            ("10.0.060.2", "24", "10.0.60.1", "1.1.1.1"),
            ("256.0.60.2", "24", "10.0.60.1", "1.1.1.1"),
            ("127.0.0.2", "24", "127.0.0.1", "1.1.1.1"),
            ("224.0.0.2", "24", "224.0.0.1", "1.1.1.1"),
            ("10.0.60.2", "24", "10.0.60.1", "0.0.0.0"),
            ('10.0.60.2";touch BAD', "24", "10.0.60.1", "1.1.1.1"),
        ]:
            with self.subTest(network=network):
                self.assertNotEqual(shell('validate_network "$@"', *network, script=NETWORK).returncode, 0)

    def test_network_defaults_come_from_the_current_system(self):
        stubs = r'''
ip() {
    case "$*" in
        "-4 route show default") echo "default via 10.0.60.1 dev eth0 proto dhcp metric 100";;
        "-4 -o addr show dev eth0 scope global") echo "2: eth0    inet 10.0.60.23/24 brd 10.0.60.255 scope global dynamic eth0";;
        *) return 1;;
    esac
}
resolvectl() { echo "Link 2 (eth0): 10.0.60.53 10.0.60.54"; }
'''
        result = shell(stubs + r'''
detect_network
printf '%s|%s|%s|%s\n' "$detected_ip" "$detected_prefix" "$detected_gateway" "$detected_dns"
''', script=NETWORK)
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), '10.0.60.23|24|10.0.60.1|10.0.60.53')

        # Enter alone takes the default; typing a value overrides it; no default and no answer stays empty.
        result = shell(r'''
ask one 'One' 1.1.1.1 <<< ""
ask two 'Two' 1.1.1.1 <<< "9.9.9.9"
ask three 'Three' "" <<< ""
printf '%s|%s|%s\n' "$one" "$two" "$three"
''', script=NETWORK)
        self.assert_success(result)
        self.assertEqual(result.stdout.splitlines()[-1], '1.1.1.1|9.9.9.9|')

    def test_nothing_detectable_gives_no_defaults(self):
        result = shell(r'''
ip() { return 1; }
resolvectl() { return 1; }
detect_network
printf '[%s|%s|%s|%s]\n' "$detected_ip" "$detected_prefix" "$detected_gateway" "$detected_dns"
''', script=NETWORK)
        self.assert_success(result)
        output = result.stdout.strip()
        # The DNS field may come from this machine's own resolv.conf; everything else must be empty.
        self.assertTrue(output.startswith('[|||') and output.endswith(']'), output)

    def test_network_step_applies_the_answers_and_remembers_them(self):
        with fixture() as directory:
            site = directory / "site.json"
            log = directory / "log"
            stubs = f'''
ip() {{ return 1; }}
resolvectl() {{ return 1; }}
sudo() {{ printf '%s\\n' "$*" >> {shlex.quote(str(log))}; if [[ $1 == sh ]]; then cat > {shlex.quote(str(directory / "keyfile"))}; fi; }}
'''
            # A bad address is asked again; then every answer is accepted.
            answers = "10.0.60.2\n24\n10.0.61.1\n1.1.1.1\n10.0.60.2\n24\n10.0.60.1\n1.1.1.1\n"
            result = subprocess.run(
                ["bash", "-c", "source " + shlex.quote(str(NETWORK)) + "\n" + stubs + 'main "$1"', "x", str(site)],
                input=answers, text=True, capture_output=True)
            self.assert_success(result)
            self.assertIn("Gateway must be distinct and in the same subnet", result.stderr)
            self.assertEqual(json.loads(site.read_text()), {"SOPHON_INFRAVM_IP": "10.0.60.2", "SOPHON_VNET_DNS": "1.1.1.1"})
            keyfile = (directory / "keyfile").read_text()
            self.assertIn("address1=10.0.60.2/24,10.0.60.1", keyfile)
            self.assertIn("dns=1.1.1.1;", keyfile)
            commands = log.read_text().splitlines()
            self.assertIn("nmcli connection reload", commands)
            self.assertIn("nmcli connection up sophon-static", commands)
            self.assertTrue(commands[0].startswith("sh -c umask 077"))
            self.assertFalse(site.with_name("site.json.tmp").exists())

    def test_dns_defaults_to_cloudflare_and_shows_the_current_server(self):
        with fixture() as directory:
            site = directory / "site.json"
            stubs = f'''
ip() {{
    case "$*" in
        "-4 route show default") echo "default via 10.0.60.1 dev eth0";;
        "-4 -o addr show dev eth0 scope global") echo "2: eth0    inet 10.0.60.23/24 brd 10.0.60.255 scope global eth0";;
        *) return 1;;
    esac
}}
resolvectl() {{ echo "Link 2 (eth0): 10.0.60.53"; }}
sudo() {{ cat > /dev/null 2>&1 || true; }}
'''
            # Enter at every prompt: address, prefix and gateway keep the current values; DNS takes the default.
            result = subprocess.run(
                ["bash", "-c", "source " + shlex.quote(str(NETWORK)) + "\n" + stubs + 'main "$1"', "x", str(site)],
                input="\n\n\n\n", text=True, capture_output=True)
            self.assert_success(result)
            self.assertIn("Current DNS server (from DHCP): 10.0.60.53", result.stdout)
            self.assertIn('ask dns \'DNS server IPv4 address\' "$default_dns"', NETWORK.read_text())
            self.assertIn("default_dns=1.1.1.1", NETWORK.read_text())
            self.assertEqual(json.loads(site.read_text()),
                             {"SOPHON_INFRAVM_IP": "10.0.60.23", "SOPHON_VNET_DNS": "1.1.1.1"})

    def test_each_prompt_is_its_own_statement(self):
        # A joined line once turned the password prompt into extra arguments of the previous command.
        for script in (INSTALL, NETWORK, SCRIPTS / "sophon-set-password"):
            for line in script.read_text().splitlines():
                if "read -r" in line:
                    self.assertTrue(line.lstrip().startswith(("read -r", "while IFS= read", "IFS=. read", "if ")), line)

    def test_first_boot_password_is_confirmed_and_set_once(self):
        with fixture() as directory:
            flag = directory / "password-set"
            received = directory / "chpasswd-input"
            script = directory / "set-password"
            script.write_text((SCRIPTS / "sophon-set-password").read_text()
                              .replace("/var/lib/sophon-core-password-set", str(flag)))
            env = tools(directory)
            stub = directory / "bin/chpasswd"
            stub.write_text(f'#!/bin/sh\ncat > {received}\n')
            stub.chmod(0o755)
            # Empty, then mismatched, then a matching password containing a colon.
            answers = "\n\nabc\ndef\nsecret:pass word\nsecret:pass word\n"
            result = subprocess.run(["bash", str(script)], input=answers, env=env, text=True, capture_output=True)
            self.assert_success(result)
            self.assertIn("must not be empty", result.stdout)
            self.assertIn("do not match", result.stdout)
            self.assertEqual(received.read_text(), "core:secret:pass word\n")
            self.assertTrue(flag.is_file())
            self.assertNotIn("secret:pass word", result.stdout + result.stderr)
            received.unlink()
            # Once a password exists the unit does nothing, even if it is started again.
            result = subprocess.run(["bash", str(script)], input="x\nx\n", env=env, text=True, capture_output=True)
            self.assert_success(result)
            self.assertFalse(received.exists())
            # Without a terminal answer (input closed) it fails instead of spinning or setting a blank password.
            flag.unlink()
            result = subprocess.run(["bash", str(script)], input="", env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(flag.exists())

    def test_fresh_disk_only(self):
        self.assert_success(shell(DISK_STUBS + '\nvalidate_disk /dev/fixture'))
        cases = {
            "SIGNATURES": "ext4", "MOUNTS": "/", "CHILDREN": "1",
            "DISK_TYPE": "rom", "DISK_RO": "1",
            "MOUNT_SOURCE": "/dev/fixture1",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                result = shell(DISK_STUBS + '\nvalidate_disk /dev/fixture',
                               env=dict(os.environ, **{name: value}))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_block_check_not_bypassed_by_a_regular_file(self):
        result = shell('validate_disk "$1"', INSTALL)
        self.assertIn("whole block disk", result.stderr)

    def test_conservative_capacity_before_install(self):
        with fixture() as media:
            bundle(media)
            result = shell('required_bytes "$1"', media)
            self.assert_success(result)
            self.assertGreater(int(result.stdout), 20 * 1024**3)
            source = INSTALL.read_text()
            self.assertLess(source.index('(( $(blockdev --getsize64 "$disk") >= required ))'),
                            source.index('coreos-installer install --offline'))

    def test_render_valid_ignition_and_host_contract(self):
        with fixture() as media:
            bundle(media)
            result = shell('render_ignition "$1"', media)
            self.assert_success(result)
            ignition = json.loads(result.stdout)
            self.assertEqual(ignition["ignition"]["version"], "3.4.0")
            # No password and no network are baked in: the first boot and step 4 ask for them.
            user, = ignition["passwd"]["users"]
            self.assertEqual(user, dict(name="core", uid=1000, groups=["sudo", "wheel"]))
            files = {
                item["path"]: (item["mode"], base64.b64decode(item["contents"]["source"].split(",", 1)[1]).decode())
                for item in ignition["storage"]["files"]
            }
            self.assertNotIn("/etc/NetworkManager/system-connections/sophon-static.nmconnection", files)
            for name in ("deploy", "host-bootstrap", "network", "set-password"):
                self.assertIn(name, " ".join(files))
            self.assertEqual(files["/usr/local/bin/sophon-deploy"][0], 0o755)
            self.assertEqual(files["/usr/local/libexec/sophon-network"][1], NETWORK.read_text().rstrip("\n"))
            self.assertEqual(files["/usr/local/libexec/sophon-set-password"][0], 0o755)
            self.assertIn("PasswordAuthentication no", files["/etc/ssh/sshd_config.d/40-sophon-console-only.conf"][1])
            self.assertIn("enabled = false", files["/etc/zincati/config.d/90-disable-auto-reboot.toml"][1])
            self.assertIn("/var/lib/systemd/linger/core", files)
            units = {unit["name"]: unit for unit in ignition["systemd"]["units"]}
            self.assertEqual(set(units), {"sophon-host-bootstrap.service", "sophon-set-password.service"})
            bootstrap = units["sophon-host-bootstrap.service"]
            self.assertTrue(bootstrap["enabled"])
            self.assertIn("User=core", bootstrap["contents"])
            self.assertIn("Requires=user@1000.service", bootstrap["contents"])
            self.assertIn("ExecStartPre=+/usr/sbin/restorecon -RF /var/lib/sophon", bootstrap["contents"])
            self.assertIn("StandardOutput=journal+console", bootstrap["contents"])
            password = units["sophon-set-password.service"]
            self.assertTrue(password["enabled"])
            # The login prompt waits for the password to be chosen, and the question never repeats.
            self.assertIn("Before=getty@tty1.service", password["contents"])
            self.assertIn("ConditionPathExists=!/var/lib/sophon-core-password-set", password["contents"])
            self.assertIn("TTYPath=/dev/tty1", password["contents"])
            self.assertNotIn("passwordHash", result.stdout)
            self.assertNotIn("sshAuthorizedKeys", result.stdout)
            self.assertNotIn("setenforce", result.stdout)

    def test_automatic_mode_never_guesses_a_disk(self):
        source = INSTALL.read_text()
        self.assertIn("--auto) auto=1", source)
        auto_stop = source.index("elif (( auto ));")
        self.assertLess(auto_stop, source.index("ask disk"))
        self.assertIn("exactly one blank disk", source[auto_stop:auto_stop + 400])
        # Unknown arguments are refused (before anything touches a disk).
        result = shell('main --bogus')
        self.assertIn("Usage:", result.stderr)
        self.assertNotEqual(result.returncode, 0)
        result = shell('main --auto extra')
        self.assertIn("Usage:", result.stderr)
        # An already-installed disk is nonblank, so booting the ISO again can never reinstall over it.
        self.assertIn("(already installed?", source)
        self.assertNotIn("INSTALL $disk", source)

    def test_ostree_var_is_selected_not_physical_root_var(self):
        with fixture() as directory:
            root = directory / "mounted-root"
            root.joinpath("var/lib").mkdir(parents=True)
            var = root / "ostree/deploy/fedora-coreos/var"
            var.mkdir(parents=True)
            var.parent.joinpath("deploy/hash.0").mkdir(parents=True)
            result = shell('installed_var "$1"', root)
            self.assert_success(result)
            self.assertEqual(result.stdout.strip(), str(var))
            other = root / "ostree/deploy/other/var"
            other.mkdir(parents=True)
            other.parent.joinpath("deploy").mkdir()
            self.assertIn("exactly one", shell('installed_var "$1"', root).stderr)

    def test_installed_root_expansion_before_artifact_copy(self):
        stubs = r'''
lsblk() {
    case "$2" in PARTN) echo 4;; FSTYPE) echo "${ROOT_FSTYPE:-xfs}";; *) return 99;; esac
}
growpart() { printf '%s\n' "${GROW_OUTPUT:-CHANGED: partition=4}"; return "${GROW_STATUS:-0}"; }
sgdisk() { [[ $1 == --move-main-table=2 && $2 == /dev/fixture ]]; }
udevadm() { [[ $1 == settle ]]; }
'''
        for output, status in (("CHANGED: partition=4", "0"), ("NOCHANGE: partition 4 is size 100", "1")):
            env = dict(os.environ, GROW_OUTPUT=output, GROW_STATUS=status)
            self.assert_success(shell(stubs + '\ngrow_installed_root /dev/fixture /dev/fixture4', env=env))
        result = shell(stubs + '\ngrow_installed_root /dev/fixture /dev/fixture4',
                       env=dict(os.environ, GROW_OUTPUT="FAILED: cannot resize", GROW_STATUS="2"))
        self.assertIn("Cannot expand", result.stderr)
        result = shell(stubs + '\ngrow_installed_root /dev/fixture /dev/fixture4',
                       env=dict(os.environ, ROOT_FSTYPE="ext4"))
        self.assertIn("numbered XFS", result.stderr)
        source = INSTALL.read_text()
        self.assertLess(source.index('grow_installed_root "$disk" "$part"'),
                        source.index('mount "$part" "$root"'))
        self.assertLess(source.index('xfs_growfs "$root"'),
                        source.index('copy_artifacts "$media" "$root"'))

    @unittest.skipUnless(shutil.which("sgdisk") and shutil.which("sfdisk"), "Native GPT tools are required")
    def test_grown_gpt_survives_coreos_first_boot_without_changing_partitions(self):
        with fixture() as directory:
            image = directory / "disk.img"
            with image.open("wb") as output:
                output.truncate(64 * 1024 * 1024)
            setup = subprocess.run(
                ["sfdisk", "--no-reread", str(image)],
                input="label: gpt\nfirst-lba: 2048\n\nstart=2048, size=128991, type=L\n",
                text=True, capture_output=True)
            self.assert_success(setup)
            before = json.loads(subprocess.check_output(["sfdisk", "--json", str(image)]))
            first_boot = ["sgdisk", "--disk-guid=R", "--move-second-header", str(image)]
            broken = subprocess.run(first_boot, text=True, capture_output=True)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("overlaps", broken.stdout + broken.stderr)
            stubs = r'''
lsblk() { case "$2" in PARTN) echo 1;; FSTYPE) echo xfs;; *) return 99;; esac; }
growpart() { echo 'NOCHANGE: already expanded'; return 1; }
udevadm() { [[ $1 == settle ]]; }
'''
            self.assert_success(shell(stubs + '\ngrow_installed_root "$1" "$1"', image))
            after = json.loads(subprocess.check_output(["sfdisk", "--json", str(image)]))
            self.assertEqual(before["partitiontable"]["partitions"], after["partitiontable"]["partitions"])
            self.assertEqual(after["partitiontable"]["firstlba"], 34)
            self.assert_success(subprocess.run(first_boot, text=True, capture_output=True))
            self.assert_success(subprocess.run(["sgdisk", "--verify", str(image)], text=True, capture_output=True))

    def test_var_missing_or_symlink_refused(self):
        with fixture() as root:
            self.assertNotEqual(shell('installed_var "$1"', root).returncode, 0)
            (root / "ostree/deploy/fedora-coreos/deploy").mkdir(parents=True)
            (root / "var").mkdir()
            (root / "ostree/deploy/fedora-coreos/var").symlink_to(root / "var", target_is_directory=True)
            self.assertNotEqual(shell('installed_var "$1"', root).returncode, 0)

    def test_persistent_copy_ownership_and_no_overwrite(self):
        with fixture() as directory:
            media = directory / "media"
            bundle(media)
            (media / "project/site.json").unlink()
            root = directory / "root"
            var = root / "ostree/deploy/fedora-coreos/var"
            var.mkdir(parents=True)
            var.parent.joinpath("deploy").mkdir()
            env = tools(directory)
            result = shell('copy_artifacts "$@"', media, root, env=env)
            self.assert_success(result)
            data = var / "lib/sophon"
            self.assertEqual((data / "runner.tar").read_bytes(), (media / "runner.tar").read_bytes())
            # The address is chosen later by sophon-deploy; the installer saves none.
            self.assertFalse((data / "project/site.json").exists())
            self.assertFalse((root / "var").exists())
            self.assertEqual(calls(directory, "chown"), [["chown", "-R", "1000:1000", str(data)]])
            result = shell('copy_artifacts "$@"', media, root, env=env)
            self.assertIn("already exists", result.stderr)

    def test_bootstrap_before_the_address_is_known_starts_portainer_without_dns(self):
        with fixture() as directory:
            bundle(directory / "media")
            data = directory / "persistent"
            shutil.copytree(directory / "media/project", data / "project")
            (data / "project/site.json").unlink()
            (data / "runner.tar").write_text("runner")
            ready = directory / "ready"
            script = directory / "host-bootstrap"
            script.write_text((SCRIPTS / "host-bootstrap.sh").read_text()
                              .replace("/var/lib/sophon", str(data))
                              .replace("/run/user/1000/sophon-ready", str(ready)))
            env = tools(directory)
            self.assert_success(subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True))
            self.assertTrue(ready.is_file())
            podman = calls(directory, "podman")
            network_create, = [call for call in podman if call[2:4] == ["network", "create"]]
            self.assertNotIn("--dns", network_create)
            self.assertEqual(len([call for call in podman if call[2:4] == ["network", "update"]]), 0)
            # Once step 4 has saved the address, a rerun points the services' network at it.
            (data / "project/site.json").write_text('{"SOPHON_INFRAVM_IP":"10.0.60.2"}')
            self.assert_success(subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True))
            self.assertIn(["podman", "--remote=false", "network", "update", "--dns-add", "10.0.60.2", "proxy"],
                          calls(directory, "podman"))

    def test_bootstrap_is_idempotent_offline_and_api_ready(self):
        with fixture() as directory:
            bundle(directory / "media")
            data = directory / "persistent"
            shutil.copytree(directory / "media/project", data / "project")
            (data / "runner.tar").write_text("runner")
            ready = directory / "ready"
            script = directory / "host-bootstrap"
            script.write_text((SCRIPTS / "host-bootstrap.sh").read_text()
                              .replace("/var/lib/sophon", str(data))
                              .replace("/run/user/1000/sophon-ready", str(ready)))
            env = tools(directory)
            for _ in range(2):
                result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
                self.assert_success(result)
                self.assertTrue(ready.is_file())
                self.assertIn("Log in as core", result.stdout)
            podman = calls(directory, "podman")
            self.assertEqual(len([call for call in podman if call[2] == "load"]), 2)
            self.assertEqual(len([call for call in podman if call[2] == "run"]), 1)
            self.assertEqual(len([call for call in podman if call[2] == "restart"]), 1)
            network_create, = [call for call in podman if call[2:4] == ["network", "create"]]
            self.assertIn("--dns", network_create)
            self.assertEqual(network_create[network_create.index("--dns") + 1], "10.0.60.2")
            self.assertIn(["podman", "--remote=false", "network", "update",
                           "--dns-add", "10.0.60.2", "proxy"], podman)
            run, = [call for call in podman if call[2] == "run"]
            self.assertIn("--restart", run)
            self.assertIn("always", run)
            self.assertIn("9443:9443", run)
            self.assertIn("/run/user/1000/podman/podman.sock:/var/run/docker.sock", run)
            self.assertNotIn("--privileged", run)
            self.assertEqual(len(calls(directory, "curl")), 2)

    def test_bootstrap_failure_does_not_offer_continuation(self):
        with fixture() as directory:
            bundle(directory / "media")
            data = directory / "persistent"
            shutil.copytree(directory / "media/project", data / "project")
            (data / "runner.tar").write_text("runner")
            ready = directory / "ready"
            ready.touch()
            script = directory / "host-bootstrap"
            script.write_text((SCRIPTS / "host-bootstrap.sh").read_text()
                              .replace("/var/lib/sophon", str(data))
                              .replace("/run/user/1000/sophon-ready", str(ready)))
            env = tools(directory)
            env["CURL_FAIL"] = "1"
            result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(ready.exists())
            self.assertNotIn("Log in as core", result.stdout)
            self.assertIn("retry", result.stderr)

    def test_metadata_is_data_not_shell(self):
        with fixture() as directory:
            data = directory / "persistent"
            (data / "project/bootstrap").mkdir(parents=True)
            (data / "project/bootstrap/portainer-image").write_text("image\nMALICIOUS=1\n")
            script = directory / "host-bootstrap"
            script.write_text((SCRIPTS / "host-bootstrap.sh").read_text()
                              .replace("/var/lib/sophon", str(data))
                              .replace("/run/user/1000/sophon-ready", str(directory / "ready")))
            result = subprocess.run(["bash", str(script)], env=tools(directory), text=True, capture_output=True)
            self.assertIn("Invalid Portainer image", result.stderr)
            self.assertFalse(calls(directory, "podman"))

    def controller(self, directory, saved=True, relocate=False):
        """A copy of sophon-deploy whose paths point into `directory`; returns (script, ready file, data dir, env)."""
        data = directory / "persistent"
        (data / "project").mkdir(parents=True)
        site = data / "project/site.json"
        if saved:
            site.write_text('{"SOPHON_INFRAVM_IP":"10.0.60.2","SOPHON_VNET_DNS":"1.1.1.1"}')
        ready = directory / "ready"
        helper = directory / "network-helper"
        helper.write_text('#!/bin/sh\necho "$@" >> "$STUB_ROOT/helper-calls"\n'
                          'printf \'{"SOPHON_INFRAVM_IP":"10.0.60.9","SOPHON_VNET_DNS":"1.1.1.1"}\' > "$1"\n')
        helper.chmod(0o755)
        text = (SCRIPTS / "sophon-deploy").read_text()
        text = (text.replace("/var/lib/sophon", str(data)) if relocate
                else text.replace("site=/var/lib/sophon/project/site.json", f"site={site}"))
        script = directory / "controller"
        script.write_text(text.replace("/run/user/1000/sophon-ready", str(ready))
                          .replace("/usr/local/libexec/sophon-network", str(helper)))
        env = tools(directory)
        # Starting or restarting host setup is what makes it ready.
        sudo = directory / "bin/sudo"
        sudo.write_text(f'#!/bin/sh\necho "sudo $*" >> "$STUB_ROOT/sudo-calls"\n'
                        f'[ "$1 $2" != "systemctl restart" ] || touch {shlex.quote(str(ready))}\n')
        sudo.chmod(0o755)
        return script, ready, data, env

    def sudo_calls(self, directory):
        path = directory / "sudo-calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_wrapper_argument_and_environment_boundary(self):
        source = (SCRIPTS / "sophon-deploy").read_text()
        allowed = source.split("for name in", 1)[1].split("\ndo", 1)[0].replace("\\", "").split()
        self.assertEqual(set(allowed), set(step_fields("configure")))
        with fixture() as directory:
            script, ready, _, env = self.controller(directory)
            ready.touch()
            env.update(SOPHON_DOMAIN="example.test", SOPHON_CF_TOKEN="secret with spaces",
                       SOPHON_INFRAVM_IP="10.0.60.2", SOPHON_VNET_DNS="1.1.1.1",
                       SOPHON_NFS_IP="forbidden", SOPHON_VNET_GATEWAY="forbidden",
                       SOPHON_KOPIA_ADMIN_PASSWORD="forbidden", SOPHON_PROXMOX_PASSWORD="forbidden",
                       SOPHON_DEPLOYMENT_STEP="provision", CONTAINER_HOST="ssh://forbidden")
            for args in ([], ["--dashboard", "--port", "41327"], ["--run", "--state-dir", "/workspace/artifacts/deployment/services"],
                         ["--help"], ["--", "--tags", "gitea"]):
                result = subprocess.run(["bash", str(script), *args], env=env, text=True, capture_output=True)
                self.assert_success(result)
                command = calls(directory, "podman")[-1]
                boundary = command.index("sophon:latest")
                self.assertEqual(command[boundary + 1:], ["python3", "src/console.py", *args])
                self.assertIn("SOPHON_DEPLOYMENT_STEP=configure", command[:boundary])
                self.assertIn("SOPHON_CF_TOKEN", command[:boundary])
                self.assertIn("SOPHON_INFRAVM_IP", command[:boundary])
                self.assertNotIn("secret with spaces", " ".join(command))
                self.assertFalse(any("forbidden" in arg or "=bootstrap" in arg for arg in command))
                self.assertIn("/var/lib/sophon/project:/workspace:Z", command)
                self.assertIn("--pull", command)
                self.assertIn("never", command)
                self.assertNotIn("-it", command)
                self.assertNotIn("--privileged", command)
                self.assertFalse(any("podman.sock" in arg or arg.startswith("/:/") for arg in command))

    def test_certificate_disk_is_copied_into_the_project_and_unmounted(self):
        with fixture() as directory:
            script, ready, data, env = self.controller(directory, relocate=True)
            ready.touch()
            disk = directory / "disk"
            disk.mkdir()
            (disk / "acme.json").write_text('{"letsencrypt":{}}')
            sudo = directory / "bin/sudo"
            sudo.write_text('#!/usr/bin/env bash\n'
                            'echo "sudo $*" >> "$STUB_ROOT/sudo-calls"\n'
                            '[[ $1 != mount ]] || cp -r "${@: -2:1}"/. "${@: -1}"\n')
            sudo.chmod(0o755)
            env["SOPHON_CERTS_DEVICE"] = str(disk)
            result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
            self.assert_success(result)
            staged = data / "project/imports/acme.json"
            self.assertEqual(staged.read_text(), '{"letsencrypt":{}}')
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            command = calls(directory, "podman")[-1]
            self.assertIn("SOPHON_ACME_FILE", command[:command.index("sophon:latest")])
            self.assertNotIn("/workspace/imports/acme.json", " ".join(command))
            commands = self.sudo_calls(directory)
            self.assertTrue(commands[0].startswith("sudo mount -o ro "))
            self.assertTrue(commands[1].startswith("sudo umount "))

    def test_without_a_certificate_disk_no_file_is_offered(self):
        with fixture() as directory:
            script, ready, _, env = self.controller(directory)
            ready.touch()
            env["SOPHON_CERTS_DEVICE"] = str(directory / "absent")
            result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
            self.assert_success(result)
            command = calls(directory, "podman")[-1]
            self.assertNotIn("SOPHON_ACME_FILE", command[:command.index("sophon:latest")])

    def test_first_run_asks_for_the_network_then_finishes_host_setup(self):
        with fixture() as directory:
            script, ready, data, env = self.controller(directory, saved=False)
            result = subprocess.run(["bash", str(script), "--run"], env=env, text=True, capture_output=True)
            self.assert_success(result)
            site = data / "project/site.json"
            self.assertEqual(json.loads(site.read_text())["SOPHON_INFRAVM_IP"], "10.0.60.9")
            self.assertEqual((directory / "helper-calls").read_text().split(), [str(site)])
            # Wait for the host setup that began at boot, then redo it so DNS points at the saved address.
            self.assertEqual(self.sudo_calls(directory), ["sudo systemctl start sophon-host-bootstrap",
                                                          "sudo systemctl restart sophon-host-bootstrap"])
            command = calls(directory, "podman")[-1]
            self.assertEqual(command[command.index("sophon:latest") + 1:], ["python3", "src/console.py", "--run"])

    def test_saved_network_is_not_asked_again_unless_requested(self):
        with fixture() as directory:
            script, ready, _, env = self.controller(directory)
            ready.touch()
            self.assert_success(subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True))
            self.assertFalse((directory / "helper-calls").exists())
            self.assertEqual(self.sudo_calls(directory), [])
            # --network is the wrapper's own switch: it asks again and is not passed on to the console.
            result = subprocess.run(["bash", str(script), "--network", "--run"], env=env, text=True, capture_output=True)
            self.assert_success(result)
            self.assertTrue((directory / "helper-calls").exists())
            command = calls(directory, "podman")[-1]
            self.assertEqual(command[command.index("sophon:latest") + 1:], ["python3", "src/console.py", "--run"])

    def test_wrapper_waits_for_unfinished_host_setup_then_refuses_root_or_not_ready(self):
        with fixture() as directory:
            script, ready, _, env = self.controller(directory)
            # A saved network but setup not finished: it waits for setup, and stops if it still is not ready.
            result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
            self.assertIn("without reinstalling", result.stderr)
            self.assertEqual(self.sudo_calls(directory), ["sudo systemctl start sophon-host-bootstrap"])
            env["STUB_UID"] = "0"
            result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
            self.assertIn("do not use sudo", result.stderr)
            self.assertFalse(calls(directory, "podman"))

    def test_ambiguous_disks_are_not_guessed(self):
        result = shell(r'''
lsblk() { printf '/dev/sda disk 0\n/dev/sdb disk 0\n'; }
validate_disk() { return 0; }
printf '[%s]\n' "$(detect_disk)"
''')
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_destructive_install_is_offline_automatic_and_not_a_boot_action(self):
        source = INSTALL.read_text()
        self.assertEqual(source.count("coreos-installer install --offline"), 1)
        install = source.index("coreos-installer install --offline")
        # No typed confirmation any more: the blank-disk guard is rechecked right before, after an abortable pause.
        self.assertNotIn('"INSTALL $disk"', source)
        self.assertLess(source.index("press Ctrl-C to abort"), install)
        self.assertLess(source.rindex('validate_disk "$disk"', 0, install), install)
        self.assertLess(source.index("sleep 10"), source.rindex('validate_disk "$disk"', 0, install))
        self.assertIn("only runs in a CoreOS live environment", source)
        self.assertIn("--console tty0 --console ttyS0,115200n8", source)
        self.assertIn("DO NOT reinstall", source)
        self.assertNotIn("podman load", source)
        self.assertNotIn("--force", source)
        self.assertNotIn("coreos-installer", (SCRIPTS / "host-bootstrap.sh").read_text())
        self.assertNotIn("coreos-installer", (SCRIPTS / "sophon-deploy").read_text())


if __name__ == "__main__":
    unittest.main()
