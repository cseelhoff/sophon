"""Pair and preflight SFTP before Configure deploys any stacks."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import paramiko

MATERIAL = ('SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64', 'SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64')
NOT_INITIALIZED = 'error connecting to repository: repository not initialized in the provided storage'


def private_file(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def redact(detail, values):
    for name, value in values.items():
        if value and any(word in name for word in ('PASSWORD', 'SECRET', 'TOKEN', 'PRIVATE_KEY')):
            detail = detail.replace(value, '[redacted]')
    return detail


def repository(values, directory, keyfile, known_hosts, operation):
    environment = dict(os.environ, KOPIA_PASSWORD=values['SOPHON_KOPIA_PASSWORD'],
                       KOPIA_CONFIG_PATH=str(directory / 'repository.config'),
                       KOPIA_CACHE_DIRECTORY=str(directory / 'cache'),
                       KOPIA_LOG_DIR=str(directory / 'logs'))
    arguments = [
        'kopia', '--file-log-level=error', '--disable-content-log', 'repository', operation, 'sftp',
        '--host=' + values['SOPHON_KOPIA_SFTP_HOST'],
        '--port=' + values['SOPHON_KOPIA_SFTP_PORT'],
        '--username=' + values['SOPHON_KOPIA_SFTP_USERNAME'],
        '--path=' + values['SOPHON_KOPIA_SFTP_PATH'],
        '--keyfile=' + str(keyfile), '--known-hosts=' + str(known_hosts),
        '--no-embed-credentials', '--no-persist-credentials', '--no-use-keyring', '--no-check-for-updates',
    ]
    if operation == 'connect':
        arguments.append('--readonly')
    return subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=180)


def check_repository_result(result, values):
    if result.returncode:
        raise RuntimeError('Kopia repository preflight failed: ' + redact(result.stderr.strip(), values))


def free_space(values, keyfile, known_hosts):
    path = values['SOPHON_KOPIA_SFTP_PATH'].replace('\\', '\\\\').replace('"', '\\"')
    result = subprocess.run([
        'sftp', '-F', '/dev/null', '-q', '-b', '-', '-P', values['SOPHON_KOPIA_SFTP_PORT'],
        '-i', str(keyfile), '-oBatchMode=yes', '-oIdentitiesOnly=yes', '-oIdentityAgent=none',
        '-oStrictHostKeyChecking=yes', '-oGlobalKnownHostsFile=/dev/null',
        '-oUserKnownHostsFile=' + str(known_hosts), '-oConnectTimeout=10',
        '-oUser=' + values['SOPHON_KOPIA_SFTP_USERNAME'], values['SOPHON_KOPIA_SFTP_HOST'],
    ], input=f'df "{path}"\n', capture_output=True, text=True, timeout=30)
    if result.returncode:
        if result.stderr.strip() == 'Server does not support statvfs@openssh.com extension':
            return None
        raise ValueError('SFTP free-space query failed: ' + redact(result.stderr.strip(), values))
    row = re.search(r'^\s*\d+\s+\d+\s+(\d+)\s+\d+\s+\d+%\s*$', result.stdout, re.MULTILINE)
    if row is None:
        raise ValueError('SFTP free-space query returned an unrecognized response.')
    return int(row[1]) * 1024


def unused_destination(sftp, path):
    entries = sftp.listdir(path)
    if not entries:
        return True
    if entries != ['.shards']:
        return False
    marker = path.rstrip('/') + '/.shards'
    attributes = sftp.lstat(marker)
    if not stat.S_ISREG(attributes.st_mode) or attributes.st_size > 1024:
        return False
    with sftp.open(marker, 'rb') as stream:
        data = stream.read(1024)
    try:
        return json.loads(data) == {'default': [3, 3], 'maxNonShardedLength': 20}
    except (ValueError, UnicodeDecodeError):
        return False


def host_keys(path, destination):
    keys = paramiko.HostKeys(str(path)).lookup(destination)
    if not keys:
        raise ValueError(f'No host keys supplied for {destination}.')
    return {kind: key.asbytes() for kind, key in keys.items()}


def prepare(values, *, root, confirm=None):
    """Return deployable SSH material after authenticated SFTP and Kopia checks."""
    values = dict(values)
    state = Path(root)
    for part in ('credentials', 'kopia'):
        state /= part
        if state.is_symlink():
            raise ValueError('Backup client state must not be a symlink.')
        state.mkdir(mode=0o700, exist_ok=True)
        state.chmod(0o700)
    host = values['SOPHON_KOPIA_SFTP_HOST']
    port = int(values['SOPHON_KOPIA_SFTP_PORT'])
    destination = host if port == 22 else f'[{host}]:{port}'
    pin = state / (hashlib.sha256(destination.encode()).hexdigest() + '.known_hosts')
    identity = state / 'id_ed25519'
    if pin.is_symlink() or identity.is_symlink():
        raise ValueError('Backup client key/pin files must not be symlinks.')
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        keyfile = directory / 'identity'
        generated = False
        if values[MATERIAL[0]]:
            private_file(keyfile, base64.b64decode(values[MATERIAL[0]], validate=True))
        else:
            if not identity.exists():
                key = Ed25519PrivateKey.generate()
                private_file(identity, key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
                generated = True
            identity.chmod(0o600)
            private_file(keyfile, identity.read_bytes())
        try:
            key = paramiko.PKey.from_path(keyfile)
        except (ValueError, TypeError, paramiko.SSHException):
            raise ValueError('Supply a valid unencrypted SSH private key or repair the saved client identity.') from None
        print('Authorize this line on the confined SFTP destination account:')
        print(f'restrict {key.get_name()} {key.get_base64()} sophon-backup')
        known_hosts = directory / 'known_hosts'
        scan = subprocess.run(['ssh-keyscan', '-T', '10', '-t', 'rsa,ecdsa,ed25519', '-p', str(port), host],
                              capture_output=True, text=True, timeout=45)
        if scan.returncode or not scan.stdout.strip():
            raise ValueError(f'Cannot scan SSH host keys at {destination}; check reachability.')
        scanned = directory / 'scanned'
        private_file(scanned, scan.stdout.encode())
        observed = host_keys(scanned, destination)
        supplied = values[MATERIAL[1]]
        if supplied:
            private_file(known_hosts, base64.b64decode(supplied, validate=True))
        elif pin.exists():
            private_file(known_hosts, pin.read_bytes())
        else:
            private_file(known_hosts, scanned.read_bytes())
            print(f'Independently verify ALL {destination} fingerprints against the server console:')
            for kind, material in observed.items():
                fingerprint = base64.b64encode(hashlib.sha256(material).digest()).decode().rstrip('=')
                print(f'{kind} SHA256:{fingerprint}')
            if confirm is None or not confirm('Have you independently verified ALL these host key fingerprints?'):
                raise ValueError('SFTP host keys were not approved; no destination changes were made.')
        if observed != host_keys(known_hosts, destination):
            raise ValueError('SFTP host key set changed or supplied pins are incomplete; independently verify before re-pairing.')
        if pin.exists():
            if observed != host_keys(pin, destination):
                raise ValueError('SFTP host key changed; an override cannot replace saved trust.')
        else:
            private_file(pin, known_hosts.read_bytes())
        if generated and confirm is not None and not confirm('Install the displayed public key on the server, then continue?'):
            raise ValueError('SFTP pairing cancelled; the generated identity is retained for retry.')
        with paramiko.SSHClient() as client:
            client.load_host_keys(str(known_hosts))
            try:
                client.connect(host, port=port, username=values['SOPHON_KOPIA_SFTP_USERNAME'], pkey=key,
                               look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=10, banner_timeout=10)
            except paramiko.BadHostKeyException:
                raise ValueError('SFTP host key changed during authentication; verify it independently.') from None
            except paramiko.AuthenticationException:
                raise ValueError('SFTP authentication failed. Install the displayed public key, then rerun Configure.') from None
            except paramiko.SSHException:
                raise ValueError('SFTP SSH handshake failed; check the pinned host keys and server configuration.') from None
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(30)
                path = values['SOPHON_KOPIA_SFTP_PATH']
                if not stat.S_ISDIR(sftp.stat(path).st_mode):
                    raise ValueError('The SFTP repository path is not a directory.')
                available = free_space(values, keyfile, known_hosts)
                payload = os.urandom(64)
                if available is not None and available < len(payload):
                    raise ValueError('The SFTP destination has insufficient free space for the write check.')
                probe = path.rstrip('/') + '/.sophon-preflight-' + uuid.uuid4().hex
                created = False
                try:
                    with sftp.open(probe, 'wx') as stream:
                        created = True
                        stream.write(payload)
                    with sftp.open(probe, 'rb') as stream:
                        if stream.read() != payload:
                            raise ValueError('SFTP read/write integrity check failed.')
                finally:
                    if created:
                        sftp.remove(probe)
                print('SFTP reachability/authentication/read/write/delete passed; ' +
                      (f'{available // (1024 * 1024)} MiB available (not a backup-size guarantee).'
                       if available is not None else 'the server does not report free space.'))
                result = repository(values, directory, keyfile, known_hosts, 'connect')
                created_repository = False
                if result.returncode and result.stderr.strip() == NOT_INITIALIZED:
                    if not unused_destination(sftp, path):
                        raise ValueError('No Kopia repository found, but the destination is not empty; refusing creation.')
                    approved = values.get('SOPHON_KOPIA_CREATE_REPOSITORY') == 'true'
                    if not approved and (confirm is None or not confirm('No repository found here. Create a new encrypted repository?')):
                        raise ValueError('Repository creation requires explicit confirmation or SOPHON_KOPIA_CREATE_REPOSITORY=true.')
                    if not unused_destination(sftp, path):
                        raise ValueError('The destination changed during confirmation; refusing repository creation.')
                    check_repository_result(repository(values, directory, keyfile, known_hosts, 'create'), values)
                    result = repository(values, directory, keyfile, known_hosts, 'connect')
                    created_repository = True
                check_repository_result(result, values)
                if (not created_repository and generated and confirm is not None
                        and values.get('SOPHON_KOPIA_BACKUPS_ENABLED') == 'true'):
                    if not confirm('Existing backup repository found on this new VM. Start scheduled backups immediately (answer n if restoring first)?'):
                        values['SOPHON_KOPIA_BACKUPS_ENABLED'] = 'false'
        values[MATERIAL[0]] = base64.b64encode(keyfile.read_bytes()).decode()
        values[MATERIAL[1]] = base64.b64encode(known_hosts.read_bytes()).decode()
    print('Kopia repository preflight passed. Service deployment can proceed.')
    return values
