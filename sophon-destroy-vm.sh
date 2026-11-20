#!/bin/sh
# Stop and delete a Sophon-managed VM on Proxmox, VMware vCenter, or Microsoft Hyper-V.
#   sh sophon-destroy-vm.sh                             # delete SOPHON_INFRAVM_NAME (prompts for confirmation)
#   sh sophon-destroy-vm.sh --vm-name sophon-dev-infravm
set -eu
cd "$(dirname "$0")"
die() { echo "sophon-destroy-vm: $*" >&2; exit 1; }

[ ! -L .env ] || die "refusing to read symlinked env file: .env"
[ ! -f .env ] || chmod 600 .env 2>/dev/null || true

case ${1:-} in
    -h|--help|help) command=--help; shift ;;
    ''|-*) command=destroy-vm ;;
    *) die "unknown argument '$1' (expected options such as --vm-name <name> or --allow-production)" ;;
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
set -- -e SOPHON_LOAD_DOTENV=1 -e SOPHON_ENTRYPOINT=destroy-vm -v "$PWD:/workspace" -v "$socket:/var/run/docker.sock" -v sophon-state:/state "$@"
if [ -t 0 ] && [ -t 1 ]; then
    set -- -it "$@"
fi
exec "$engine" run --rm --init -e PYTHONUNBUFFERED=1 "$@"
