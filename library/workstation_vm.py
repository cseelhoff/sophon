#!/usr/bin/python

"""Request VMware Workstation provisioning from the Windows host helper."""

import json
import os
from pathlib import Path
import re
import time
import uuid

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r'''
---
module: workstation_vm
short_description: Provision a two-ISO Sophon InfraVM on VMware Workstation
description:
  - Sends a bounded request through the shared state directory to the Windows launcher helper.
  - Existing VMs are only started if this workflow marked them.
options:
  action:
    type: str
    choices: [authenticate, provision]
    default: provision
  name:
    type: str
    default: sophon-infravm
  cores:
    type: int
    default: 8
  memory:
    type: int
    default: 8192
  disk_gb:
    type: int
    default: 100
  network:
    type: str
    choices: [bridged, nat, hostonly]
    default: bridged
  coreos_iso:
    type: path
  bundle_iso:
    type: path
requirements: [Windows launcher helper, VMware Workstation Pro]
author: Sophon
'''


def request_host(action, params, state_dir, timeout=1800):
    root = Path(state_dir) / 'workstation'
    requests = root / 'requests'
    responses = root / 'responses'
    requests.mkdir(parents=True, exist_ok=True)
    responses.mkdir(parents=True, exist_ok=True)
    request_id = str(uuid.uuid4())
    request_path = requests / f'{request_id}.json'
    response_path = responses / f'{request_id}.json'
    payload = {'id': request_id, 'action': action}
    if action == 'provision':
        payload.update({
            'name': params['name'], 'cores': params['cores'], 'memory': params['memory'],
            'disk_gb': params['disk_gb'], 'network': params['network'],
            'coreos_iso': Path(params['coreos_iso']).relative_to('/workspace').as_posix(),
            'bundle_iso': Path(params['bundle_iso']).relative_to('/workspace').as_posix(),
        })
    temporary_path = request_path.with_suffix('.tmp')
    temporary_path.write_text(json.dumps(payload), encoding='utf-8')
    temporary_path.replace(request_path)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if response_path.is_file():
                result = json.loads(response_path.read_text(encoding='utf-8-sig'))
                if not result.get('ok'):
                    raise ValueError(result.get('error', 'Workstation helper failed.'))
                return result
            time.sleep(0.5)
        raise TimeoutError('Timed out waiting for the Windows Workstation helper.')
    finally:
        request_path.unlink(missing_ok=True)
        response_path.unlink(missing_ok=True)


def main():
    module = AnsibleModule(argument_spec={
        'action': {'choices': ['authenticate', 'provision'], 'default': 'provision'},
        'name': {'default': 'sophon-infravm'}, 'cores': {'type': 'int', 'default': 8},
        'memory': {'type': 'int', 'default': 8192}, 'disk_gb': {'type': 'int', 'default': 100},
        'network': {'choices': ['bridged', 'nat', 'hostonly'], 'default': 'bridged'},
        'coreos_iso': {'type': 'path'}, 'bundle_iso': {'type': 'path'},
    }, required_if=[('action', 'provision', ['coreos_iso', 'bundle_iso'])])
    state_dir = os.environ.get('SOPHON_STATE_DIR', '/state')
    try:
        result = request_host(module.params['action'], module.params, state_dir)
    except (OSError, ValueError, TimeoutError) as error:
        module.fail_json(msg=str(error))
    result['changed'] = bool(result.get('changed'))
    module.exit_json(**result)


if __name__ == '__main__':
    main()