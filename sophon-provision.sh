#!/usr/bin/env bash
# Runs a sophon-tools command with the right container engine and socket (Docker or Podman, Linux/macOS/WSL).
#   bash sophon-provision.sh [prepare|provision|certificates|test|...] [args]   (default: provision)
# Every exported SOPHON_* variable (and CF_DNS_API_TOKEN) is passed by name, so values never appear on the command line.
set -euo pipefail
cd "$(dirname "$0")"

command=${1:-provision}
[ $# -gt 0 ] && shift
die() { echo "sophon-provision: $*" >&2; exit 1; }

# Load SOPHON_* settings from an env file; variables already set in the shell win, empty values are skipped.
load_env_file() {
    local file=$1
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        [[ $line =~ ^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$ ]] || continue
        local key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
        [[ $key == SOPHON_* || $key == CF_DNS_API_TOKEN ]] || continue
        if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then value=${BASH_REMATCH[1]}; fi
        [ -z "$value" ] || [ -n "${!key:-}" ] || export "$key=$value"
    done < "$file"
}

if [ "$command" = verify-restore ]; then
    load_env_file "${XDG_CONFIG_HOME:-$HOME/.config}/sophon/verify-restore.env"
fi
load_env_file .env

engine=$(command -v docker >/dev/null 2>&1 && echo docker || { command -v podman >/dev/null 2>&1 && echo podman; }) ||
    true
[ -n "$engine" ] || die "Docker or Podman is required."
"$engine" info >/dev/null 2>&1 || die "$engine is installed but not running or not reachable."

# A `docker` command can really be Podman (podman-docker), which has a different socket.
if socket=$("$engine" info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null) && [ -n "$socket" ]; then
    socket=${socket#unix://}
    [[ $socket == /* ]] || socket=/run/user/$(id -u)/podman/podman.sock
    if [ "$(uname)" = Linux ] && [ ! -S "$socket" ]; then
        systemctl --user enable --now podman.socket >/dev/null 2>&1 || true
    fi
else
    socket=/var/run/docker.sock
fi

"$engine" image inspect sophon-tools >/dev/null 2>&1 || "$engine" build -t sophon-tools -f Containerfile .

tty=()
if [ -t 0 ] && [ -t 1 ]; then tty=(-it); fi

need() { # need VARIABLE 'Prompt' [default]: ask only when the variable is unset
    [ -z "${!1:-}" ] || return 0
    local answer
    read -rp "$2${3:+ [$3]}: " answer
    answer=${answer:-${3:-}}
    [ -n "$answer" ] || die "$2 is required."
    export "$1=$answer"
}
need_secret() {
    [ -z "${!1:-}" ] || return 0
    local value
    read -rsp "$1: " value; echo
    [ -n "$value" ] || die "$1 is required."
    export "$1=$value"
}

extra_mounts=()
if [ "$command" = verify-restore ]; then
    if [ -t 0 ]; then
        need SOPHON_PROXMOX_HOST 'Proxmox host name or address'
        need SOPHON_PROXMOX_NODE 'Proxmox node' proxmox
        need_secret SOPHON_PROXMOX_PASSWORD
    fi
    if [ -z "${SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64:-}" ] && [ -n "${SOPHON_VERIFY_RESTORE_SFTP_PRIVATE_KEY_FILE:-}" ]; then
        key_file=${SOPHON_VERIFY_RESTORE_SFTP_PRIVATE_KEY_FILE/#\~/$HOME}
        [ -f "$key_file" ] || die "SFTP key file not found: $key_file"
        SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64=$(base64 < "$key_file" | tr -d '\r\n')
        export SOPHON_KOPIA_SFTP_PRIVATE_KEY_B64
        unset SOPHON_VERIFY_RESTORE_SFTP_PRIVATE_KEY_FILE
    fi
    if [ -z "${SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64:-}" ] && [ -n "${SOPHON_VERIFY_RESTORE_SFTP_KNOWN_HOSTS_FILE:-}" ]; then
        kh_file=${SOPHON_VERIFY_RESTORE_SFTP_KNOWN_HOSTS_FILE/#\~/$HOME}
        [ -f "$kh_file" ] || die "SFTP known_hosts file not found: $kh_file"
        SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64=$(base64 < "$kh_file" | tr -d '\r\n')
        export SOPHON_KOPIA_SFTP_KNOWN_HOSTS_B64
        unset SOPHON_VERIFY_RESTORE_SFTP_KNOWN_HOSTS_FILE
    fi
    if [ -n "${SOPHON_VERIFY_RESTORE_CA_FILE:-}" ]; then
        ca_file=${SOPHON_VERIFY_RESTORE_CA_FILE/#\~/$HOME}
        [ -f "$ca_file" ] || die "CA file not found: $ca_file"
        ca_abs=$(cd "$(dirname "$ca_file")" && pwd)/$(basename "$ca_file")
        extra_mounts+=(-v "$ca_abs:/tmp/sophon-verify-ca.pem:ro")
        export SOPHON_VERIFY_RESTORE_CA_FILE=/tmp/sophon-verify-ca.pem
    fi
fi

if [ "$command" = provision ] && [ -t 0 ]; then
    if [ -z "${SOPHON_HYPERVISOR:-}" ]; then
        printf '%s\n' 'Where should InfraVM run?' '  1) Proxmox' '  2) VMware vCenter' \
            '  3) Microsoft Hyper-V on another host (WinRM)' \
            '  4) Nowhere yet: only download and build the installation media (step 1), then exit'
        read -rp 'Choose 1-4: ' choice
        case $choice in
            1) export SOPHON_HYPERVISOR=proxmox ;;
            2) export SOPHON_HYPERVISOR=vcenter ;;
            3) export SOPHON_HYPERVISOR=hyperv ;;
            4) command=prepare ;;
            *) die "Choose a number from 1 to 4." ;;
        esac
    fi
    if [ "$command" = provision ]; then
        case ${SOPHON_HYPERVISOR:-proxmox} in
            vcenter)
                need SOPHON_VCENTER_URL 'vCenter URL (https://...)'
                need SOPHON_VCENTER_USERNAME 'vCenter username' administrator@vsphere.local
                need SOPHON_VCENTER_DATACENTER 'Datacenter'
                need SOPHON_VCENTER_CLUSTER 'Cluster'
                need SOPHON_VCENTER_DATASTORE 'Datastore'
                need SOPHON_VCENTER_NETWORK 'Port group'
                need_secret SOPHON_VCENTER_PASSWORD ;;
            hyperv)
                need SOPHON_HYPERV_HOST 'Hyper-V host name or address'
                need SOPHON_HYPERV_USERNAME 'Hyper-V username' Administrator
                need SOPHON_HYPERV_SWITCH 'Virtual switch'
                need_secret SOPHON_HYPERV_PASSWORD ;;
            *)
                need SOPHON_PROXMOX_HOST 'Proxmox host name or address'
                need SOPHON_PROXMOX_NODE 'Proxmox node' proxmox
                need_secret SOPHON_PROXMOX_PASSWORD ;;
        esac
    fi
fi

env_args=()
while IFS= read -r name; do env_args+=(-e "$name"); done < <(compgen -e | grep -E '^(SOPHON_|CF_DNS_API_TOKEN$)' || true)

exec "$engine" run --rm --init ${tty[@]+"${tty[@]}"} \
    -v "$PWD:/workspace" -v "$socket:/var/run/docker.sock" -v sophon-state:/state \
    ${extra_mounts[@]+"${extra_mounts[@]}"} \
    ${env_args[@]+"${env_args[@]}"} sophon-tools "$command" "$@"
