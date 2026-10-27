#!/usr/bin/env bash
# Run with sudo bash /<artifact-mount>/install.sh from the stock FCOS live console.
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
        guest/sophon-controller guest/host-bootstrap.sh \
        project/bootstrap/portainer-image project/bootstrap/portainer-archive; do
        [[ -v 'listed[$path]' && -f $media/$path ]] || die "Missing required artifact: $path"
    done
}

ipv4_number() {
    local value=$1 part result=0
    local -a parts
    [[ $value =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    IFS=. read -r -a parts <<< "$value"
    for part in "${parts[@]}"; do
        [[ ${#part} -le 3 && ( $part == 0 || $part != 0* ) ]] || return 1
        (( 10#$part <= 255 )) || return 1
        result=$(( (result << 8) | 10#$part ))
    done
    printf '%s\n' "$result"
}

validate_network() {
    local ip=$1 prefix=$2 gateway=$3 dns=$4 ipn gwn dnsn mask hostmask
    [[ $prefix =~ ^([1-9]|[12][0-9]|30)$ ]] || die "Prefix must be 1 through 30."
    ipn=$(ipv4_number "$ip") || die "Invalid IPv4 address."
    gwn=$(ipv4_number "$gateway") || die "Invalid gateway IPv4 address."
    dnsn=$(ipv4_number "$dns") || die "Invalid DNS IPv4 address."
    # Reject unspecified, multicast, reserved and loopback addresses.
    for number in "$ipn" "$gwn" "$dnsn"; do
        (( number > 0 && (number >> 24) != 127 && (number >> 24) < 224 )) ||
            die "Use unicast, non-loopback IPv4 addresses."
    done
    mask=$(( (0xffffffff << (32 - prefix)) & 0xffffffff ))
    hostmask=$(( 0xffffffff ^ mask ))
    (( (ipn & mask) == (gwn & mask) && ipn != gwn )) ||
        die "Gateway must be distinct and in the same subnet."
    (( (ipn & hostmask) != 0 && (ipn & hostmask) != hostmask &&
       (gwn & hostmask) != 0 && (gwn & hostmask) != hostmask )) ||
        die "IP and gateway must not be network or broadcast addresses."
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
    printf 'Checking that every byte of %s is blank; this can take several minutes.\n' "$disk"
    cmp --silent --bytes="$bytes" "$disk" /dev/zero ||
        die "Target contains data or cannot be read; only fresh zero-filled disks are supported."
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
    local media=$1 ip=$2 prefix=$3 gateway=$4 dns=$5 hash=$6 unit network
    validate_network "$ip" "$prefix" "$gateway" "$dns"
    [[ $hash =~ ^\$6\$[a-zA-Z0-9./]{1,16}\$[a-zA-Z0-9./]{86}$ ]] ||
        die "Invalid SHA-512 password hash."
    network=$(printf '[connection]\nid=sophon-static\ntype=ethernet\n\n[ipv4]\nmethod=manual\naddress1=%s/%s,%s\ndns=%s;\n\n[ipv6]\nmethod=disabled\n' "$ip" "$prefix" "$gateway" "$dns")
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
    printf '{"ignition":{"version":"3.4.0"},"passwd":{"users":[{"name":"core","uid":1000,"passwordHash":"%s","groups":["sudo","wheel"]}]},"storage":{"files":[' "$hash"
    ignition_file /etc/hostname 420 sophon-infravm; printf ','
    ignition_file /etc/NetworkManager/system-connections/sophon-static.nmconnection 384 "$network"; printf ','
    ignition_file /etc/sysctl.d/99-sophon-lowports.conf 420 $'net.ipv4.ip_unprivileged_port_start=0\n'; printf ','
    ignition_file /etc/systemd/resolved.conf.d/nostub.conf 420 $'[Resolve]\nDNSStubListener=no\n'; printf ','
    ignition_file /etc/zincati/config.d/90-disable-auto-reboot.toml 420 $'[updates]\nenabled = false\n'; printf ','
    ignition_file /etc/ssh/sshd_config.d/40-sophon-console-only.conf 420 $'PasswordAuthentication no\nKbdInteractiveAuthentication no\n'; printf ','
    ignition_file /var/lib/systemd/linger/core 420 ''; printf ','
    ignition_file /etc/issue.d/sophon.issue 420 $'Sophon: log in as core, then run sophon-controller.\nIf setup failed: sudo systemctl restart sophon-host-bootstrap\nIf firmware boots the live ISO again, select the installed disk; do not reinstall.\n'; printf ','
    ignition_file /usr/local/bin/sophon-controller 493 "$(cat "$media/guest/sophon-controller")"; printf ','
    ignition_file /usr/local/libexec/sophon-host-bootstrap 493 "$(cat "$media/guest/host-bootstrap.sh")"
    printf '],"links":[{"path":"/etc/systemd/user/sockets.target.wants/podman.socket","target":"/usr/lib/systemd/user/podman.socket"},{"path":"/etc/systemd/user/default.target.wants/podman-restart.service","target":"/usr/lib/systemd/user/podman-restart.service"}]},"systemd":{"units":[{"name":"sophon-host-bootstrap.service","enabled":true,"contents":'
    json_string "$unit"
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
    local media=$1 root=$2 ip=$3 dns=$4 var available needed
    ipv4_number "$ip" >/dev/null || die "Invalid installed IP."
    ipv4_number "$dns" >/dev/null || die "Invalid installed DNS."
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
    printf '{"SOPHON_INFRAVM_IP":"%s","SOPHON_VNET_DNS":"%s"}\n' "$ip" "$dns" \
        > "$var/lib/sophon/project/site.json"
    chown -R 1000:1000 "$var/lib/sophon"
    # The first-boot service restores these labels with the installed policy.
    sync
}

main() {
    [[ $# == 0 ]] || die "Usage: sudo bash /<artifact-mount>/install.sh (interactive fresh-disk install only)."
    [[ $EUID == 0 ]] || die "Run with sudo from the CoreOS live console."
    [[ -e /run/ostree-live ]] ||
        die "This installer only runs in a CoreOS live environment, never on the installed host."
    local media disk ip prefix gateway dns password confirmation hash required work root part source
    media=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    verify_bundle "$media"
    [[ -x $media/tools/busybox ]] || die "Missing executable static BusyBox."
    for source in coreos-installer growpart sgdisk xfs_growfs; do
        command -v "$source" >/dev/null || die "Required live installer tool is unavailable: $source"
    done
    printf '%s\n' 'Fresh-disk installation only. Existing installations must use sophon-controller or restart sophon-host-bootstrap.'
    lsblk -e7 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL
    read -r -p 'Fresh target whole disk (for example /dev/vda): ' disk
    disk=$(readlink -f -- "$disk")
    validate_disk "$disk"
    required=$(required_bytes "$media")
    (( $(blockdev --getsize64 "$disk") >= required )) ||
        die "Disk is too small; need at least $required bytes for OS, artifacts and image extraction."
    read -r -p 'Installed IPv4 address: ' ip
    read -r -p 'IPv4 prefix length (1-30): ' prefix
    read -r -p 'IPv4 gateway: ' gateway
    read -r -p 'DNS server IPv4 address: ' dns
    validate_network "$ip" "$prefix" "$gateway" "$dns"
    read -r -s -p 'New core console password: ' password; printf '\n'
    read -r -s -p 'Confirm core console password: ' confirmation; printf '\n'
    [[ -n $password && $password == "$confirmation" ]] || die "Passwords are empty or do not match."
    # BusyBox -P 0 reads stdin; omitting -P would prompt/read /dev/tty.
    hash=$(printf '%s\n' "$password" | "$media/tools/busybox" mkpasswd -m sha512 -P 0)
    unset password confirmation
    work=/run/sophon-install
    install -d -m 0700 "$work"
    render_ignition "$media" "$ip" "$prefix" "$gateway" "$dns" "$hash" > "$work/config.ign"
    unset hash
    printf 'ALL DATA ON %s WILL BE DESTROYED. Type exactly: INSTALL %s\n' "$disk" "$disk"
    read -r confirmation
    [[ $confirmation == "INSTALL $disk" ]] || die "Disk confirmation did not match."
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
    copy_artifacts "$media" "$root" "$ip" "$dns"
    umount "$root"
    trap - EXIT
    rm -f "$work/config.ign"
    printf '%s\n' \
        'Installation complete. Rebooting to the installed disk.' \
        'Log in as core with your console password, wait for host setup, then run: sophon-controller' \
        'Optional dashboard: sophon-controller --dashboard' \
        'If firmware boots the live ISO again, select the disk or detach ISOs. DO NOT reinstall.' \
        'For setup failures: journalctl -u sophon-host-bootstrap; sudo systemctl restart sophon-host-bootstrap'
    sync
    systemctl reboot
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
