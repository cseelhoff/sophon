# Sophon deployment instructions

This guide describes what the operator must provide and do for a fresh deployment
on **Proxmox** or **VMware vCenter**, including a destination with no internet
access. Replace the example addresses and `example.com` with your own values.

## Overview: where each phase runs

| Step | Where you work | What happens |
|---|---|---|
| 1. Prepare / Prestage | Bootstrap laptop with internet | Prepare the stock CoreOS ISO and Sophon deployment ISO, including the offline runner and container images |
| 2. Provision | Bootstrap laptop with hypervisor management access | Create InfraVM with a new blank disk, attach both ISOs, and start it |
| 3. Install | InfraVM's hypervisor console | Install CoreOS and artifacts to disk; enter static networking and the `core` console password |
| 4. Configure | Installed InfraVM's console | Enter the domain, optional certificate payload, and service credentials; deploy services through the local controller container |

Steps 3-4 are the same for both hypervisors. There is no separate
controller VM, early NFS server, SSH key, guest SSH connection, or disk-image
conversion. NFS/Kopia backup infrastructure is outside this walkthrough.

The two reusable installation files are:

```text
artifacts/coreos/fedora-coreos-live.iso
artifacts/bootstrap/sophon-deployment.iso
```

They do not contain site passwords, private keys, the domain, or guest networking.
An offline site's certificates travel as a separate base64 payload, pasted in
Step 4. They are never baked into either ISO.

## Before you start

- A bootstrap laptop with this repository, Nix, and enough free space for the
  image archives, offline runner, build inputs, and both ISOs.
- Internet during initial preparation, or an already complete matching prestage
  cache and locally available Nix development environment.
- Management-network access from that laptop to the chosen hypervisor.
- Permission to create the VM and upload media, plus access to its graphical
  console. The laptop does not need a direct connection to the guest for installation.
- Capacity for the VM. The defaults are 8 CPU cores, 8192 MiB RAM, and a 100 GiB
  disk; increase these as appropriate. The installer checks actual space requirements.
- A password manager for the independent console and service credentials.

### Provisioning inputs: Proxmox

Choose **Proxmox** in the bootstrap dashboard and provide:

| Input | What the user must know |
|---|---|
| Host and API port | Reachable Proxmox hostname/IP; port defaults to `8006` |
| Management password | Password for the default `root@pam` account |
| Node | Actual Proxmox node name, not necessarily its address or the default `proxmox` |
| ISO storage | Storage enabled for ISO images, commonly `local` |
| VM disk storage | Storage for the new VM disk, commonly `local-lvm` |
| Bridge | Guest-network bridge, commonly `vmbr0` |
| VLAN | VLAN tag if needed; leave empty for an untagged network |
| VM settings | VM name, CPU cores, memory in MiB, and disk size in GiB |

The selected bridge/VLAN must carry the subnet you will enter at the live console.
Proxmox certificate verification defaults to disabled in the current configuration;
for production with a trusted certificate, set `proxmox_validate_certs=true`
through the Ansible configuration.

### Provisioning inputs: VMware vCenter

Choose **VMware vCenter** and provide:

| Input | What the user must know |
|---|---|
| HTTPS URL | For example, `https://vcenter.example.com`; standalone ESXi is not supported |
| Username and password | Account permitted to create/configure VMs, use the network, and upload to the datastore |
| Certificate trust | A trusted chain, or the path to an independently verified CA PEM on the bootstrap laptop |
| Datacenter and cluster | Existing inventory names |
| Shared datastore | Datastore accessible to the selected cluster's hosts, with room for media and the new thin disk |
| Port group | Existing standard/distributed port group already configured for the intended VLAN |
| VM folder | Optional existing absolute inventory folder; empty uses the datacenter's VM root |
| VM settings | VM name, CPU cores, memory in MiB, and disk size in GiB |

The laptop must reach both vCenter and the datastore upload endpoints it selects.
`nix develop` supplies `govc`; VMware guest tools are not required. HTTPS
verification is always enabled. Do not trust an entire downloaded CA bundle
without independently verifying the CA that signs your vCenter certificate.
Windows/browser trust and the laptop's CLI trust are separate: use the CA PEM
setting if the CLI does not already trust that CA.

**For either provider:** the domain, guest IP, console password, and service
passwords are not provisioning inputs. Conflicting unmarked VMs are preserved and
rejected. Compatible, completed Sophon VMs are only started on a rerun; their
disks are not replaced. Use a fresh deployment rather than expecting provisioning
to erase or repair an existing VM.

## 1. Prepare installation media

While connected, enter `nix develop` in this repository and run
`ansible-playbook prestage.yml` to prepare the complete reusable media and cache.
The bootstrap command below also performs preparation automatically.
See the [prestage runbook](prestage.md) for disconnected laptop requirements.

No domain or private certificate state is needed to build the ISOs. If the
destination is offline, obtain its separate certificate payload while you still
have internet, using the command in the offline section below.

## 2. Provision the VM

From the repository on the bootstrap laptop:

```bash
nix develop
python3 deployment.py --phase bootstrap --serve --port 41327
```

1. Open the printed loopback URL, normally `http://127.0.0.1:41327/`.
2. Choose **Configure**, select the provider, and enter its inputs above.
3. Save the configuration and start the bootstrap run.
4. Let any missing preparation, media upload, and VM creation finish successfully.
5. Open the new VM's hypervisor console; it should boot the stock CoreOS live ISO.

If VS Code is remote or running in WSL and its browser cannot reach the printed
URL, forward port `41327` using VS Code's **Ports** view and open the forwarded
address. This is a laptop dashboard access issue, not a need for guest SSH.

To prepare media without provisioning anything, run `ansible-playbook prestage.yml`
inside `nix develop`. See the [prestage runbook](prestage.md) for cache handling,
or the [README](README.md#vcenter-phase-1) for terminal-based vCenter configuration.

## 3. Install CoreOS from the console

Have the following ready for **either** hypervisor:

| Input | Example / requirement |
|---|---|
| Installation disk | Fresh whole disk; inspect it rather than assuming its name |
| Static guest IPv4 | For example, `192.168.50.10`, unused and outside the DHCP pool |
| Prefix length | For example, `24` |
| Gateway | For example, `192.168.50.1`, reachable on the chosen bridge/VLAN or port group |
| Upstream DNS | A reachable DNS server; do not point it at InfraVM itself as its upstream |
| Console password | A strong new password for the fixed local username `core`; enter it twice |

In the live console:

```bash
lsblk
sudo mkdir -p /mnt/sophon
sudo mount -o ro /dev/disk/by-label/SOPHON_DEPLOY /mnt/sophon
sudo bash /mnt/sophon/install.sh
```

1. Select the blank installation disk, usually `/dev/vda` on Proxmox or
   `/dev/sda` on VMware. Confirm the actual disk with `lsblk`.
2. Enter the networking and console-password inputs.
3. Confirm the exact `INSTALL <disk>` phrase shown by the installer.
4. Wait for verification, installation, artifact copying, and automatic reboot.
   Whole-disk blank checks can take many minutes on a large or slow disk.
5. Log in to the installed system as `core` using the console password.
6. Wait for host setup to finish loading the offline runner and starting Portainer.
   The first load can also take several minutes.
7. Detach the two installation ISOs after confirming the installed system has booted.
   Subsequent operation uses the disk, not either ISO.

The installer rejects nonblank or mounted target disks. Do not work around that
safety check by wiping an unidentified disk.

**Offline target:** keep your separately generated certificate payload ready.
Paste it into the optional certificate field in Step 4; no extra CD is required.

## 4. Configure services

### Site settings the user must provide

| Input | Requirement |
|---|---|
| InfraVM IP and upstream DNS | Confirm the defaults carried from installation |
| Domain | Publicly registered domain managed by Cloudflare, such as `example.com`; not a private `.local` name |
| ACME email | Real contact address for certificate issuance |
| Cloudflare API token | For an internet-connected destination; **leave empty on an offline destination with imported certificates** |
| Pre-issued certificates | Optional base64 `acme.json` from the connected command below; required for a new offline deployment |
| Tunnel name | Defaults to `sophon`; relevant if using an existing Cloudflare tunnel |
| Keycloak realm and display name | Realm defaults to `sophon`; display name is optional |

For connected issuance, scope the Cloudflare token to the intended zone with
**Zone / Zone / Read** and **Zone / DNS / Edit**. Add
**Account / Cloudflare Tunnel / Edit** if using tunnels. DNS-01 validation
does not require exposing the VM to inbound public HTTP.

### Credentials and license the user must provide

Prepare these values in your password manager before starting the wizard:

| Value | Purpose |
|---|---|
| Portainer admin password | Local administrator; at least 12 characters |
| Portainer Business Edition license | Needed for the standard licensed deployment, including LDAP-group-based administrator mapping |
| Keycloak admin password | Keycloak administration |
| Keycloak database password | Persistent Keycloak database |
| Portainer OIDC client secret | Shared secret between Keycloak and Portainer |
| Gitea admin password | Gitea's local administrator |
| LDAP admin password | Directory administration and configured binds |
| LDAP seed-user password | Seeded `john.doe` account |
| LDAP seed-admin password | Seeded `john.doe.admin` account |
| Gitea database password | Optional separate value; otherwise reuses the supplied Portainer admin password |

Use distinct strong passwords for a real site. Keep the `core` console password
separate from management and service credentials. No Proxmox/vCenter password
is needed in Step 4.

In the installed `core` console, run:

```bash
sophon-controller
```

Enter the settings, answer the hidden secret prompts, and confirm deployment.
The optional certificate prompt accepts the complete base64 payload, or
`@/workspace/path/to/payload.txt` for a file inside the mounted project. Its
terminal input supports long pastes without the usual canonical-line truncation.
Do not paste a JSON account key alone: the store must include all issued
certificates and their private keys.
The controller deploys DNS, Traefik, LDAP, Keycloak, and Gitea through Portainer.
Wait for a successful completion rather than stopping when a container first starts.

Inputs are not saved for reuse by the wizard. Keep the actual deployed values:
reruns need the **same existing credentials**, especially database passwords and
the OIDC client secret. Changing a wizard input is not a password-rotation procedure.
Protect deployment logs and service configuration; they may contain credentials.

If host setup fails or Portainer's uninitialized setup window expires:

```bash
journalctl -u sophon-host-bootstrap
sudo systemctl restart sophon-host-bootstrap
```

Then rerun the wizard; do not reinstall the VM. The restart preserves Portainer data.
An optional web interface is available with
`sophon-controller --dashboard --port 41327`; run it **instead of**, not alongside,
the console wizard. Its loopback browser connectivity must be arranged separately.

## Verify the result

1. Configure client DNS or split-DNS so `traefik`, `portainer`, `dns`, `ldap`,
   `auth`, and `git` under your domain resolve to InfraVM. The controller's
   internal name mappings do not change Windows or other clients' DNS.
2. Open `https://portainer.example.com`, `https://auth.example.com`, and
   `https://git.example.com`; certificates should be trusted without bypasses.
3. Check all five intended stacks in Portainer. Persistent containers should be
   running; initialization containers may correctly be exited with status `0`.
4. Sign in through Keycloak using the seeded LDAP accounts. Verify Gitea SSO and
   that Portainer gives `john.doe` standard access and `john.doe.admin`
   administrator access.
5. With both CDs empty, cleanly shut down and power on the VM. Confirm services,
   stored data, trusted certificates, and sign-in recover without reinstalling
   or rerunning deployment.

For example, from a client with `dig` and `curl`, using the default realm:

```bash
dig @192.168.50.10 auth.example.com A
curl --fail --show-error \
  --resolve auth.example.com:443:192.168.50.10 \
  https://auth.example.com/realms/sophon/.well-known/openid-configuration
```

The temporary `--resolve` mapping tests this endpoint without changing client
DNS and still verifies the HTTPS hostname and chain. After startup, allow time
for databases and Keycloak to become ready, not just for Portainer to answer.

## Offline destinations: two-step certificate handoff

These steps apply identically to Proxmox and vCenter. A destination without
internet cannot obtain or renew its own public certificates. Working certificates
are required for the LDAPS/SSO deployment chain, not just browser appearance.

### A. On a connected machine: run one certificate command

1. Complete `ansible-playbook prestage.yml` on a connected machine.
2. If the bootstrap laptop will also be disconnected, carry the matching
   repository and complete prestage cache, not just the two guest ISOs. Ensure
   `nix develop` already works offline; entering it must not require fresh downloads.
   Keep private identity state and logs out of the general cache transfer.
3. Have the public Cloudflare-managed domain, ACME contact email, and a token
   with Zone / Zone / Read and Zone / DNS / Edit for that zone.
4. On that connected machine, use the Sophon Python environment and Docker,
   Podman, or a native Traefik 3.x executable. Run:

```bash
nix develop
python3 certificates.py issue
```

The command prompts for the domain and, when issuance is needed, email and a
hidden Cloudflare token. It issues the six default service certificates through
DNS-01, reuses the ACME account, and keeps private per-domain state under
`~/.local/state/sophon/certificates/`. Valid certificates with more than 30 days
remaining are reused without another issuance or a token prompt.

Progress goes to stderr; stdout is **one base64 string** containing the complete
`acme.json`. The token is not in this payload. Copy the string privately. To save
it instead of displaying it in terminal scrollback:

```bash
umask 077
python3 certificates.py issue --domain example.com > acme-payload.txt
```

`--email`, `--engine docker|podman|traefik`, `--state-dir`, and `--timeout` are
available for automation. `CF_DNS_API_TOKEN` can supply the token securely through
the environment. Do not put literal tokens in command arguments.

### B. On InfraVM: paste the payload in Step 4

Run `sophon-controller` and paste into **Pre-issued certificates** when asked.
The optional dashboard offers a textarea for the same payload. Leave Cloudflare
token empty at the disconnected destination.

Sophon validates all six names, trusted chains, expiry, and matching keys before
atomically writing a mode-`0600` store. Account-only or malformed payloads are
rejected without replacing the prior file. The payload is not passed to Ansible
through environment variables or copied shell commands.

The first deployment seeds Traefik from this file. Its existing certificate-dumper
extracts the LDAP certificate/key automatically; you do not handle separate PEMs.
The Cloudflare token remains on the connected machine, and both ISOs stay reusable.
Base64 is not encryption: protect the payload, clipboard, saved file, and renewal
state as credentials. No certificate ISO is needed.

### Later renewals and timekeeping

- Provide a trustworthy local time source or correctly set guest UTC and its
  hardware clock. Check `date -u` and `timedatectl`, including after a cold boot.
  A wrong hypervisor/NTP clock can break certificates and OIDC. Do not disable
  TLS verification as a workaround or use an inaccurate NTP source.
- Schedule certificate refresh monthly. Let's Encrypt certificates normally
  last 90 days; do not wait for expiry or repeatedly reissue and exhaust rate limits.
- Keep the connected machine's protected ACME account/store for renewals.
- For each refresh, run the connected command again, then paste the new payload
  into the wizard launched with:

  ```bash
  sophon-controller --refresh-certificates
  ```

  This uses the existing full service deployment, so supply the same deployed
  service credentials. With those settings already exported and the new local
  store already imported, `sophon-controller --run --refresh-certificates` works too.
  Refresh is not supported through `--dashboard`.
- A normal rerun intentionally preserves Traefik's live certificate store;
  importing a newer file alone does not replace that store. The explicit refresh
  briefly stops Traefik, replaces the validated state, and starts it again.
- Recheck trusted HTTPS, LDAPS, and SSO after refresh. Retain the protected renewal
  source; retire unprotected clipboard/file copies of the exported payload.

See the [full sneakernet runbook](docs/runbooks/sneakernet-refresh.md) for
verification commands and expired-certificate recovery, and
[preconditions](docs/preconditions.md) for detailed environment-variable mappings.
