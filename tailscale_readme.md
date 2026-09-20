# Tailscale for the Deployment Controller

The deployment web server and Ansible need both routed access to the site and
DNS resolution through the site's CoreDNS. Tailscale does not have to run in
the same environment as Ansible: Windows can provide connectivity and DNS to
WSL. Starting Tailscale only inside a devcontainer, however, does not install
routes on the host.

## Recommended: Windows -> WSL -> Sophon

For this setup, run **the Tailscale client on Windows only**, and run Sophon
from `nix develop` in WSL. Prefer this over running another Tailscale daemon
inside WSL or a devcontainer under WSL.

Windows handles the VPN and split DNS; WSL uses Windows networking and DNS
tunneling. This avoids competing DNS managers: WSL can overwrite resolver
settings written by a Linux Tailscale client, producing a DNS health warning
and failed service-name lookups even when subnet routes work.

### Connect Windows

Install the Windows Tailscale client and connect to the **`cpt_177@outlook.com`**
tailnet, the same account used by the Raspberry Pi router (`cloudflared`).
In PowerShell, elevated if required:

```powershell
tailscale up
tailscale set --accept-routes=true --accept-dns=true --shields-up=true
tailscale status
```

- `--accept-routes=true` uses the Pi's advertised subnet routes. Windows
  already enables this by default; setting it explicitly is fine.
- `--accept-dns=true` applies the tailnet's DNS settings, including split DNS.
- `--shields-up=true` blocks unsolicited inbound Tailscale connections to
  Windows, but permits outbound deployment connections and their replies.
  Use `--shields-up=false` if you need inbound access such as RDP over Tailscale.

These preferences persist. `tailscale set` changes only the named preferences,
without requiring you to repeat other non-default settings. Check that status
shows owner `cpt_177@` and the peer `cloudflared` (`100.116.48.64`); a successful
login to a different tailnet will not provide the required access.

### Configure Split DNS

In the [Tailscale admin DNS page](https://login.tailscale.com/admin/dns) for
this tailnet, add a **restricted nameserver**:

| Setting | Value |
|---|---|
| Nameserver | `10.0.60.3` (CoreDNS on InfraVM) |
| Restrict to domain | `177cpt.com` |

This is a tailnet-wide DNS setting. It directs queries for the service domain
to CoreDNS without replacing DNS for unrelated internet domains. Adding
`177cpt.com` only as a search domain is not sufficient, and MagicDNS alone does
not resolve Sophon's service names.

CoreDNS must be running, and Windows must be able to reach it over the approved
`10.0.60.0/24` subnet route. On a first deployment, CoreDNS is deployed before
the controller DNS preflight; configure DNS and retry if that preflight stops
the run.

### Keep WSL DNS Tunneling Enabled

Keep WSL DNS tunneling enabled (`dnsTunneling=true` in the `[wsl2]` section of
the Windows user's `.wslconfig`) and keep `generateResolvConf=true` in the
`[network]` section of WSL's `/etc/wsl.conf`. On NixOS, manage the latter
through the NixOS configuration rather than editing a generated file.

Do not manually replace WSL's generated resolver file with a CoreDNS address.
With DNS tunneling, WSL delegates lookups to Windows, including its VPN DNS
policies. The generated nameserver is commonly `10.255.255.254`; it need not
show the CoreDNS address directly.

Changes to WSL startup settings may require `wsl --shutdown` from PowerShell.
That stops WSL processes, including the dashboard and any deployment: only do
this when no deployment is running, then reopen WSL and restart the dashboard.

See [Microsoft's DNS tunneling guidance](https://learn.microsoft.com/en-us/windows/wsl/troubleshooting#networking-considerations-with-dns-tunneling)
and [Tailscale's DNS documentation](https://tailscale.com/kb/1054/dns).

### Stop Duplicate Clients and Verify from WSL

If you previously started the native WSL service described below, stop it:

```bash
sudo systemctl stop sophon-tailscaled.service
```

Also stop any other WSL or devcontainer Tailscale daemon you started. Do not
delete saved node state or Kopia keys. An offline `sophon-devcontainer` entry
in Tailscale status is expected once its daemon is stopped.

Check DNS in Windows PowerShell:

```powershell
Resolve-DnsName traefik.177cpt.com
```

Then check from the WSL terminal that will run Sophon:

```bash
getent ahostsv4 traefik.177cpt.com
timeout 5 bash -lc '</dev/tcp/10.0.20.11/8006' && echo 'Proxmox API reachable'
timeout 5 bash -lc '</dev/tcp/10.0.60.2/2049' && echo 'NFS reachable'
timeout 5 bash -lc '</dev/tcp/10.0.60.3/9443' && echo 'Portainer reachable'
```

Both DNS checks must return `10.0.60.3`, and all three TCP checks must succeed
with the WSL Tailscale daemon stopped. A direct `dig @10.0.60.3` query alone
does not prove that Ansible's normal resolver works.

WSL does not need a `tailscale0` interface or routing table 52 in this setup;
traffic can leave through its normal Windows-facing interface. If deploying
from a devcontainer under WSL, repeat DNS and TCP checks inside that container:
container networking and DNS do not necessarily inherit WSL's behavior.

Once these checks pass, run `nix develop` and start the deployment dashboard in
WSL as described in the [README](README.md#deployment-diagram). No WSL or
devcontainer Tailscale client is needed for the verified Windows-hosted setup.

## Alternative: Native Linux Host with `nix develop`

On a Linux host with systemd, enter `nix develop` from the repository root.
The shell includes `tailscale` and `tailscaled`; the `sophon-tailscale` helper
is only installed in the devcontainer.

Stop any container daemon before reusing its `.tailscale/` state on the host.
Never run two daemons against the same state file. When migrating existing
container state, make the directory and state file owned by host root, retaining
directory mode `0700` and state-file mode `0600`. Do not delete the state to
resolve an ownership error.

Start the host daemon once per boot:

```bash
sudo install -d -m 700 "$PWD/.tailscale"
sudo systemd-run --unit=sophon-tailscaled --description='Sophon host Tailscale' \
  "$(command -v tailscaled)" \
  --socket="$PWD/.tailscale/tailscaled.sock" \
  --state="$PWD/.tailscale/tailscaled.state"
```

This runtime service survives terminal exit, but is not enabled at boot.
If it is already active, skip starting it again.

```bash
sudo "$(command -v tailscale)" --socket="$PWD/.tailscale/tailscaled.sock" up \
  --accept-routes --shields-up --hostname=sophon-devcontainer
sudo "$(command -v tailscale)" --socket="$PWD/.tailscale/tailscaled.sock" status
systemctl is-active sophon-tailscaled.service
```

The hostname above preserves the existing node's name; it does not mean the
daemon runs in a container. If login is requested, use **`cpt_177@outlook.com`**,
as described below. For other CLI commands in this guide, replace
`sophon-tailscale` with
`sudo "$(command -v tailscale)" --socket="$PWD/.tailscale/tailscaled.sock"`.
The route and TCP checks below should pass on the host before deploying.

When moving deployment execution from a rootless container to the host, also
check ownership of `artifacts/deployment/` and `artifacts/kopia/`, including
their files. They must be accessible to the host user running Ansible.
Preserve existing Kopia keys and keep private directories at `0700` and
private keys/logs at `0600`; do not regenerate keys or make secrets world-readable.

## Alternative: Tailscale Inside the Devcontainer

The devcontainer includes Tailscale from the Nix profile and installs a
`sophon-tailscale` helper into `/usr/local/bin`. Rebuild the devcontainer after
changes to `.devcontainer/` so the helper is available.

In VS Code, run **Dev Containers: Rebuild and Reopen in Container** from the
command palette (or **Dev Containers: Rebuild Container** if already inside).
Wait for the image build to finish, then run the commands below in the container
terminal, not the host terminal.

The Tailscale state is stored in `.tailscale/tailscaled.state`. That directory is
ignored by Git so the node key can persist between VS Code devcontainer sessions
without being committed.

### Start Tailscale

Run this command after the devcontainer starts:

```bash
sophon-tailscale up
```

The helper creates `.tailscale/`, starts `tailscaled` with the repo-local socket
and state file, then runs `tailscale up --accept-routes --shields-up`.

If you need to pass additional `tailscale up` flags, append them:

```bash
sophon-tailscale up --hostname=sophon-devcontainer
```

If the persisted state is valid, Tailscale should reconnect without a browser
login. If the node key expired or was revoked, Tailscale will print a new auth
URL.

### Use the Project's Tailscale Account

Sign in as **`cpt_177@outlook.com`** and select that account's tailnet when
authorizing the devcontainer. This is the account used by the Raspberry Pi
subnet router (`cloudflared`). Do not use `177cpt@proton.me` or a different
personal account: a successful login to another tailnet does not provide
access to this router.

Open the authentication URL in a private/incognito browser window if your
browser is already signed into another Tailscale account. Use the same sign-in
provider used for the Pi, and verify the account before approving the device.
The installed Tailscale CLI (1.92.3) has no email/username-hint option for
`up` or `login`; `--login-server` selects a control server, not an account.

After authorization, check:

```bash
sophon-tailscale status
```

The devcontainer should show the owner `cpt_177@` and list `cloudflared`
(`100.116.48.64`) as a peer. If it shows another owner or an unexpected set of
peers, reauthenticate from a local devcontainer terminal:

```bash
sophon-tailscale up --force-reauth
```

This disconnects the current Tailscale session. Do not run it from an SSH or
RDP session that depends on that connection. Open the new URL and authorize
with `cpt_177@outlook.com`. A normal `sophon-tailscale up` reuses saved state
and will not correct a previously selected account.

### Custom Control Server

For Headscale or another non-default control server, add `--login-server`:

```bash
sophon-tailscale up --login-server=https://your-headscale.example.com
```

## Check Status and Routes

These interface and route-table checks apply when Tailscale runs directly on
the Linux controller or inside its devcontainer, not when Windows provides
Tailscale connectivity to WSL.

```bash
sophon-tailscale status
ip route show table 52
```

Expected subnet routes for this project:

```text
10.0.20.0/24 dev tailscale0
10.0.60.0/24 dev tailscale0
```

Route lookup checks:

```bash
ip route get 10.0.20.11
ip route get 10.0.60.2
ip route get 10.0.60.3
```

TCP checks that match the playbook workflow:

```bash
timeout 5 bash -lc '</dev/tcp/10.0.20.11/8006' && echo 'Proxmox API reachable'
timeout 5 bash -lc '</dev/tcp/10.0.60.2/2049' && echo 'NFS reachable'
timeout 5 bash -lc '</dev/tcp/10.0.60.3/9443' && echo 'Portainer reachable'
```

## Remote Subnet Router

The remote Tailscale server, currently a Raspberry Pi 5 running Debian, must
advertise and forward the project subnets.

Enable forwarding on the Pi:

```bash
sudo tee /etc/sysctl.d/99-tailscale-subnet-router.conf >/dev/null <<'PI_EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
PI_EOF

sudo sysctl --system
```

Advertise the routes from the Pi:

```bash
sudo tailscale up --advertise-routes=10.0.20.0/24,10.0.60.0/24
```

Approve the advertised routes in the Tailscale admin console. Clients must
accept subnet routes (the Windows default and enabled by the devcontainer
helper), but client settings cannot substitute for route approval.

Confirm the Pi can reach the target networks locally:

```bash
ip route get 10.0.20.11
ip route get 10.0.60.2
```

## Notes

- The Dockerfile writes the Nix settings `sandbox = false` and
  `filter-syscalls = false` before running the Nix installer. The installer
  itself needs these settings inside the restricted build container; writing
  them afterward can fail with `cannot set host name: Operation not permitted`.
- `/dev/net/tun` must exist in the devcontainer.
- The devcontainer needs `NET_ADMIN`, `NET_RAW`, and `/dev/net/tun` in
  `.devcontainer/devcontainer.json`.
- `.tailscale/` contains a Tailscale node key. Keep it ignored and treat it like
  a secret.
