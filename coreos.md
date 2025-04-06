Okay, let's break down creating a Fedora CoreOS VM on Proxmox using the API.

**Can it be done *entirely* via the API?**

Almost, but **not quite 100%**. There are two main hurdles usually requiring shell access:

1.  **Getting the QCOW2 Image onto Proxmox Storage:** The Proxmox API doesn't have a direct function to download an arbitrary file (like the FCOS QCOW2 image) from the internet *directly* onto a Proxmox storage pool. You typically need to download it to the Proxmox host first using `wget` or `curl` via SSH, or upload it via the Web UI/SFTP. *However*, once downloaded to your *local* machine (where you run the API script), you *can* potentially use the API to upload it to storage, but this can be slow for large images and sometimes less reliable than placing it directly on the host.
2.  **Importing the QCOW2 as a VM Disk:** The most straightforward way to associate an existing QCOW2 file with a *new* VM is using the `qm importdisk` command via SSH. While the API can create disks, directly importing an arbitrary QCOW2 file *from the Proxmox host's filesystem* into a VM's configuration via a single API call is not directly supported in the way `qm importdisk` works. The API is better at creating *new* blank disks or cloning existing Proxmox volumes (like templates).

**Recommended Approach: Hybrid (API + Minimal Shell)**

The most practical and reliable method involves:

1.  **Shell:** Download the FCOS image to the Proxmox host.
2.  **API:** Create the VM configuration (CPU, RAM, Network, etc.).
3.  **API:** Upload the Ignition configuration file.
4.  **API:** Configure the VM to use the Ignition file.
5.  **Shell:** Use `qm importdisk` to import the downloaded QCOW2 as the VM's boot disk.
6.  **API:** Set the boot order and other final settings (like enabling UEFI).
7.  **API:** Start the VM.

**Detailed Step-by-Step Script (Hybrid Approach)**

This example uses Python with the `proxmoxer` library, which simplifies API interaction.

**Prerequisites:**

1.  **Install `proxmoxer`:**
    ```bash
    pip install proxmoxer
    ```
2.  **Proxmox API Token:** Create an API token in Proxmox (Datacenter -> Permissions -> API Tokens). Note the Token ID (e.g., `root@pam!mytoken`) and the Secret.
3.  **Fedora CoreOS Image:** Download the desired FCOS QCOW2 image (e.g., stable, testing) **to your Proxmox host** via SSH. Place it somewhere accessible, like `/var/lib/vz/template/qcow/`.
    ```bash
    # Example (run on Proxmox host via SSH)
    # Find the latest stable URL from: https://getfedora.org/en/coreos/download?stream=stable
    FCOS_URL=\"https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/39.20240304.3.0/x86_64/fedora-coreos-39.20240304.3.0-qemu.x86_64.qcow2.xz\"
    IMAGE_XZ=$(basename \"$FCOS_URL\")
    IMAGE_QCOW2=\"${IMAGE_XZ%.xz}\"
    DEST_DIR=\"/var/lib/vz/template/qcow\" # Or your preferred storage location directory

    mkdir -p \"$DEST_DIR\"
    cd \"$DEST_DIR\"
    wget \"$FCOS_URL\"
    unxz \"$IMAGE_XZ\"
    # You now have the .qcow2 file in $DEST_DIR
    echo \"FCOS Image Path: $DEST_DIR/$IMAGE_QCOW2\"
    ```
    *Keep track of the full path to the `.qcow2` file on the Proxmox host.*
4.  **Ignition Configuration File:** Create an Ignition file (`config.ign`) on the machine where you will run the Python script. This file configures the OS on first boot (users, SSH keys, files, etc.). You typically generate this from a Butane config (`*.bu`) file.
    *Example `config.bu` (Butane):*
    ```yaml
    variant: fcos
    version: 1.5.0
    passwd:
      users:
        - name: core
          ssh_authorized_keys:
            - ssh-rsa AAAA... # Replace with your public SSH key
    # Add storage, networkd, systemd unit configurations as needed
    ```
    *Compile Butane to Ignition:* Use the `butane` tool (e.g., in a container):
    ```bash
    podman run --interactive --rm quay.io/coreos/butane:release --pretty --strict < config.bu > config.ign
    ```
    *Make sure `config.ign` is in the same directory as your Python script, or adjust the path in the script.*

**Python Script (`create_fcos_vm.py`):**

```python
#!/usr/bin/env python3

import sys
import os
from proxmoxer import ProxmoxAPI
import getpass
import time
import subprocess # To run the shell command (qm importdisk)

# --- Configuration ---
# Proxmox Connection Details
PROXMOX_HOST = 'your-proxmox-ip-or-hostname'
PROXMOX_USER = 'root@pam' # Or your user with API token, e.g., 'apiuser@pve!mytoken'
PROXMOX_TOKEN_ID = 'your_token_id' # e.g., mytoken - Required if using token auth
PROXMOX_TOKEN_SECRET = None # Set this to your token secret string
PROXMOX_PASSWORD = None # Set this ONLY if using password auth (less secure)

# VM Configuration
VM_NODE = 'pve' # Name of your Proxmox node
VM_ID = 9000 # Desired VM ID (or set to None to find next available)
VM_NAME = 'fedora-coreos-vm'
VM_MEMORY = 2048 # MB
VM_CORES = 2
VM_SOCKETS = 1
VM_BRIDGE = 'vmbr0' # Your network bridge
VM_STORAGE = 'local-lvm' # Target storage for the VM disk AND ignition snippet
VM_DISK_INTERFACE = 'virtio' # 'scsi' or 'virtio' - virtio recommended for FCOS
VM_DISK_SIZE_GB = 20 # Final desired size of the disk after import

# FCOS & Ignition Configuration
FCOS_IMAGE_PATH_ON_HOST = '/var/lib/vz/template/qcow/fedora-coreos-39.20240304.3.0-qemu.x86_64.qcow2' # **IMPORTANT**: Path on the Proxmox HOST
IGNITION_FILE_PATH_LOCAL = './config.ign' # Path to your ignition file on THIS machine

# SSH Details for running qm importdisk remotely
# Leave PROXMOX_SSH_USER empty to skip remote execution (you'll need to run qm importdisk manually)
PROXMOX_SSH_USER = 'root' # User to SSH into Proxmox host as (must have key auth setup or use password)
PROXMOX_SSH_HOST = PROXMOX_HOST # Hostname/IP for SSH (usually same as API host)
# If using SSH Key, ensure it's loaded in your agent or specify key path if needed via ssh command options
# --- End Configuration ---

def find_next_vmid(proxmox, start_id=100):
    \"\"\"Finds the next available VM ID.\"\"\"
    existing_vms = {vm['vmid'] for vm in proxmox.cluster.resources.get(type='vm')}
    current_id = start_id
    while current_id in existing_vms:
        current_id += 1
    return current_id

def run_remote_ssh_command(host, user, command):
    \"\"\"Runs a command on the Proxmox host via SSH.\"\"\"
    ssh_command = ['ssh', f'{user}@{host}', command]
    print(f\"[*] Running SSH command: {' '.join(ssh_command)}\")
    try:
        result = subprocess.run(ssh_command, check=True, capture_output=True, text=True)
        print(\"[+] SSH Command Output:\")
        print(result.stdout)
        if result.stderr:
            print(\"[!] SSH Command Error Output:\")
            print(result.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f\"[ERROR] SSH command failed: {e}\")
        print(f\"Stderr: {e.stderr}\")
        print(f\"Stdout: {e.stdout}\")
        return False
    except FileNotFoundError:
        print(\"[ERROR] 'ssh' command not found. Is it installed and in your PATH?\")
        return False


print(f\"--- Starting FCOS VM Creation ({VM_NAME}) ---\")

# 1. Connect to Proxmox API
try:
    if PROXMOX_TOKEN_SECRET:
        print(f\"[*] Connecting to https://{PROXMOX_HOST}:8006 with token...\")
        proxmox = ProxmoxAPI(
            PROXMOX_HOST,
            user=PROXMOX_TOKEN_ID,
            token=PROXMOX_TOKEN_SECRET,
            verify_ssl=False # Set to True if you have valid SSL certs
        )
    elif PROXMOX_PASSWORD:
         print(f\"[*] Connecting to https://{PROXMOX_HOST}:8006 with password...\")
         proxmox = ProxmoxAPI(
            PROXMOX_HOST,
            user=PROXMOX_USER,
            password=PROXMOX_PASSWORD,
            verify_ssl=False
        )
    else:
         # Try password prompt if nothing else set
         print(f\"[*] Connecting to https://{PROXMOX_HOST}:8006, requesting password...\")
         PROXMOX_PASSWORD = getpass.getpass(f\"Enter password for {PROXMOX_USER}: \")
         proxmox = ProxmoxAPI(
            PROXMOX_HOST,
            user=PROXMOX_USER,
            password=PROXMOX_PASSWORD,
            verify_ssl=False
         )

    # Test connection
    proxmox.version.get()
    print(\"[+] Connected to Proxmox API successfully.\")

except Exception as e:
    print(f\"[ERROR] Failed to connect to Proxmox API: {e}\")
    sys.exit(1)

# 2. Determine VM ID
if VM_ID is None:
    try:
        VM_ID = find_next_vmid(proxmox, start_id=9000) # Start search higher for safety
        print(f\"[*] No VM ID specified, found next available ID: {VM_ID}\")
    except Exception as e:
        print(f\"[ERROR] Could not find next available VM ID: {e}\")
        sys.exit(1)
else:
    # Basic check if VM ID already exists
    try:
        proxmox.nodes(VM_NODE).qemu(VM_ID).status.current.get()
        print(f\"[ERROR] VM ID {VM_ID} already exists on node {VM_NODE}.\")
        sys.exit(1)
    except Exception: # Expect an error if VM doesn't exist
        print(f\"[*] Using specified VM ID: {VM_ID}\")
        pass

# 3. Create Basic VM Configuration (without the main disk yet)
vm_config = {
    'vmid': VM_ID,
    'node': VM_NODE,
    'name': VM_NAME,
    'memory': VM_MEMORY,
    'cores': VM_CORES,
    'sockets': VM_SOCKETS,
    'net0': f'virtio,bridge={VM_BRIDGE}',
    'ostype': 'l26', # Linux 6.x kernel
    'scsihw': 'virtio-scsi-pci', # SCSI controller type
    'boot': 'order=scsi0', # Placeholder, will be updated after disk import
    'ide2': 'none,media=cdrom' # Optional: Placeholder CDROM
    # We will add the main disk later via importdisk
    # We will add UEFI/OVMF and Ignition config later
}
print(f\"[*] Creating VM {VM_ID} ({VM_NAME}) with basic configuration...\")
try:
    task_id = proxmox.nodes(VM_NODE).qemu.post(**vm_config)
    # Wait for task completion (optional but good practice)
    # proxmoxer doesn't have a built-in wait function, requires polling status
    print(f\"[+] VM creation task started: {task_id}. Waiting briefly...\")
    time.sleep(5) # Simple wait, proper polling is better for production
    print(f\"[+] Basic VM config for {VM_ID} created.\")
except Exception as e:
    print(f\"[ERROR] Failed to create VM configuration: {e}\")
    # Attempt cleanup if needed (optional)
    # try: proxmox.nodes(VM_NODE).qemu(VM_ID).delete()
    # except: pass
    sys.exit(1)

# 4. Upload Ignition Configuration as a Snippet
print(f\"[*] Uploading Ignition file '{IGNITION_FILE_PATH_LOCAL}' to storage '{VM_STORAGE}'...\")
try:
    with open(IGNITION_FILE_PATH_LOCAL, 'rb') as f: # Read as bytes
        # The filename on storage will be automatically generated by Proxmox
        # We need to capture the Volume ID (volid) Proxmox assigns.
        upload_result = proxmox.nodes(VM_NODE).storage(VM_STORAGE).upload.post(
            content='snippets',
            filename=f, # Pass the file handle
            # Proxmox handles naming, typically based on the upload filename
            # We might need to *find* the actual name later if not returned directly
        )
        print(f\"[+] Ignition file upload task started: {upload_result}. Waiting briefly...\")
        time.sleep(5) # Wait for upload task

        # --- Find the actual snippet volume ID ---
        # The upload API doesn't reliably return the final volid. We list snippets.
        ignition_volid = None
        time.sleep(2) # Give Proxmox a moment to list the new file
        contents = proxmox.nodes(VM_NODE).storage(VM_STORAGE).content.get(content='snippets')
        # Try to find based on original filename (Proxmox might sanitize it)
        base_ign_name = os.path.basename(IGNITION_FILE_PATH_LOCAL)
        for item in contents:
            if base_ign_name in item.get('volid', ''): # Proxmox usually includes original name
                ignition_volid = item['volid']
                break

        if not ignition_volid:
             print(f\"[WARNING] Could not automatically find uploaded snippet volid for {base_ign_name}.\")
             print(f\"          Please check storage '{VM_STORAGE}' content type 'snippets' in Proxmox UI.\")
             print(f\"          You will need to manually set the cicustom parameter later.\")
             # Attempt to guess based on common naming convention (less reliable)
             ignition_volid = f\"{VM_STORAGE}:snippets/{base_ign_name}\"
             print(f\"          Attempting to use guessed volid: {ignition_volid}\")
             # sys.exit(1) # Option to exit if not found
        else:
             print(f\"[+] Found uploaded Ignition snippet Volume ID: {ignition_volid}\")

except FileNotFoundError:
    print(f\"[ERROR] Ignition file not found at: {IGNITION_FILE_PATH_LOCAL}\")
    sys.exit(1)
except Exception as e:
    print(f\"[ERROR] Failed to upload Ignition file: {e}\")
    sys.exit(1)


# 5. Configure VM to use Ignition and UEFI/OVMF
if ignition_volid:
    print(f\"[*] Configuring VM {VM_ID} for Ignition and UEFI/OVMF...\")
    config_update = {
        'cicustom': f'vendor={ignition_volid}',
        'bios': 'ovmf', # Enable UEFI
        # Proxmox needs an EFI disk for OVMF. Add one if the storage supports it.
        # Typically needed on non-ZFS storage. 'local-lvm' usually needs it.
        'efidisk0': f'{VM_STORAGE}:1,format=raw,efitype=4m,pre-enrolled-keys=1' # Add EFI disk (1MB size is often enough, format raw)
         # Note: Size parameter for efidisk0 is implicit (1MB) or can be specified (e.g., size=4M)
         # Proxmox 7+ might auto-create this if storage is selected, but explicit is safer.
         # 'format=raw' might be needed depending on storage type. Check Proxmox docs for your storage.
         # If using ZFS, efidisk0 might just be 'STORAGE:1' without format/size.
    }
    try:
        proxmox.nodes(VM_NODE).qemu(VM_ID).config.put(**config_update)
        print(\"[+] Configured Ignition (cicustom) and UEFI/OVMF.\")
    except Exception as e:
        print(f\"[ERROR] Failed to set Ignition/UEFI config for VM {VM_ID}: {e}\")
        # Decide if you want to proceed without ignition/uefi or exit
        # sys.exit(1)
else:
    print(\"[WARNING] Skipping Ignition/UEFI configuration as snippet VolID was not found.\")


# 6. **SHELL COMMAND STEP**: Import the FCOS QCOW2 Disk
# This is the step usually requiring shell access on the Proxmox host.
print(f\"[*] Preparing to import disk '{FCOS_IMAGE_PATH_ON_HOST}' to VM {VM_ID} on storage '{VM_STORAGE}'...\")

# Choose the target disk name/interface (e.g., scsi0, virtio0)
target_disk_interface = f\"{VM_DISK_INTERFACE}0\" # e.g., virtio0 or scsi0

qm_command = f\"qm importdisk {VM_ID} {FCOS_IMAGE_PATH_ON_HOST} {VM_STORAGE} --format qcow2\"

if PROXMOX_SSH_USER:
    print(f\"[*] Attempting remote execution of: {qm_command}\")
    success = run_remote_ssh_command(PROXMOX_SSH_HOST, PROXMOX_SSH_USER, qm_command)
    if not success:
        print(\"[ERROR] Remote qm importdisk command failed. See errors above.\")
        print(\"        You may need to run the command manually on the Proxmox host:\")
        print(f\"        ssh {PROXMOX_SSH_USER}@{PROXMOX_SSH_HOST} \\\"{qm_command}\\\"\")
        print(\"        Then re-run the final steps of this script (attach/resize/boot order).\")
        sys.exit(1)
    print(\"[+] Remote qm importdisk command executed.\")
else:
    print(\"[!] SSH User not configured (PROXMOX_SSH_USER is empty).\")
    print(\"    You MUST run the following command manually on the Proxmox host:\")
    print(f\"    {qm_command}\")
    input(\"    Press Enter after you have successfully run the command on the host...\")

# 7. Attach the Imported Disk and Set Boot Order
print(f\"[*] Attaching imported disk and setting boot order for VM {VM_ID}...\")
# After import, Proxmox usually names the disk image file like 'vm-VMID-disk-N.qcow2'
# and adds an 'unused' disk entry to the VM config. We need to find this entry.
time.sleep(5) # Give Proxmox time to register the imported disk
try:
    vm_conf = proxmox.nodes(VM_NODE).qemu(VM_ID).config.get()
    unused_disk_key = None
    unused_disk_volid = None
    for key, value in vm_conf.items():
        if key.startswith('unused') and VM_STORAGE in value:
            unused_disk_key = key
            unused_disk_volid = value.split(',')[0] # Get the volume ID part (e.g., local-lvm:vm-100-disk-0)
            break

    if not unused_disk_key:
        print(f\"[ERROR] Could not find the imported disk (unused) in VM {VM_ID} config.\")
        print(f\"        Check the VM config in Proxmox UI. Was the import successful?\")
        sys.exit(1)

    print(f\"[+] Found imported disk: {unused_disk_key} ({unused_disk_volid})\")

    # Attach the unused disk to the desired interface (e.g., virtio0)
    # And set the boot order to prioritize this disk
    attach_config = {
        target_disk_interface: unused_disk_volid, # Attach the volume to the target interface
        'delete': unused_disk_key,         # Remove the 'unused' placeholder entry
        'boot': f'order={target_disk_interface}' # Set boot order
    }
    proxmox.nodes(VM_NODE).qemu(VM_ID).config.put(**attach_config)
    print(f\"[+] Attached disk as {target_disk_interface} and set boot order.\")

    # 8. (Optional but Recommended) Resize the Disk
    print(f\"[*] Resizing disk {target_disk_interface} to {VM_DISK_SIZE_GB}GB...\")
    # Note: FCOS will automatically expand its filesystem on first boot if the block device is larger
    try:
        proxmox.nodes(VM_NODE).qemu(VM_ID).resize.put(
            disk=target_disk_interface,
            size=f'+{VM_DISK_SIZE_GB}G' # Use '+' syntax to increase size relative to import
        )
        print(f\"[+] Disk resize task started for {target_disk_interface}. Waiting briefly...\")
        time.sleep(5) # Resize can take a moment
        print(\"[+] Disk resized.\")
    except Exception as e:
        print(f\"[ERROR] Failed to resize disk {target_disk_interface}: {e}\")
        # This might not be fatal, FCOS might still boot with original size

except Exception as e:
    print(f\"[ERROR] Failed to attach/configure imported disk for VM {VM_ID}: {e}\")
    sys.exit(1)

# 9. (Optional) Start the VM
start_vm = True # Set to False if you don't want to start it immediately
if start_vm:
    print(f\"[*] Starting VM {VM_ID}...\")
    try:
        task_id = proxmox.nodes(VM_NODE).qemu(VM_ID).status.start.post()
        print(f\"[+] VM start task initiated: {task_id}. Check Proxmox console.\")
        # Add task status polling here for robust check
    except Exception as e:
        print(f\"[ERROR] Failed to start VM {VM_ID}: {e}\")
else:
    print(f\"[*] VM {VM_ID} created but not started.\")

print(f\"--- FCOS VM Creation ({VM_NAME} / {VM_ID}) Complete ---\")
```

**How to Use:**

1.  **Configure:** Edit the configuration variables at the top of the `create_fcos_vm.py` script (Proxmox details, VM settings, paths). Pay close attention to `PROXMOX_HOST`, `PROXMOX_TOKEN_ID`/`SECRET`, `VM_NODE`, `VM_STORAGE`, `FCOS_IMAGE_PATH_ON_HOST`, and `IGNITION_FILE_PATH_LOCAL`. Also configure `PROXMOX_SSH_USER` and `PROXMOX_SSH_HOST` if you want the script to run `qm importdisk` remotely (requires passwordless SSH key auth setup between your machine and the Proxmox host for that user).
2.  **Run Prerequisites:** Make sure you've downloaded the FCOS image to the Proxmox host (Step 3 in Prerequisites) and created the `config.ign` file locally (Step 4).
3.  **Execute Script:**
    ```bash
    python ./create_fcos_vm.py
    ```
4.  **Monitor:** The script will output its progress. If SSH execution is enabled, it will run `qm importdisk`. If not, it will pause and ask you to run the command manually on the Proxmox host. Finally, it will attempt to start the VM. You can monitor the VM's console in the Proxmox Web UI to see the FCOS first boot process and Ignition applying the configuration.

This hybrid approach leverages the API for configuration and setup but relies on the standard, robust `qm importdisk` shell command for integrating the pre-downloaded QCOW2 image, which is generally the most reliable method for this specific task.