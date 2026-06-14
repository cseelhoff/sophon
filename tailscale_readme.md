# Tailscale in the Devcontainer

The devcontainer includes Tailscale from the Nix profile. Because `sudo` does not
include `/home/vscode/.nix-profile/bin` in its secure path, run Tailscale commands
with the full binary path or define the variables below first.

The Tailscale state is stored in `.tailscale/tailscaled.state`. That directory is
ignored by Git so the node key can persist between VS Code devcontainer sessions
without being committed.

## Start Tailscale

Run these commands after the devcontainer starts:

```bash
TS_BIN=/home/vscode/.nix-profile/bin
TS_DIR=/workspaces/sophon/.tailscale
TS_SOCKET="$TS_DIR/tailscaled.sock"
TS_STATE="$TS_DIR/tailscaled.state"

sudo install -d -m 700 "$TS_DIR"

sudo "$TS_BIN/tailscaled" \
  --socket="$TS_SOCKET" \
  --state="$TS_STATE" &
```

Then bring the node online:

```bash
sudo "$TS_BIN/tailscale" \
  --socket="$TS_SOCKET" \
  up \
  --accept-routes \
  --shields-up
```

If the persisted state is valid, Tailscale should reconnect without a browser
login. If the node key expired or was revoked, Tailscale will print a new auth
URL.

For Headscale or another non-default control server, add `--login-server`:

```bash
sudo "$TS_BIN/tailscale" \
  --socket="$TS_SOCKET" \
  up \
  --login-server=https://your-headscale.example.com \
  --accept-routes \
  --shields-up
```

## Check Status and Routes

```bash
sudo "$TS_BIN/tailscale" --socket="$TS_SOCKET" status
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

Approve the advertised routes in the Tailscale admin console. The devcontainer
already uses `--accept-routes`, but the routes will not appear until they are
approved.

Confirm the Pi can reach the target networks locally:

```bash
ip route get 10.0.20.11
ip route get 10.0.60.2
```

## Notes

- `/dev/net/tun` must exist in the devcontainer.
- The devcontainer needs `NET_ADMIN`, `NET_RAW`, and `/dev/net/tun` in
  `.devcontainer/devcontainer.json`.
- `.tailscale/` contains a Tailscale node key. Keep it ignored and treat it like
  a secret.
