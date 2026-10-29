#!/usr/bin/python

"""Provision a blank, console-installed InfraVM using VMware's native govc CLI."""

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r'''
---
module: vcenter_vm
short_description: Authenticate to vCenter or provision a two-ISO Sophon InfraVM
description:
  - Uses govc with credentials in its environment, never its arguments.
  - Existing marked VMs are started without modifying their disks or media.
  - Unmarked, template, ambiguous and incomplete VMs are never adopted.
options:
  action:
    description: Authenticate without changes, or provision and start InfraVM.
    type: str
    choices: [authenticate, provision]
    default: provision
  url:
    description: HTTPS vCenter URL without embedded credentials.
    type: str
    required: true
  username:
    description: Account with VM creation, network assignment and datastore upload permissions.
    type: str
    required: true
  password:
    description: vCenter password, passed to govc only through its environment.
    type: str
    required: true
  ca_file:
    description: Optional independently verified CA PEM on the bootstrap laptop.
    type: path
    default: ''
  datacenter:
    description: Existing vCenter datacenter name.
    type: str
    required: true
  cluster:
    description: Existing cluster used for DRS placement of a new VM.
    type: str
  datastore:
    description: Shared datastore for immutable installation ISOs and the new thin disk.
    type: str
  network:
    description: Existing standard or distributed port group, already configured for its VLAN.
    type: str
  folder:
    description: Existing absolute VM inventory folder; empty uses the datacenter VM root.
    type: str
    default: ''
  name:
    description: InfraVM inventory name, unique within the selected folder tree.
    type: str
    default: sophon-infravm
  cores:
    description: CPU count for a new VM.
    type: int
    default: 8
  memory:
    description: Memory in MiB for a new VM.
    type: int
    default: 8192
  disk_gb:
    description: Fresh thin-provisioned disk capacity in GiB.
    type: int
    default: 100
  coreos_iso:
    description: Local Sophon installer ISO (the stock Fedora CoreOS live ISO with the unattended installer baked in).
    type: path
  bundle_iso:
    description: Local reusable Sophon artifact ISO.
    type: path
requirements: [govc]
author: Sophon
'''

MARKER = 'sophon-infravm-two-iso-v1'


def connection_environment(params):
    url = urlsplit(params['url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path not in ('', '/', '/sdk') or url.query or url.fragment):
        raise ValueError('vCenter URL must be HTTPS, without embedded credentials, query or fragment.')
    return {
        'GOVC_URL': params['url'], 'GOVC_USERNAME': params['username'],
        'GOVC_PASSWORD': params['password'], 'GOVC_DATACENTER': params['datacenter'],
        'GOVC_DATASTORE': params.get('datastore') or '',
        'GOVC_TLS_CA_CERTS': params.get('ca_file') or '',
        'GOVC_INSECURE': 'false', 'GOVC_PERSIST_SESSION': 'false',
        'GOVC_DEBUG': 'false', 'GOVC_TRACE': 'false', 'GOVC_VERBOSE': 'false',
    }


def provision(params, run):
    name = params['name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', name):
        raise ValueError('VM name must contain 1-80 letters, digits, dots, underscores or hyphens.')
    if params['cores'] < 1 or params['memory'] < 1024 or params['disk_gb'] < 40:
        raise ValueError('Provide valid CPU/memory and at least 40 GiB of disk space.')
    folder = params['folder'] or f"/{params['datacenter']}/vm"
    paths = run('find', folder, '-type', 'm', '-name', name).splitlines()
    if len(paths) > 1:
        raise ValueError('Multiple VMs use this name in the selected folder. No VMs have been changed.')
    if paths:
        vm = json.loads(run('vm.info', '-json', paths[0]))['virtualMachines'][0]
        if vm['config'].get('template') or vm['config'].get('annotation') != MARKER:
            raise ValueError('Existing VM is unmanaged, a template or an incomplete bootstrap. It has not been changed.')
        power = vm['runtime']['powerState']
        if power not in ('poweredOn', 'poweredOff'):
            raise ValueError(f'Existing VM power state is {power}; inspect it before continuing.')
        if power == 'poweredOff':
            run('vm.power', '-on', paths[0])
        return {'changed': power == 'poweredOff', 'vm_path': paths[0], 'created': False}

    media = []
    for label, source in (('coreos', params['coreos_iso']), ('deployment', params['bundle_iso'])):
        path = Path(source)
        if not path.is_file():
            raise ValueError(f'Missing installation ISO: {path}')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        media.append((path, f'sophon-{label}-{digest}.iso', path.stat().st_size))

    run('datastore.mkdir', '-p', 'sophon-isos')
    listing = json.loads(run('datastore.ls', '-json', '-l', 'sophon-isos/'))
    files = {entry['path']: entry for directory in listing for entry in directory.get('file', [])}
    for source, filename, size in media:
        if filename in files:
            if files[filename].get('fileSize') != size:
                raise ValueError('A hash-named ISO has an unexpected datastore size; refusing to overwrite it.')
        else:
            run('datastore.upload', str(source), f'sophon-isos/{filename}')

    vm_path = f'{folder.rstrip("/")}/{name}'
    run('vm.create', '-on=false', '-annotation', MARKER + ':preparing',
        '-folder', folder, '-cluster', params['cluster'], '-ds', params['datastore'],
        '-net', params['network'], '-net.adapter', 'vmxnet3', '-g', 'fedora64Guest',
        '-c', str(params['cores']), '-m', str(params['memory']),
        '-disk', f"{params['disk_gb']}G", '-disk.controller', 'pvscsi', '-disk.thick=false',
        '-iso', f'sophon-isos/{media[0][1]}', name)
    device = run('device.cdrom.add', '-vm', vm_path).strip()
    if not device:
        raise ValueError('vCenter did not identify the new artifact CD drive; VM remains marked incomplete.')
    run('device.cdrom.insert', '-vm', vm_path, '-device', device, f'sophon-isos/{media[1][1]}')
    run('device.boot', '-vm', vm_path, '-order', 'disk,cdrom')
    run('vm.change', '-vm', vm_path, '-annotation', MARKER)
    run('vm.power', '-on', vm_path)
    return {'changed': True, 'vm_path': vm_path, 'created': True}


def main():
    module = AnsibleModule(argument_spec={
        'action': {'choices': ['authenticate', 'provision'], 'default': 'provision'},
        'url': {'required': True}, 'username': {'required': True},
        'password': {'required': True, 'no_log': True},
        'ca_file': {'type': 'path', 'default': ''}, 'datacenter': {'required': True},
        'cluster': {}, 'datastore': {}, 'network': {}, 'folder': {'default': ''},
        'name': {'default': 'sophon-infravm'}, 'cores': {'type': 'int', 'default': 8},
        'memory': {'type': 'int', 'default': 8192}, 'disk_gb': {'type': 'int', 'default': 100},
        'coreos_iso': {'type': 'path'}, 'bundle_iso': {'type': 'path'},
    }, required_if=[('action', 'provision', ['cluster', 'datastore', 'network', 'coreos_iso', 'bundle_iso'])])
    binary = module.get_bin_path('govc', required=True)
    try:
        environment = connection_environment(module.params)
        # Do not inherit alternate credentials, session caches or debug settings.
        for key in tuple(os.environ):
            if key.startswith('GOVC_'):
                del os.environ[key]

        def run(*arguments):
            rc, stdout, stderr = module.run_command([binary, *arguments], environ_update=environment)
            if rc:
                module.fail_json(msg=f'govc {arguments[0]} failed: {stderr.strip()}', rc=rc)
            return stdout

        about = json.loads(run('about', '-json'))['about']
        if about['apiType'] != 'VirtualCenter':
            raise ValueError('Select a vCenter Server, not a standalone ESXi endpoint.')
        result = ({'changed': False, 'server': about['fullName']}
                  if module.params['action'] == 'authenticate' else provision(module.params, run))
    except (OSError, ValueError, KeyError) as error:
        module.fail_json(msg=str(error))
    module.exit_json(**result)


if __name__ == '__main__':
    main()
