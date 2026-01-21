#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2026
# GNU General Public License v3.0+

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: proxmox_shell

short_description: Execute commands on Proxmox VE node shell via termproxy WebSocket

version_added: "1.0.0"

description:
    - Execute shell commands on a Proxmox VE node using the termproxy/vncwebsocket API.
    - Requires pre-authenticated PVE ticket and CSRF token (obtain via /api2/json/access/ticket).

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
    command:
        description: Shell command to execute
        required: true
        type: str
    pve_auth_cookie:
        description: PVE authentication ticket/cookie (from /api2/json/access/ticket)
        required: true
        type: str
        no_log: true
    pve_csrf_token:
        description: PVE CSRF prevention token (from /api2/json/access/ticket)
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

# Then use this module:
- name: Run uptime on Proxmox node
  proxmox_shell:
    command: uptime
    pve_auth_cookie: "{{ pve_ticket.json.data.ticket }}"
    pve_csrf_token: "{{ pve_ticket.json.data.CSRFPreventionToken }}"
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
    # - OSC sequences: \x1b]...\x07
    # - Character set: \x1b(X or \x1b)X
    # - Private modes: \x1b[?...h or \x1b[?...l
    ansi_pattern = re.compile(
        r'\x1b\[\?[0-9;]*[hl]'   # Private mode set/reset (e.g., \x1b[?2004h)
        r'|\x1b\[[0-9;]*[a-zA-Z]'  # Standard CSI sequences
        r'|\x1b\][^\x07]*\x07'     # OSC sequences
        r'|\x1b[()][AB012]'        # Character set selection
        r'|\x1b[=>]'               # Keypad modes
    )
    return ansi_pattern.sub('', text)


def create_term_session(api_host, api_port, node, pve_auth_cookie, pve_csrf_token, validate_certs):
    """
    Create a terminal proxy session via POST /api2/json/nodes/{node}/termproxy
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
                    pve_auth_cookie, term_session, timeout, validate_certs):
    """
    Connect to Proxmox WebSocket and execute command.
    Returns the command output.
    """
    # Build WebSocket URL
    vncticket = term_session['ticket']
    port = term_session['port']
    
    ws_url = (
        f'wss://{api_host}:{api_port}/api2/json/nodes/{node}/vncwebsocket'
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
    
    def has_prompt(text):
        """Check if text contains a shell prompt."""
        return bool(re.search(r'[$#]\s*$', text, re.MULTILINE))
    
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
                
                # Send command after first prompt appears
                if not command_sent and has_prompt(msg):
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
                if command_sent:
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
        command=dict(type='str', required=True),
        pve_auth_cookie=dict(type='str', required=True, no_log=True),
        pve_csrf_token=dict(type='str', required=True, no_log=True),
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
            pve_csrf_token=module.params['pve_csrf_token'],
            validate_certs=module.params['validate_certs']
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
            validate_certs=module.params['validate_certs']
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
