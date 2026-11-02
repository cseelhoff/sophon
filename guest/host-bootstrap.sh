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
    local project=/var/lib/sophon/project image archive image_archive infravm_ip dns_server dns_frontend_ids
    local -a lines dns_frontends
    rm -f /run/user/1000/sophon-ready
    mapfile -t lines < "$project/bootstrap/portainer-image"
    [[ ${#lines[@]} == 1 && ${lines[0]} =~ ^[a-zA-Z0-9][a-zA-Z0-9./:@_-]*$ ]] || {
        echo "Invalid Portainer image metadata." >&2; return 1;
    }
    image=${lines[0]}
    mapfile -t lines < "$project/bootstrap/portainer-archive"
    [[ ${#lines[@]} == 1 && ${lines[0]} =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*\.tar$ ]] || {
        echo "Invalid Portainer archive metadata." >&2; return 1;
    }
    archive=$project/artifacts/containers/${lines[0]}
    # The address arrives later, when sophon-deploy asks for the network; until then Portainer needs no DNS server.
    infravm_ip=$(jq -er '.SOPHON_INFRAVM_IP | select(type == "string" and length > 0)' "$project/site.json" 2>/dev/null) || infravm_ip=
    [[ -f $archive && -f /var/lib/sophon/runner.tar ]]
    systemctl --user start podman.socket
    for image_archive in "sophon:latest|/var/lib/sophon/runner.tar" "$image|$archive"; do
        if ! podman --remote=false image exists "${image_archive%%|*}"; then
            podman --remote=false load -i "${image_archive#*|}"
        fi
        podman --remote=false image exists "${image_archive%%|*}"
    done
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
            if [[ ${#dns_frontends[@]} == 1 && -n $infravm_ip ]]; then
                podman --remote=false network update --dns-drop "$infravm_ip" proxy
            fi
            podman --remote=false network update --dns-add "$dns_server" proxy
        fi
    else
        podman --remote=false network create ${dns_server:+--dns "$dns_server"} \
            --label com.docker.compose.network=proxy proxy
    fi
    if ! podman --remote=false volume exists portainer_data; then
        podman --remote=false volume create portainer_data
    fi
    if podman --remote=false container exists portainer; then
        podman --remote=false restart portainer
    else
        podman --remote=false run -d --pull never --name portainer --restart always \
            --security-opt label=disable --network proxy -p 9443:9443 \
            --label sophon.backup.version=2 \
            --label sophon.backup.group=portainer \
            --label sophon.backup.stop-during-backup=false \
            --label sophon.backup.stop-timeout=120 \
            --label sophon.backup.covers.0.path=/data \
            --label sophon.backup.paths.0.type=portainer-backup \
            --label sophon.backup.paths.0.name=control-plane \
            -v /run/user/1000/podman/podman.sock:/var/run/docker.sock \
            -v portainer_data:/data:Z "$image" --http-enabled
    fi
    [[ $(podman --remote=false inspect --format '{{.State.Running}}' portainer) == true ]]
    # Portainer HTTP may take a few seconds after the container starts.
    local attempt
    for attempt in {1..60}; do
        if curl --fail --silent --insecure --max-time 2 \
            https://127.0.0.1:9443/api/status >/dev/null; then
            touch /run/user/1000/sophon-ready
            printf '%s\n' 'Sophon is ready. Log in as core and run: sophon-deploy'
            return 0
        fi
        sleep 2
    done
    echo "Portainer is not ready; retry: sudo systemctl restart sophon-host-bootstrap" >&2
    return 1
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
