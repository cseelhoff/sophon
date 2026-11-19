#!/usr/bin/env bash
# Runs on the installed disk as core, never in the live ISO.
set -euo pipefail
export HOME=/var/home/core XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
unset CONTAINER_HOST CONTAINER_CONNECTION DOCKER_HOST

main() {
    [[ $(id -u) == 1000 && $(id -un) == core ]] || {
        echo "Host bootstrap must run as core." >&2; return 1;
    }
    local project=/var/lib/sophon/project infravm_ip dns_server dns_frontend_ids attempt
    local -a dns_frontends
    rm -f /run/user/1000/sophon-ready
    # The address arrives later, when sophon-deploy asks for the network; until then proxy needs no DNS server.
    infravm_ip=$(jq -er '.SOPHON_INFRAVM_IP | select(type == "string" and length > 0)' "$project/site.json" 2>/dev/null) || infravm_ip=
    [[ -f /var/lib/sophon/runner.tar ]]
    install -d -m 0700 /var/lib/sophon/stacks "$HOME/.local/share/containers/storage" "$HOME/sophon-coarse"
    if [[ ! -e $HOME/.local/share/containers/storage/volumes ]]; then
        [[ $(stat -f -c %T "$HOME/.local/share/containers/storage") == btrfs ]] || {
            echo "Persistent /var must be Btrfs for Podman volume snapshots." >&2; return 1;
        }
        btrfs subvolume create "$HOME/.local/share/containers/storage/volumes" >/dev/null
    fi
    systemctl --user start podman.socket
    if ! podman --remote=false image exists sophon:latest; then
        podman --remote=false load -i /var/lib/sophon/runner.tar
    fi
    podman --remote=false image exists sophon:latest
    dns_server=$infravm_ip
    dns_frontend_ids=$(podman --remote=false ps -a \
        --filter label=sophon.dns.frontend=adguardhome --format '{{.ID}}') || {
        echo "Could not inspect existing LAN DNS frontends." >&2; return 1;
    }
    dns_frontends=()
    [[ -z $dns_frontend_ids ]] || mapfile -t dns_frontends <<< "$dns_frontend_ids"
    [[ ${#dns_frontends[@]} -le 1 ]] || {
        echo "Multiple containers are marked as the LAN DNS frontend." >&2; return 1;
    }
    if [[ ${#dns_frontends[@]} == 1 ]]; then
        [[ $(podman --remote=false inspect --format '{{.HostConfig.NetworkMode}}' "${dns_frontends[0]}") == host ]] || {
            echo "The marked LAN DNS frontend must use host networking." >&2; return 1;
        }
        # pasta clones the LAN IP into its namespace; only this mapped address reaches the host.
        dns_server=169.254.1.2
    fi
    if podman --remote=false network exists proxy; then
        if [[ -n $dns_server ]]; then
            while IFS= read -r old_dns; do
                [[ -n $old_dns && $old_dns != "$dns_server" ]] || continue
                podman --remote=false network update --dns-drop "$old_dns" proxy || true
            done < <(podman --remote=false network inspect proxy --format '{{range .NetworkDNSServers}}{{println .}}{{end}}' 2>/dev/null || true)
            podman --remote=false network update --dns-add "$dns_server" proxy
        fi
    else
        podman --remote=false network create ${dns_server:+--dns "$dns_server"} \
            --label com.docker.compose.network=proxy proxy
    fi
    for attempt in {1..30}; do
        if [[ -S /run/user/1000/podman/podman.sock ]] && podman --remote=false info >/dev/null 2>&1; then
            touch /run/user/1000/sophon-ready
            printf '%s\n' 'Sophon is ready. Log in as core and run: sophon-deploy'
            return 0
        fi
        sleep 1
    done
    echo "Podman socket is not ready; retry: sudo systemctl restart sophon-host-bootstrap" >&2
    return 1
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
