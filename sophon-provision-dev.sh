#!/bin/sh
# Provision and auto-restore a dev/drill Sophon InfraVM in safe standby mode on SOPHON_DEV_NETWORK_CIDR.
#   sh sophon-provision-dev.sh                  # recreate & restore sophon-dev-infravm in safe standby mode
#   sh sophon-provision-dev.sh --wait-https     # also verify restored HTTPS endpoints from this workstation
set -eu
cd "$(dirname "$0")"
die() { echo "sophon-provision-dev: $*" >&2; exit 1; }

[ ! -L .env ] || die "refusing to read symlinked env file: .env"
[ ! -f .env ] || chmod 600 .env 2>/dev/null || true

case ${1:-} in
    -h|--help|help) command=--help; shift ;;
    ''|-*) command=provision-dev ;;
    *) die "unknown argument '$1' (expected options such as --wait-https or --secrets-iso)" ;;
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

ca_mount=
if [ "$command" = provision-dev ] && [ -n "${SOPHON_VERIFY_RESTORE_CA_FILE:-}" ]; then
    ca_file=$SOPHON_VERIFY_RESTORE_CA_FILE
    case $ca_file in '~/'*) ca_file=$HOME/${ca_file#\~/} ;; esac
    [ -f "$ca_file" ] || die "CA file not found: $ca_file"
    ca_abs=$(cd "$(dirname "$ca_file")" && pwd)/$(basename "$ca_file")
    ca_mount="$ca_abs:/tmp/sophon-verify-ca.pem:ro"
    export SOPHON_VERIFY_RESTORE_CA_FILE=/tmp/sophon-verify-ca.pem
fi

set -- sophon-tools "$command" "$@"
for name in $(awk 'BEGIN { for (k in ENVIRON) if (k ~ /^(SOPHON_|CF_DNS_API_TOKEN$)/) print k }'); do
    set -- -e "$name" "$@"
done
[ -n "$ca_mount" ] && set -- -v "$ca_mount" "$@"
set -- -e SOPHON_LOAD_DOTENV=1 -e SOPHON_ENTRYPOINT=provision-dev -v "$PWD:/workspace" -v "$socket:/var/run/docker.sock" -v sophon-state:/state "$@"
if [ -t 0 ] && [ -t 1 ]; then
    set -- -it "$@"
fi
exec "$engine" run --rm --init -e PYTHONUNBUFFERED=1 "$@"
