#!/usr/bin/env python3
"""Dev-only live console/SFTP onboarding checks; never deploy application stacks."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


def inside(args):
    root = Path('/workspace')
    sys.path.insert(0, str(root / 'src'))
    import console
    import deployment
    values = deployment.new_control({}, 'configure')['values']
    for name, field in deployment.step_fields('configure').items():
        if field[2] == 'password':
            values[name] = 'dev-only-service-password'
    values.update(SOPHON_DEPLOYMENT_STEP='configure', SOPHON_DOMAIN='dev.invalid',
                  SOPHON_INFRAVM_IP='192.0.2.3', SOPHON_KOPIA_SFTP_HOST='127.0.0.1',
                  SOPHON_KOPIA_SFTP_USERNAME=os.environ['M3_SFTP_USER'],
                  SOPHON_KOPIA_SFTP_PATH=args.repository,
                  SOPHON_KOPIA_PASSWORD=os.environ['M3_REPOSITORY_PASSWORD'],
                  SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64='', SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64='',
                  SOPHON_KOPIA_CREATE_REPOSITORY='false')
    if args.scenario in ('wrong-password', 'nonempty', 'unattended-create'):
        values['SOPHON_KOPIA_CREATE_REPOSITORY'] = 'true'
    if args.scenario == 'wrong-password':
        values['SOPHON_KOPIA_PASSWORD'] += '-wrong'
    if args.scenario == 'changed-cache':
        values['SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64'] = base64.b64encode(
            (root / 'expected-known-hosts').read_bytes()).decode()
    os.environ.update(values)
    output = io.StringIO()
    deployed = False

    def deploy(arguments):
        nonlocal deployed
        assert arguments == ['--step', 'configure', '--', '4-configure.yml']
        assert os.environ['SOPHON_KOPIA_PREFLIGHT_COMPLETE'] == 'true'
        deployed = True
        return 0

    def answer(prompt):
        if 'fingerprints?' in prompt:
            offered = set(re.findall(r'SHA256:[A-Za-z0-9+/]+', output.getvalue()))
            expected = set((root / 'expected-fingerprints').read_text().splitlines())
            assert offered == expected, 'Scanned fingerprints differ from independently read server keys'
            return 'y'
        if 'displayed public key' in prompt:
            return 'n' if args.scenario == 'pair-cancel' else 'y'
        if 'encrypted repository?' in prompt:
            return 'y'
        return ''

    console.deployment.main = deploy
    console.sys.stdin.isatty = lambda: True
    import builtins
    builtins.input = answer
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        if args.scenario == 'runtime-error':
            state = root / 'credentials/kopia'
            pin = next(state.glob('*.known_hosts'))
            for name in ('PASSWORD', 'SERVER_PASSWORD', 'SFTP_HOST', 'SFTP_PORT', 'SFTP_USERNAME', 'SFTP_PATH'):
                os.environ['KOPIA_' + name] = values['SOPHON_KOPIA_' + name]
            os.environ.update(KOPIA_PASSWORD=values['SOPHON_KOPIA_PASSWORD'] + '-wrong',
                              KOPIA_CONFIG_PATH='/tmp/repository.config', KOPIA_CACHE_DIRECTORY='/tmp/cache',
                              KOPIA_LOG_DIR='/tmp/logs',
                              KOPIA_SFTP_PRIVATE_KEY_B64=base64.b64encode((state / 'id_ed25519').read_bytes()).decode(),
                              KOPIA_SFTP_KNOWN_HOSTS_B64=base64.b64encode(pin.read_bytes()).decode())
            runtime = root / 'roles/kopia/files/runtime.py'
            sys.path.insert(0, str(runtime.parent))
            sys.argv = [str(runtime)]
            try:
                runpy.run_path(str(runtime), run_name='__main__')
            except SystemExit as error:
                code = error.code
        else:
            code = console.main([] if args.scenario in ('pair-cancel', 'create') else ['--run'])
    text = output.getvalue()
    assert os.environ['M3_REPOSITORY_PASSWORD'] not in text
    assert '-----BEGIN OPENSSH PRIVATE KEY-----' not in text
    print(json.dumps({'scenario': args.scenario, 'code': code, 'deployed': deployed,
                      'output': text, 'available_space_reported': 'MiB available' in text}))


def host(args):
    source = Path(args.source).resolve()
    report = {'checks': {}, 'production_contacted': False}
    environment = dict(os.environ, M3_REPOSITORY_PASSWORD=secrets.token_urlsafe(32),
                       M3_SFTP_USER=os.environ['USER'])

    def command(argv, **kwargs):
        result = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=300, **kwargs)
        if result.returncode:
            raise RuntimeError(f'Dev command failed: {argv[:3]}: {result.stderr[-2000:]}')
        return result.stdout

    def podman(*argv):
        return command(['podman', '--cgroup-manager=cgroupfs', *argv])

    assert podman('info', '--format', '{{.Host.Security.Rootless}}').strip() == 'true'
    assert 'portainer' not in podman('ps', '-a', '--format', '{{.Names}}').lower()
    authorized = Path.home() / '.ssh/authorized_keys'
    if authorized.is_symlink() or not authorized.is_file():
        raise RuntimeError('Dev authorized_keys must be an existing regular file')
    authorized_line = None
    with tempfile.TemporaryDirectory(prefix='sophon-m3-', dir=args.sftp_root) as temporary:
        temporary = Path(temporary)
        project, repository = temporary / 'project', temporary / 'repository'
        (project / 'src').mkdir(parents=True)
        (project / 'tests/spikes').mkdir(parents=True)
        (project / 'roles/kopia/files').mkdir(parents=True)
        repository.mkdir()
        for name in ('console.py', 'deployment.py', 'certificates.py', 'backup_destination.py'):
            shutil.copy(source / 'src' / name, project / 'src' / name)
        for name in ('runtime.py', 'backup.py'):
            shutil.copy(source / 'roles/kopia/files' / name, project / 'roles/kopia/files' / name)
        shutil.copy(__file__, project / 'tests/spikes/kopia_m3.py')
        expected, fingerprints = [], []
        for public in sorted(Path('/etc/ssh').glob('ssh_host_*_key.pub')):
            fields = public.read_text().split()
            if fields[0] in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
                expected.append('127.0.0.1 ' + ' '.join(fields[:2]))
                fingerprints.append(command(['ssh-keygen', '-lf', str(public), '-E', 'sha256']).split()[1])
        (project / 'expected-known-hosts').write_text('\n'.join(expected) + '\n')
        (project / 'expected-fingerprints').write_text('\n'.join(fingerprints) + '\n')

        def scenario(name, path=repository):
            time.sleep(6)
            output = podman('run', '--rm', '--pull=never', '--network', 'host', '--cap-drop', 'ALL',
                            '-v', str(project) + ':/workspace', '-e', 'M3_REPOSITORY_PASSWORD', '-e', 'M3_SFTP_USER',
                            '--entrypoint', 'python3', args.image, '/workspace/tests/spikes/kopia_m3.py',
                            '--dev-only', '--inside', '--scenario', name, '--repository', str(path))
            return json.loads(output)

        try:
            result = scenario('pair-cancel')
            assert result['code'] == 2 and not result['deployed']
            key = project / 'credentials/kopia/id_ed25519'
            assert key.stat().st_mode & 0o777 == 0o600
            assert key.parent.stat().st_mode & 0o777 == 0o700
            identity = hashlib.sha256(key.read_bytes()).hexdigest()
            public = command(['ssh-keygen', '-y', '-f', str(key)]).strip()
            authorized_line = f'restrict,command="internal-sftp" {public} sophon-m3-{uuid.uuid4().hex}\n'
            with authorized.open('a') as stream:
                stream.write('\n' + authorized_line)
                stream.flush()
                os.fsync(stream.fileno())
            report['checks']['project_key_permissions_and_independent_fingerprints'] = True
            result = scenario('unattended')
            assert result['code'] == 2 and not result['deployed'] and 'explicit confirmation' in result['output']
            report['checks']['missing_repository_requires_confirmation'] = True
            result = scenario('create')
            assert result['code'] == 0 and result['deployed'], result['output']
            assert result['available_space_reported']
            markers = list(repository.glob('kopia.repository*'))
            assert len(markers) == 1, 'Unexpected native repository entries: ' + str(sorted(p.name for p in repository.iterdir()))
            marker_path = markers[0]
            marker = hashlib.sha256(marker_path.read_bytes()).hexdigest()
            result = scenario('rerun')
            assert result['code'] == 0 and result['deployed'], result['output']
            assert hashlib.sha256(key.read_bytes()).hexdigest() == identity
            assert hashlib.sha256(marker_path.read_bytes()).hexdigest() == marker
            assert not list(repository.glob('.sophon-preflight-*'))
            report['checks']['confirmed_creation_reused_key_and_existing_repository'] = True
            report['checks']['authenticated_write_read_delete_and_free_space'] = True
            result = scenario('wrong-password')
            assert result['code'] == 2 and not result['deployed'] and 'invalid repository password' in result['output'], result['output']
            assert hashlib.sha256(marker_path.read_bytes()).hexdigest() == marker
            report['checks']['wrong_password_with_creation_approval_never_reinitialized'] = True
            automated = temporary / 'automated'
            automated.mkdir()
            result = scenario('unattended-create', automated)
            assert result['code'] == 0 and result['deployed'], result['output']
            assert len(list(automated.glob('kopia.repository*'))) == 1
            assert hashlib.sha256(marker_path.read_bytes()).hexdigest() == marker
            report['checks']['explicit_unattended_creation_of_an_empty_destination'] = True
            unrelated = temporary / 'unrelated'
            unrelated.mkdir()
            (unrelated / 'keep').write_text('must not change')
            result = scenario('nonempty', unrelated)
            assert result['code'] == 2 and not result['deployed']
            assert not list(unrelated.glob('kopia.repository*'))
            assert (unrelated / 'keep').read_text() == 'must not change'
            report['checks']['nonempty_destination_never_initialized'] = True
            pin = next(key.parent.glob('*.known_hosts'))
            original = pin.read_bytes()
            pin.write_text('127.0.0.1 ' + public + '\n')
            result = scenario('changed-cache')
            assert result['code'] == 2 and not result['deployed'] and 'override cannot replace' in result['output']
            pin.write_bytes(original)
            report['checks']['changed_saved_host_key_rejected_even_with_override'] = True
            result = scenario('runtime-error')
            assert result['code'] == 1 and not result['deployed']
            diagnostic = next(line for line in result['output'].splitlines()
                              if line.startswith('KOPIA_STARTUP_ERROR_JSON='))
            message = json.loads(diagnostic.split('=', 1)[1])['message']
            assert 'invalid repository password' in message
            report['checks']['native_runtime_emitted_redacted_startup_error'] = message
            report['checks']['no_application_stacks_deployed'] = True
        finally:
            if authorized_line:
                remaining = [line for line in authorized.read_text().splitlines(keepends=True)
                             if line != authorized_line]
                authorized.write_text(''.join(remaining))
                assert authorized_line not in authorized.read_text()
            report['cleanup'] = {'temporary_authorized_key_removed': True}
            Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    report['cleanup']['project_credentials_and_repositories_removed'] = not temporary.exists()
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dev-only', action='store_true', required=True)
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--scenario')
    parser.add_argument('--repository')
    parser.add_argument('--source')
    parser.add_argument('--sftp-root')
    parser.add_argument('--image', default='localhost/sophon:latest')
    parser.add_argument('--output')
    args = parser.parse_args()
    inside(args) if args.inside else host(args)
