#!/bin/sh
# Build Sophon OS, app, SFTP server, and unattended secrets ISOs from `.env` (prompts for anything omitted).
#   sh sophon-build-isos.sh                      # build secrets ISOs + CoreOS/apps/SFTP ISOs
#   sh sophon-build-isos.sh --os-only            # build only secret-free CoreOS/apps/SFTP ISOs
#   sh sophon-build-isos.sh --secrets-only       # build only mode-0600 unattended secrets ISOs
#   sh sophon-build-isos.sh certificates issue   # air-gapped 90-day wildcard certificate renewal
set -eu
cd "$(dirname "$0")"
die() { echo "sophon-build-isos: $*" >&2; exit 1; }

[ ! -L .env ] || die "refusing to read symlinked env file: .env"
[ ! -f .env ] || chmod 600 .env 2>/dev/null || true

case ${1:-} in
    ''|-h|--help) [ -z "${1:-}" ] && command=build-isos || { command=$1; shift; } ;;
    -*) command=build-isos ;;
    build-isos|prepare|secrets|certificates|help) command=$1; shift ;;
    *) die "unknown command '$1' (use sophon-provision.sh for VM provisioning commands)" ;;
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
set -- -e SOPHON_LOAD_DOTENV=1 -e SOPHON_ENTRYPOINT=build-isos -v "$PWD:/workspace" -v "$socket:/var/run/docker.sock" -v sophon-state:/state "$@"
if [ -t 0 ] && [ -t 1 ]; then
    set -- -it "$@"
fi
exec "$engine" run --rm --init -e PYTHONUNBUFFERED=1 "$@"
