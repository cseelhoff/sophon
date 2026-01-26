#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2026
# GNU General Public License v3.0+

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: proxmox_shell

short_description: Execute commands on Proxmox VE node, VM, or container via termproxy WebSocket

version_added: "1.0.0"

description:
    - Execute shell commands on a Proxmox VE node, QEMU VM, or LXC container using the termproxy/vncwebsocket API.
    - Requires pre-authenticated PVE ticket and CSRF token (obtain via /api2/json/access/ticket).
    - For VMs/containers, specify vmid and optionally vmtype.

options:
    api_host:
        description: Proxmox VE hostname or IP
        required: true
        type: str
    api_port:
        description: Proxmox VE API port
        type: int
        default: 8006
    api_user:
        description: Proxmox VE user (e.g., root@pam)
        required: true
        type: str
    node:
        description: Target Proxmox node name
        required: true
        type: str
    vmid:
        description: VM or container ID. If not specified, connects to node shell.
        type: int
        required: false
    vmtype:
        description: Type of VM (qemu or lxc). Only used when vmid is specified.
        type: str
        choices: ['qemu', 'lxc']
        default: qemu
    vm_user:
        description: Username for VM/container login. Required for VMs that present a login prompt.
        type: str
        required: false
    vm_password:
        description: Password for VM/container login. Required for VMs that present a login prompt.
        type: str
        required: false
        no_log: true
    command:
        description: Shell command to execute
        required: true
        type: str
    pve_auth_cookie:
        description: PVE authentication ticket/cookie (from /api2/json/access/ticket)
        required: true
        type: str
        no_log: true
    proxmox_csrf_token:
        description: Proxmox CSRF prevention token (from /api2/json/access/ticket)
        required: true
        type: str
        no_log: true
    timeout:
        description: Command execution timeout in seconds
        type: int
        default: 30
    validate_certs:
        description: Validate SSL certificates
        type: bool
        default: true

requirements:
    - websocket-client (pip install websocket-client)

author:
    - Proxmox Shell Module
'''

EXAMPLES = r'''
# First obtain ticket in playbook:
- name: Obtain Proxmox API ticket
  ansible.builtin.uri:
    url: "https://{{ api_host }}:{{ api_port }}/api2/json/access/ticket"
    method: POST
    body:
      username: "{{ api_user }}"
      password: "{{ api_password }}"
    body_format: form-urlencoded
    validate_certs: false
  register: pve_ticket

# Run command on Proxmox node shell:
- name: Run uptime on Proxmox node
  proxmox_shell:
    api_host: "{{ api_host }}"
    node: "pve"
    pve_auth_cookie: "{{ pve_ticket.json.data.ticket }}"
    proxmox_csrf_token: "{{ pve_ticket.json.data.CSRFPreventionToken }}"
    command: uptime
  register: result

# Run command on a QEMU VM console:
- name: Run command on VM 100
  proxmox_shell:
    api_host: "{{ api_host }}"
    node: "pve"
    vmid: 100
    vmtype: qemu
    vm_user: root
    vm_password: "{{ vm_root_password }}"
    pve_auth_cookie: "{{ pve_ticket.json.data.ticket }}"
    proxmox_csrf_token: "{{ pve_ticket.json.data.CSRFPreventionToken }}"
    command: hostname
  register: result

# Run command on an LXC container:
- name: Run command on container 101
  proxmox_shell:
    api_host: "{{ api_host }}"
    node: "pve"
    vmid: 101
    vmtype: lxc
    vm_user: root
    vm_password: "{{ container_root_password }}"
    pve_auth_cookie: "{{ pve_ticket.json.data.ticket }}"
    proxmox_csrf_token: "{{ pve_ticket.json.data.CSRFPreventionToken }}"
    command: cat /etc/os-release
  register: result

- debug:
    var: result.stdout
'''

RETURN = r'''
stdout:
    description: Command output (cleaned of ANSI codes)
    type: str
    returned: always
stdout_raw:
    description: Raw command output including ANSI codes
    type: str
    returned: always
changed:
    description: Always true since we executed a command
    type: bool
    returned: always
'''

import json
import re
import ssl
import time
from urllib.parse import urlencode

from ansible.module_utils.basic import AnsibleModule

# Check for websocket-client
HAS_WEBSOCKET = True
WEBSOCKET_IMPORT_ERROR = None
try:
    import websocket
except ImportError as e:
    HAS_WEBSOCKET = False
    WEBSOCKET_IMPORT_ERROR = str(e)

# For Python 2/3 compatibility
try:
    from http.client import HTTPSConnection
except ImportError:
    from httplib import HTTPSConnection


def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    # Match various ANSI escape sequences:
    # - CSI sequences: \x1b[...X (where X is a letter)
    # - OSC sequences: \x1b]...\x1b\\ or \x1b]...\x07
    # - Character set: \x1b(X or \x1b)X
    # - Private modes: \x1b[?...h or \x1b[?...l
    ansi_pattern = re.compile(
        r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC sequences (e.g., \x1b]3008;...\x1b\\)
        r'|\x1b\[\?[0-9;]*[hl]'   # Private mode set/reset (e.g., \x1b[?2004h)
        r'|\x1b\[[0-9;]*[a-zA-Z]'  # Standard CSI sequences
        r'|\x1b[()][AB012]'        # Character set selection
        r'|\x1b[=>]'               # Keypad modes
    )
    return ansi_pattern.sub('', text)


def create_term_session(api_host, api_port, node, pve_auth_cookie, pve_csrf_token, validate_certs, vmid=None, vmtype='qemu'):
    """
    Create a terminal proxy session.
    - Node shell: POST /api2/json/nodes/{node}/termproxy
    - QEMU VM: POST /api2/json/nodes/{node}/qemu/{vmid}/termproxy
    - LXC container: POST /api2/json/nodes/{node}/lxc/{vmid}/termproxy
    Returns dict with 'port' and 'ticket' (vncticket)
    """
    context = None
    if not validate_certs:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    conn = HTTPSConnection(api_host, api_port, context=context)
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Cookie': f'PVEAuthCookie={pve_auth_cookie}',
        'CSRFPreventionToken': pve_csrf_token
    }
    
    # Build path based on target type
    if vmid is not None:
        path = f'/api2/json/nodes/{node}/{vmtype}/{vmid}/termproxy'
    else:
        path = f'/api2/json/nodes/{node}/termproxy'
    
    conn.request('POST', path, body='', headers=headers)
    response = conn.getresponse()
    body = response.read().decode('utf-8')
    conn.close()
    
    if response.status != 200:
        raise Exception(f'Failed to create terminal session: HTTP {response.status} - {body}')
    
    data = json.loads(body)
    return data['data']


def execute_command(api_host, api_port, api_user, node, command, 
                    pve_auth_cookie, term_session, timeout, validate_certs,
                    vmid=None, vmtype='qemu', vm_user=None, vm_password=None):
    """
    Connect to Proxmox WebSocket and execute command.
    If vm_user/vm_password provided, handles login prompt first.
    Returns the command output.
    """
    # Build WebSocket URL
    vncticket = term_session['ticket']
    port = term_session['port']
    
    # Build path based on target type
    if vmid is not None:
        ws_path = f'/api2/json/nodes/{node}/{vmtype}/{vmid}/vncwebsocket'
    else:
        ws_path = f'/api2/json/nodes/{node}/vncwebsocket'
    
    ws_url = (
        f'wss://{api_host}:{api_port}{ws_path}'
        f'?port={port}&vncticket={urlencode({"t": vncticket})[2:]}'
    )
    
    # SSL options
    sslopt = {}
    if not validate_certs:
        sslopt = {
            'cert_reqs': ssl.CERT_NONE,
            'check_hostname': False
        }
    
    # Connect
    ws = websocket.WebSocket(sslopt=sslopt)
    ws.settimeout(timeout)
    ws.connect(
        ws_url,
        header=[f'Cookie: PVEAuthCookie={pve_auth_cookie}'],
        sslopt=sslopt
    )
    
    output_buffer = []
    command_sent = False
    logged_in = vm_user is None  # If no vm_user, assume already logged in (node shell)
    login_stage = 'waiting'  # waiting -> username_sent -> password_sent -> done
    
    def has_prompt(text):
        """Check if text contains a shell prompt."""
        return bool(re.search(r'[$#]\s*$', text, re.MULTILINE))
    
    def has_login_prompt(text):
        """Check if text contains a login prompt."""
        return bool(re.search(r'login:\s*$', text, re.IGNORECASE))
    
    def has_password_prompt(text):
        """Check if text contains a password prompt."""
        return bool(re.search(r'password:\s*$', text, re.IGNORECASE))
    
    try:
        # Send authentication: USER:VNCTICKET\n
        auth_msg = f'{api_user}:{vncticket}\n'
        ws.send(auth_msg)
        
        # Wait for OK response
        while True:
            msg = ws.recv()
            if isinstance(msg, bytes):
                msg = msg.decode('utf-8', errors='replace')
            if msg == 'OK':
                break
        
        # Process terminal output
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                ws.settimeout(2.0)
                msg = ws.recv()
                if not msg:
                    continue
                    
                if isinstance(msg, bytes):
                    msg = msg.decode('utf-8', errors='replace')
                
                output_buffer.append(msg)
                
                # Handle VM login if credentials provided
                if not logged_in:
                    if login_stage == 'waiting':
                        if has_login_prompt(msg):
                            # Send username
                            user_data = vm_user + '\r'
                            byte_len = len(user_data.encode('utf-8'))
                            ws.send(f'0:{byte_len}:{user_data}')
                            login_stage = 'username_sent'
                            continue
                        elif has_prompt(msg):
                            # Already logged in, skip login flow
                            logged_in = True
                            login_stage = 'done'
                            # Fall through to send command
                    elif login_stage == 'username_sent' and has_password_prompt(msg):
                        # Send password
                        pass_data = vm_password + '\r'
                        byte_len = len(pass_data.encode('utf-8'))
                        ws.send(f'0:{byte_len}:{pass_data}')
                        login_stage = 'password_sent'
                        continue
                    elif login_stage == 'password_sent' and has_prompt(msg):
                        # Login successful, now logged in
                        logged_in = True
                        login_stage = 'done'
                        # Fall through to send command
                
                # Send command after prompt appears (and logged in)
                if logged_in and not command_sent and has_prompt(msg):
                    cmd_data = command + '\r'
                    byte_len = len(cmd_data.encode('utf-8'))
                    ws.send(f'0:{byte_len}:{cmd_data}')
                    command_sent = True
                    continue
                
                # After sending command, wait for prompt to return (command done)
                if command_sent and has_prompt(msg):
                    # Drain any remaining output
                    time.sleep(0.2)
                    try:
                        ws.settimeout(0.3)
                        while True:
                            extra = ws.recv()
                            if extra:
                                if isinstance(extra, bytes):
                                    extra = extra.decode('utf-8', errors='replace')
                                output_buffer.append(extra)
                            else:
                                break
                    except:
                        pass
                    break
                    
            except websocket.WebSocketTimeoutException:
                # On timeout, send Enter to wake up the terminal if waiting for login
                if not logged_in and login_stage == 'waiting':
                    ws.send('0:1:\r')
                elif command_sent:
                    break
                continue
            except Exception:
                break
            
    finally:
        ws.close()
    
    return ''.join(output_buffer)


def parse_command_output(raw_output, command):
    """
    Parse raw terminal output to extract just the command result.
    Removes login banner, command echo, and trailing prompt.
    """
    # Strip ANSI escape codes first for easier parsing
    clean_output = strip_ansi(raw_output)
    
    # Remove carriage returns
    clean_output = clean_output.replace('\r', '')
    
    lines = clean_output.split('\n')
    result_lines = []
    found_command = False
    
    for line in lines:
        stripped = line.strip()
        
        # Look for the echoed command (it appears after a prompt)
        if not found_command:
            # Check if this line ends with or contains our command
            if command in stripped:
                found_command = True
                continue
            continue
        
        # Once we found command, collect output until we hit another prompt
        if found_command:
            # Skip empty lines at start of output
            if not result_lines and not stripped:
                continue
            # Stop at prompt (e.g., "root@pm:~#" or "user@host:~$")
            if re.match(r'^[a-zA-Z0-9_-]+@[a-zA-Z0-9_-]+[:\s~]*[$#]\s*$', stripped):
                break
            result_lines.append(line.rstrip())
    
    result = '\n'.join(result_lines).strip()
    return result


def run_module():
    module_args = dict(
        api_host=dict(type='str', default='pve.lan'),
        api_port=dict(type='int', default=8006),
        api_user=dict(type='str', default='root@pam'),
        node=dict(type='str', default='pve'),
        vmid=dict(type='int', required=False, default=None),
        vmtype=dict(type='str', default='qemu', choices=['qemu', 'lxc']),
        vm_user=dict(type='str', required=False, default=None),
        vm_password=dict(type='str', required=False, default=None, no_log=True),
        command=dict(type='str', required=True),
        pve_auth_cookie=dict(type='str', required=True, no_log=True),
        proxmox_csrf_token=dict(type='str', required=True, no_log=True),
        timeout=dict(type='int', default=30),
        validate_certs=dict(type='bool', default=False),
    )

    result = dict(
        changed=True,
        stdout='',
        stdout_raw=''
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False
    )

    if not HAS_WEBSOCKET:
        module.fail_json(
            msg=f'websocket-client Python package required: pip install websocket-client. Error: {WEBSOCKET_IMPORT_ERROR}'
        )

    try:
        # Create terminal session
        term_session = create_term_session(
            api_host=module.params['api_host'],
            api_port=module.params['api_port'],
            node=module.params['node'],
            pve_auth_cookie=module.params['pve_auth_cookie'],
            pve_csrf_token=module.params['proxmox_csrf_token'],
            validate_certs=module.params['validate_certs'],
            vmid=module.params['vmid'],
            vmtype=module.params['vmtype']
        )
        
        # Execute command via WebSocket
        raw_output = execute_command(
            api_host=module.params['api_host'],
            api_port=module.params['api_port'],
            api_user=module.params['api_user'],
            node=module.params['node'],
            command=module.params['command'],
            pve_auth_cookie=module.params['pve_auth_cookie'],
            term_session=term_session,
            timeout=module.params['timeout'],
            validate_certs=module.params['validate_certs'],
            vmid=module.params['vmid'],
            vmtype=module.params['vmtype'],
            vm_user=module.params['vm_user'],
            vm_password=module.params['vm_password']
        )
        
        # Parse output
        stdout = parse_command_output(raw_output, module.params['command'])
        
        result['stdout'] = stdout
        result['stdout_raw'] = raw_output
        
        module.exit_json(**result)
        
    except Exception as e:
        module.fail_json(msg=str(e), **result)


def main():
    run_module()


if __name__ == '__main__':
    main()
