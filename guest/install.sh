#!/usr/bin/env bash
# Run with sudo bash /<artifact-mount>/install.sh [--auto] from the CoreOS live console. The Sophon live ISO runs it
# with --auto by itself (see guest/sophon-autoinstall).
set -euo pipefail
umask 077

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
is_block_device() { [[ -b $1 ]]; }

verify_bundle() {
    local media=$1 line path found
    local -A listed=()
    [[ -f $media/SHA256SUMS ]] || die "Missing SHA256SUMS."
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^[[:xdigit:]]{64}\ \ (.+)$ ]] || die "Invalid checksum manifest."
        path=${BASH_REMATCH[1]}
        path=${path#./}
        [[ -n $path && $path != /* && $path != SHA256SUMS &&
            $path != *\\* && ! $path =~ [[:cntrl:]] &&
            ! /$path/ =~ /\.\.?/ && ! -v 'listed[$path]' ]] ||
            die "Unsafe or duplicate checksum path."
        listed["$path"]=1
    done < "$media/SHA256SUMS"
    [[ ${#listed[@]} -gt 0 ]] || die "Empty checksum manifest."
    found=$(find "$media" ! -type d ! -type f -print -quit)
    [[ -z $found ]] || die "Artifact media must contain only regular files and directories."
    while IFS= read -r -d '' path; do
        path=${path#"$media/"}
        [[ $path == SHA256SUMS || -v 'listed[$path]' ]] ||
            die "File missing from checksum manifest: $path"
    done < <(find "$media" -type f -print0)
    (cd "$media" && sha256sum --strict --check SHA256SUMS) ||
        die "Artifact checksum verification failed; do not install."
    for path in install.sh runner.tar tools/busybox project/src/console.py \
        guest/sophon-deploy guest/host-bootstrap.sh guest/sophon-network guest/sophon-set-password \
        project/bootstrap/portainer-image project/bootstrap/portainer-archive; do
        [[ -v 'listed[$path]' && -f $media/$path ]] || die "Missing required artifact: $path"
    done
}

# The only disk that passes every blank-disk check, if there is exactly one; empty otherwise.
detect_disk() {
    local name found=()
    while IFS= read -r name; do
        ( validate_disk "$name" ) >/dev/null 2>&1 && found+=("$name")
    done < <(lsblk -dnrpo NAME,TYPE,RO 2>/dev/null | awk '$2 == "disk" && $3 == 0 { print $1 }')
    (( ${#found[@]} == 1 )) && printf '%s\n' "${found[0]}"
    return 0
}

# ask VARIABLE 'Prompt' [default]: Enter alone accepts the default shown in brackets.
ask() {
    local answer
    read -r -p "$2${3:+ [$3]}: " answer
    printf -v "$1" '%s' "${answer:-${3:-}}"
}

validate_disk() {
    local disk=$1 type readonly children mounts signatures bytes source device
    [[ $disk == /dev/* ]] && is_block_device "$disk" ||
        die "Select a whole block disk under /dev."
    type=$(lsblk -dnro TYPE "$disk")
    readonly=$(lsblk -dnro RO "$disk")
    [[ $type == disk && $readonly == 0 ]] || die "Target must be a writable whole disk, not an ISO."
    children=$(lsblk -nrpo NAME "$disk")
    [[ $children == "$disk" ]] || die "Target has partitions or dependent devices; existing installations are preserved."
    mounts=$(lsblk -nro MOUNTPOINTS "$disk")
    [[ -z ${mounts//[[:space:]]/} ]] || die "Target is mounted; live and installed disks cannot be overwritten."
    mounts=$(findmnt -rn -o SOURCE)
    while IFS= read -r source; do
        [[ $source == /dev/* ]] || continue
        source=${source%%\[*}
        children=$(lsblk -snrpo NAME "$source") ||
            die "Cannot establish the parent devices of a mounted filesystem."
        while IFS= read -r device; do
            [[ $(readlink -f "$device") != "$disk" ]] ||
                die "Target backs a mounted filesystem (possibly the live ISO)."
        done <<< "$children"
    done <<< "$mounts"
    signatures=$(wipefs --no-act --noheadings --output TYPE "$disk")
    [[ -z $signatures ]] || die "Target is nonblank; existing filesystems are preserved."
    bytes=$(blockdev --getsize64 "$disk")
    [[ $bytes =~ ^[0-9]+$ && $bytes -gt 0 ]] || die "Cannot determine target size."
}

required_bytes() {
    local media=$1 project runner
    project=$(du -sb "$media/project"); project=${project%%[[:space:]]*}
    runner=$(stat -c %s "$media/runner.tar")
    # Reserve OS/headroom plus six copies for archives, layers and writable storage.
    printf '%s\n' "$(( 20 * 1024 * 1024 * 1024 + 6 * (project + runner) ))"
}

json_string() {
    local value=$1
    value=${value//\\/\\\\}; value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}; value=${value//$'\r'/\\r}; value=${value//$'\t'/\\t}
    printf '"%s"' "$value"
}

ignition_file() {
    local path=$1 mode=$2 content=$3 encoded
    encoded=$(printf %s "$content" | base64 -w0)
    printf '{"path":"%s","mode":%s,"contents":{"source":"data:;base64,%s"}}' "$path" "$mode" "$encoded"
}

render_ignition() {
    local media=$1 unit password_unit
    unit='[Unit]
Description=Sophon offline host bootstrap
After=network-online.target user@1000.service
Wants=network-online.target
Requires=user@1000.service
ConditionPathExists=/var/lib/sophon/project/bootstrap/portainer-image

[Service]
Type=oneshot
RemainAfterExit=yes
User=core
Environment=HOME=/var/home/core
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
ExecStartPre=+/usr/sbin/restorecon -RF /var/lib/sophon
ExecStart=/usr/local/libexec/sophon-host-bootstrap
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
'
    # The console login is locked until the operator chooses a password here, before the first login prompt.
    password_unit='[Unit]
Description=Sophon first-boot console password
ConditionPathExists=!/var/lib/sophon-core-password-set
After=systemd-user-sessions.service plymouth-quit-wait.service
Before=getty@tty1.service getty.target

[Service]
Type=oneshot
StandardInput=tty-force
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
ExecStart=/usr/local/libexec/sophon-set-password

[Install]
WantedBy=multi-user.target
'
    printf '{"ignition":{"version":"3.4.0"},"passwd":{"users":[{"name":"core","uid":1000,"groups":["sudo","wheel"]}]},"storage":{"files":['
    ignition_file /etc/hostname 420 sophon-infravm; printf ','
    ignition_file /etc/sysctl.d/99-sophon-lowports.conf 420 $'net.ipv4.ip_unprivileged_port_start=0\n'; printf ','
    ignition_file /etc/systemd/resolved.conf.d/nostub.conf 420 $'[Resolve]\nDNSStubListener=no\n'; printf ','
    ignition_file /etc/zincati/config.d/90-disable-auto-reboot.toml 420 $'[updates]\nenabled = false\n'; printf ','
    ignition_file /etc/ssh/sshd_config.d/40-sophon-console-only.conf 420 $'PasswordAuthentication no\nKbdInteractiveAuthentication no\n'; printf ','
    ignition_file /var/lib/systemd/linger/core 420 ''; printf ','
    ignition_file /etc/issue.d/sophon.issue 420 $'Sophon: log in as core, then run sophon-deploy (it asks for the network).\nIf setup failed: sudo systemctl restart sophon-host-bootstrap\nIf firmware boots the live ISO again, select the installed disk; do not reinstall.\n'; printf ','
    ignition_file /usr/local/bin/sophon-deploy 493 "$(cat "$media/guest/sophon-deploy")"; printf ','
    ignition_file /usr/local/libexec/sophon-host-bootstrap 493 "$(cat "$media/guest/host-bootstrap.sh")"; printf ','
    ignition_file /usr/local/libexec/sophon-network 493 "$(cat "$media/guest/sophon-network")"; printf ','
    ignition_file /usr/local/libexec/sophon-set-password 493 "$(cat "$media/guest/sophon-set-password")"
    printf '],"links":[{"path":"/etc/systemd/user/sockets.target.wants/podman.socket","target":"/usr/lib/systemd/user/podman.socket"},{"path":"/etc/systemd/user/default.target.wants/podman-restart.service","target":"/usr/lib/systemd/user/podman-restart.service"}]},"systemd":{"units":[{"name":"sophon-host-bootstrap.service","enabled":true,"contents":'
    json_string "$unit"
    printf '},{"name":"sophon-set-password.service","enabled":true,"contents":'
    json_string "$password_unit"
    printf '}]}}\n'
}

installed_var() {
    local root=$1 path
    local -a candidates=()
    # FCOS /var belongs to the OSTree stateroot, not the mounted physical root.
    for path in "$root"/ostree/deploy/*/var; do
        [[ -d $path && ! -L $path ]] || continue
        [[ -d ${path%/var}/deploy ]] || continue
        candidates+=("$path")
    done
    [[ ${#candidates[@]} == 1 ]] || die "Expected exactly one installed OSTree stateroot/var; refusing ambiguous copy."
    printf '%s\n' "${candidates[0]}"
}

grow_installed_root() {
    local disk=$1 part=$2 number output
    number=$(lsblk -dnro PARTN "$part")
    [[ $number =~ ^[1-9][0-9]*$ && $(lsblk -dnro FSTYPE "$part") == xfs ]] ||
        die "Expected a numbered XFS root partition on the newly installed disk."
    if output=$(growpart "$disk" "$number" 2>&1); then
        printf '%s\n' "$output"
    elif [[ $output == NOCHANGE:* ]]; then
        printf '%s\n' "$output"
    else
        die "Cannot expand the installed root partition: $output"
    fi
    # Normalize growpart's GPT bounds before CoreOS's first-boot sgdisk update.
    sgdisk --move-main-table=2 "$disk"
    udevadm settle
}

copy_artifacts() {
    local media=$1 root=$2 var available needed
    var=$(installed_var "$root")
    available=$(df -B1 --output=avail "$var" | tail -n 1)
    needed=$(required_bytes "$media")
    (( available >= needed - 12 * 1024 * 1024 * 1024 )) ||
        die "Installed persistent /var lacks artifact and image extraction capacity."
    [[ ! -e $var/lib/sophon ]] || die "Persistent Sophon data already exists; refusing to replace it."
    install -d -m 0755 "$var/lib/sophon"
    cp -a -- "$media/project" "$var/lib/sophon/project"
    cp -- "$media/runner.tar" "$var/lib/sophon/runner.tar"
    # ISO files are read-only; the runner must be able to write its bind mount.
    chmod -R u+rwX "$var/lib/sophon"
    chown -R 1000:1000 "$var/lib/sophon"
    # The first-boot service restores these labels with the installed policy.
    sync
}

main() {
    local auto=0
    case ${1:-} in
        '') ;;
        --auto) auto=1 ;;
        *) die "Usage: sudo bash /<artifact-mount>/install.sh [--auto] (fresh-disk install only)." ;;
    esac
    [[ $# -le 1 ]] || die "Usage: sudo bash /<artifact-mount>/install.sh [--auto] (fresh-disk install only)."
    [[ $EUID == 0 ]] || die "Run with sudo from the CoreOS live console."
    [[ -e /run/ostree-live ]] ||
        die "This installer only runs in a CoreOS live environment, never on the installed host."
    local media disk required work root part source
    media=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    verify_bundle "$media"
    [[ -x $media/tools/busybox ]] || die "Missing executable static BusyBox."
    for source in coreos-installer growpart sgdisk xfs_growfs; do
        command -v "$source" >/dev/null || die "Required live installer tool is unavailable: $source"
    done
    printf '%s\n' 'Fresh-disk installation only. Existing installations must use sophon-deploy or restart sophon-host-bootstrap.'
    lsblk -e7 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL
    disk=$(detect_disk)
    if [[ -n $disk ]]; then
        printf 'Target disk: %s (the only blank disk)\n' "$disk"
    elif (( auto )); then
        # Unattended runs never guess: a second boot of this ISO after installing finds no blank disk and stops here.
        die "Automatic install needs exactly one blank disk and found none or several (already installed? boot the installed disk, not the ISO). To choose a disk yourself: sudo bash $media/install.sh"
    else
        ask disk 'No single blank disk found automatically. Target whole disk (for example /dev/vda)' ''
    fi
    disk=$(readlink -f -- "$disk")
    validate_disk "$disk"
    required=$(required_bytes "$media")
    (( $(blockdev --getsize64 "$disk") >= required )) ||
        die "Disk is too small; need at least $required bytes for OS, artifacts and image extraction."
    work=/run/sophon-install
    install -d -m 0700 "$work"
    render_ignition "$media" > "$work/config.ign"
    printf 'ALL DATA ON %s WILL BE DESTROYED (it is blank). Installing in 10 seconds; press Ctrl-C to abort.\n' "$disk"
    sleep 10
    # Recheck immediately before the sole destructive command.
    validate_disk "$disk"
    coreos-installer install --offline --console tty0 --console ttyS0,115200n8 \
        --ignition-file "$work/config.ign" "$disk"
    udevadm settle
    part=$(lsblk -nrpo NAME,PARTLABEL "$disk" | awk '$2 == "root" {print $1}')
    [[ -n $part && $part != *$'\n'* ]] && is_block_device "$part" ||
        die "Cannot identify the single installed root partition. Do not reinstall."
    source=$(lsblk -dnro PKNAME "$part")
    [[ /dev/$source == "$disk" || $source == "$disk" ]] ||
        die "Installed root partition is not on the selected disk."
    grow_installed_root "$disk" "$part"
    root=$work/installed-root
    install -d -m 0700 "$root"
    mount "$part" "$root"
    trap 'umount /run/sophon-install/installed-root || true' EXIT
    xfs_growfs "$root"
    copy_artifacts "$media" "$root"
    umount "$root"
    trap - EXIT
    rm -f "$work/config.ign"
    printf '%s\n' \
        'Installation complete. Rebooting to the installed disk.' \
        'On first boot choose the core console password, log in as core, then run: sophon-deploy' \
        'If firmware boots the live ISO again, select the disk or detach ISOs. DO NOT reinstall.' \
        'For setup failures: journalctl -u sophon-host-bootstrap; sudo systemctl restart sophon-host-bootstrap'
    sync
    systemctl reboot
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
