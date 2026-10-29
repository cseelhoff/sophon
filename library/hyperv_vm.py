#!/usr/bin/python

"""Provision a blank, console-installed InfraVM on Hyper-V over WinRM (PowerShell remoting)."""

import base64
import hashlib
import json
import ntpath
from pathlib import Path
import re

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r'''
---
module: hyperv_vm
short_description: Authenticate to a Hyper-V host or provision a two-ISO Sophon InfraVM
description:
  - Runs the Hyper-V PowerShell cmdlets on the host through WinRM (pypsrp); nothing is installed on the host.
  - Existing marked VMs are started without modifying their disks or media.
  - Unmarked, ambiguous and incomplete VMs are never adopted.
options:
  action:
    description: Authenticate without changes, or provision and start InfraVM.
    type: str
    choices: [authenticate, provision]
    default: provision
  host:
    description: Hyper-V host name or address reachable over WinRM.
    type: str
    required: true
  port:
    description: WinRM port. 5985 is HTTP (NTLM message encryption), 5986 is HTTPS with a verified certificate.
    type: int
    default: 5985
  username:
    description: Account in Administrators or Hyper-V Administrators, for example `Administrator` or `DOMAIN\\user`.
    type: str
    required: true
  password:
    description: Account password, sent only inside the WinRM session.
    type: str
    required: true
  ca_file:
    description: Optional CA bundle that verifies the WinRM HTTPS certificate (port 5986). Verification is never disabled.
    type: path
  switch:
    description: Existing virtual switch to connect InfraVM to (it must already carry the right VLAN).
    type: str
    required: true
  path:
    description: Folder on the host for the VM, its disk and the ISOs. Empty uses the host's default VM path.
    type: str
  name:
    description: VM name.
    type: str
    default: sophon-infravm
  cores:
    type: int
    default: 8
  memory:
    description: Startup memory in MiB (static).
    type: int
    default: 8192
  disk_gb:
    description: Dynamically expanding disk capacity in GiB.
    type: int
    default: 100
  coreos_iso:
    description: Path of the Sophon installer ISO (the stock CoreOS live ISO with the unattended installer baked in) on this machine.
    type: path
  bundle_iso:
    description: Path of the Sophon deployment ISO on this machine.
    type: path
'''

MARKER = 'sophon-infravm-two-iso-v1'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
# No wildcard characters: the cmdlets would treat them as patterns.
SWITCH = re.compile(r'[^\x00-\x1f*?\[\]]{1,128}')
WINDOWS_PATH = re.compile(r'[A-Za-z]:\\[^\x00-\x1f<>"|?*]*')

# Every script receives its arguments as base64 JSON, so no value is ever interpolated into PowerShell source.
PRELUDE = (
    "$ErrorActionPreference = 'Stop'\n"
    "$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json\n"
)

AUTHENTICATE = r'''
$h = Get-VMHost
$s = Get-VMSwitch | Where-Object { $_.Name -eq $p.switch }
if (-not $s) { throw "Virtual switch '$($p.switch)' does not exist on this host." }
[pscustomobject]@{ host = $h.Name; vm_path = $h.VirtualMachinePath } | ConvertTo-Json -Compress
'''

FIND = r'''
$vms = @(Get-VM | Where-Object { $_.Name -eq $p.name })
$vm = $vms | Select-Object -First 1
[pscustomobject]@{ count = $vms.Count; state = [string]$vm.State; notes = [string]$vm.Notes } | ConvertTo-Json -Compress
'''

STAT_ISOS = r'''
New-Item -ItemType Directory -Force -Path $p.iso_dir | Out-Null
$found = foreach ($name in $p.names) {
    $item = Get-Item -LiteralPath ([IO.Path]::Combine($p.iso_dir, $name)) -ErrorAction SilentlyContinue
    [pscustomobject]@{ name = $name; size = $(if ($item) { [int64]$item.Length } else { -1 }) }
}
ConvertTo-Json -InputObject @($found) -Compress
'''

FINALIZE_ISO = r'''
$part = [IO.Path]::Combine($p.iso_dir, $p.name + '.part')
$actual = (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $p.sha256) {
    Remove-Item -LiteralPath $part -Force
    throw 'Uploaded ISO failed SHA-256 verification and was removed.'
}
Move-Item -LiteralPath $part -Destination ([IO.Path]::Combine($p.iso_dir, $p.name)) -Force
'''

CREATE = r'''
New-Item -ItemType Directory -Force -Path $p.path | Out-Null
$coreos = [IO.Path]::Combine($p.iso_dir, $p.coreos_name)
$bundle = [IO.Path]::Combine($p.iso_dir, $p.bundle_name)
$vm = New-VM -Name $p.name -Generation 2 -MemoryStartupBytes ([int64]$p.memory_mb * 1MB) `
    -Path $p.path -NewVHDPath ([IO.Path]::Combine($p.path, $p.name + '.vhdx')) -NewVHDSizeBytes ([int64]$p.disk_gb * 1GB) `
    -SwitchName $p.switch
# The marker stays ':preparing' until the hardware is complete, so a half-built VM is never adopted.
Set-VM -VM $vm -StaticMemory -AutomaticCheckpointsEnabled $false -CheckpointType Disabled -Notes ($p.marker + ':preparing')
Set-VMProcessor -VM $vm -Count $p.cores
Set-VMFirmware -VM $vm -EnableSecureBoot Off
$null = Add-VMDvdDrive -VM $vm -Path $coreos
$null = Add-VMDvdDrive -VM $vm -Path $bundle
$disk = Get-VMHardDiskDrive -VM $vm
$installer = Get-VMDvdDrive -VM $vm | Where-Object { $_.Path -eq $coreos }
Set-VMFirmware -VM $vm -BootOrder $disk, $installer
Set-VM -VM $vm -Notes $p.marker
Start-VM -VM $vm
'''

START = r'''
$vm = Get-VM | Where-Object { $_.Name -eq $p.name }
Start-VM -VM $vm
'''


def script(body, **arguments):
    payload = base64.b64encode(json.dumps(arguments).encode()).decode()
    return PRELUDE.replace('{payload}', payload) + body


def provision(params, host, info):
    """Create (or start) InfraVM. `host` offers run(script, **args) -> parsed JSON and upload(local, remote)."""
    name = params['name']
    if not NAME.fullmatch(name):
        raise ValueError('VM name must contain 1-80 letters, digits, dots, underscores or hyphens.')
    if params['cores'] < 1 or params['memory'] < 1024 or params['disk_gb'] < 40:
        raise ValueError('Provide valid CPU/memory and at least 40 GiB of disk space.')
    path = params.get('path') or info['vm_path']
    if not WINDOWS_PATH.fullmatch(path):
        raise ValueError('The VM folder must be an absolute Windows path such as D:\\Hyper-V.')

    found = host.run(script(FIND, name=name))
    if found['count'] > 1:
        raise ValueError('Multiple VMs use this name. No VMs have been changed.')
    if found['count'] == 1:
        if found['notes'] != MARKER:
            raise ValueError('Existing VM is unmanaged or an incomplete bootstrap. It has not been changed.')
        if found['state'] not in ('Running', 'Off'):
            raise ValueError(f"Existing VM state is {found['state']}; inspect it before continuing.")
        if found['state'] == 'Off':
            host.run(script(START, name=name))
        return {'changed': found['state'] == 'Off', 'vm_path': f"{info['host']}/{name}", 'created': False}

    media = []
    for label, source in (('coreos', params['coreos_iso']), ('deployment', params['bundle_iso'])):
        source = Path(source)
        if not source.is_file():
            raise ValueError(f'Missing installation ISO: {source}')
        with source.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        media.append((source, f'sophon-{label}-{digest}.iso', digest, source.stat().st_size))

    iso_dir = ntpath.join(path, 'sophon-isos')
    existing = {item['name']: item['size']
                for item in host.run(script(STAT_ISOS, iso_dir=iso_dir, names=[item[1] for item in media]))}
    for source, filename, digest, size in media:
        if existing[filename] == size:
            continue
        if existing[filename] != -1:
            raise ValueError('A hash-named ISO has an unexpected size on the host; refusing to overwrite it.')
        host.upload(str(source), ntpath.join(iso_dir, filename + '.part'))
        host.run(script(FINALIZE_ISO, iso_dir=iso_dir, name=filename, sha256=digest))

    host.run(script(CREATE, name=name, path=path, iso_dir=iso_dir, switch=params['switch'],
                    memory_mb=params['memory'], disk_gb=params['disk_gb'], cores=params['cores'],
                    marker=MARKER, coreos_name=media[0][1], bundle_name=media[1][1]))
    return {'changed': True, 'vm_path': f"{info['host']}/{name}", 'created': True}


class WinRMHost:
    def __init__(self, params):
        from pypsrp.client import Client
        self.address = f"{params['host']}:{params['port']}"
        secure = params['port'] == 5986
        self.client = Client(
            params['host'], username=params['username'], password=params['password'], port=params['port'],
            ssl=secure, auth='negotiate', encryption='auto',
            cert_validation=(params['ca_file'] or True) if secure else True)

    def call(self, function, *arguments):
        from requests.exceptions import RequestException
        try:
            return function(*arguments)
        except RequestException as error:
            raise ValueError(
                f'Cannot reach WinRM on {self.address} ({type(error).__name__}). Enable it on the host '
                '(Enable-PSRemoting -Force), allow the port through Windows Firewall for this machine, '
                f'and check the address. Details: {str(error)[:200]}') from None

    def run(self, source):
        output, streams, had_errors = self.call(self.client.execute_ps, source)
        if had_errors:
            raise ValueError('Hyper-V host reported: ' + '; '.join(str(error) for error in streams.error))
        return json.loads(output) if output.strip() else {}

    def upload(self, local, remote):
        self.call(self.client.copy, local, remote)


def main():
    module = AnsibleModule(argument_spec={
        'action': {'choices': ['authenticate', 'provision'], 'default': 'provision'},
        'host': {'required': True}, 'port': {'type': 'int', 'default': 5985},
        'username': {'required': True}, 'password': {'required': True, 'no_log': True},
        'ca_file': {'type': 'path', 'default': ''}, 'switch': {'required': True}, 'path': {'default': ''},
        'name': {'default': 'sophon-infravm'}, 'cores': {'type': 'int', 'default': 8},
        'memory': {'type': 'int', 'default': 8192}, 'disk_gb': {'type': 'int', 'default': 100},
        'coreos_iso': {'type': 'path'}, 'bundle_iso': {'type': 'path'},
    }, required_if=[('action', 'provision', ['coreos_iso', 'bundle_iso'])])
    params = module.params
    try:
        if not SWITCH.fullmatch(params['switch']):
            raise ValueError('The virtual switch name contains unsupported characters.')
        try:
            host = WinRMHost(params)
        except ImportError:
            raise ValueError('pypsrp is missing; run this through the sophon-tools container.')
        info = host.run(script(AUTHENTICATE, switch=params['switch']))
        result = ({'changed': False, 'server': info['host']}
                  if params['action'] == 'authenticate' else provision(params, host, info))
    except (OSError, ValueError, KeyError) as error:
        module.fail_json(msg=str(error))
    except Exception as error:  # pypsrp raises its own transport/authentication errors
        module.fail_json(msg=f'Could not run PowerShell on the Hyper-V host: {type(error).__name__}: {error}')
    module.exit_json(**result)


if __name__ == '__main__':
    main()
