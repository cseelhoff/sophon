#!/bin/sh
# Provision or manage production Sophon VMs on Proxmox, VMware vCenter, or Microsoft Hyper-V.
#   sh sophon-provision.sh                  # deploy SFTP Backup VM (if paired) + production InfraVM
#   sh sophon-provision.sh --recreate       # replace production InfraVM in place (safe standby until activated)
#   sh sophon-provision.sh backup           # deploy only the standalone Alpine SFTP Backup Server VM
set -eu
cd "$(dirname "$0")"
die() { echo "sophon-provision: $*" >&2; exit 1; }

[ ! -L .env ] || die "refusing to read symlinked env file: .env"
[ ! -f .env ] || chmod 600 .env 2>/dev/null || true

case ${1:-} in
    ''|-h|--help) [ -z "${1:-}" ] && command=provision || { command=$1; shift; } ;;
    -*) command=provision ;;
    backup) command=provision-backup; shift ;;
    provision|provision-backup|test|shell|help) command=$1; shift ;;
    dev|provision-dev|verify-restore)
        shift
        die "dev/drill provisioning lives in sophon-provision-dev.sh — run: sh sophon-provision-dev.sh${*:+ $*}" ;;
    destroy-vm)
        shift
        die "VM deletion lives in sophon-destroy-vm.sh — run: sh sophon-destroy-vm.sh${*:+ $*}" ;;
    build-isos|prepare|secrets|certificates)
        sub=${1#build-isos}; shift
        die "ISO/secrets commands live in sophon-build-isos.sh — run: sh sophon-build-isos.sh${sub:+ $sub}${*:+ $*}" ;;
    *) die "unknown command '$1' (expected backup or --recreate; see also sophon-provision-dev.sh and sophon-destroy-vm.sh)" ;;
esac

engine=$(command -v docker >/dev/null 2>&1 && echo docker || { command -v podman >/dev/null 2>&1 && echo podman; }) || true
[ -n "$engine" ] || die "Docker or Podman is required."
"$engine" info >/dev/null 2>&1 || die "$engine is installed but not running or not reachable."

if socket=$("$engine" info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null) && [ -n "$socket" ]; then
    socket=${socket#unix://}
    case $socket in /*) ;; *) socket=/run/user/$(id -u)/podman/podman.sock ;; esac
    [ "$(uname)" != Linux ] || [ -S "$socket" ] || systemctl --user enable --now podman.socket >/dev/null 2>&1 || true
else
    socket=/var/run/docker.sock
fi

"$engine" image inspect sophon-tools >/dev/null 2>&1 || "$engine" build -t sophon-tools -f Containerfile .

set -- sophon-tools "$command" "$@"
for name in $(awk 'BEGIN { for (k in ENVIRON) if (k ~ /^(SOPHON_|CF_DNS_API_TOKEN$)/) print k }'); do
    set -- -e "$name" "$@"
done
set -- -e SOPHON_LOAD_DOTENV=1 -e SOPHON_ENTRYPOINT=provision -v "$PWD:/workspace" -v "$socket:/var/run/docker.sock" -v sophon-state:/state "$@"
if [ -t 0 ] && [ -t 1 ]; then
    set -- -it "$@"
fi
exec "$engine" run --rm --init -e PYTHONUNBUFFERED=1 "$@"
