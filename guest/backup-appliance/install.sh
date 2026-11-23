#!/bin/sh
# Install the prebuilt Sophon Backup Server OS onto a blank disk from the live Alpine installer ISO.
# Usage: /usr/local/libexec/sophon-backup-install [--auto]
set -eu

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run the installer as root."
[ ! -e /etc/sophon-backup-installed ] || die "Already booted from the installed Sophon Backup Server."

auto=0
if [ "${1:-}" = "--auto" ]; then
    auto=1
    shift
fi
[ $# -eq 0 ] || die "Usage: sophon-backup-install [--auto]"

find_media() {
    for candidate in /media/cdrom /media/sr0 /media/sr1 /media/usb; do
        if [ -f "$candidate/sophon-backup-os.tar.gz" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    mkdir -p /mnt/sophon-media
    for dev in /dev/disk/by-label/SOPHON_BACKUP_INSTALL /dev/sr0 /dev/sr1 /dev/sr2; do
        [ -b "$dev" ] || continue
        if mount -o ro "$dev" /mnt/sophon-media 2>/dev/null; then
            if [ -f /mnt/sophon-media/sophon-backup-os.tar.gz ]; then
                printf '%s\n' /mnt/sophon-media
                return 0
            fi
            umount /mnt/sophon-media 2>/dev/null || true
        fi
    done
    return 1
}

is_blank_disk() {
    name=$1
    dev="/dev/$name"
    [ -b "$dev" ] || return 1
    for child in "/sys/block/$name/$name"*; do
        [ ! -e "$child" ] || return 1
    done
    if sfdisk -d "$dev" >/dev/null 2>&1; then
        return 1
    fi
    if [ -n "$(blkid "$dev" 2>/dev/null)" ]; then
        return 1
    fi
    return 0
}

candidate_disks() {
    for sys in /sys/block/*; do
        [ -d "$sys" ] || continue
        name=${sys##*/}
        case "$name" in
            loop*|ram*|sr*|fd*|dm-*|md*|zram*) continue ;;
        esac
        sectors=$(cat "$sys/size" 2>/dev/null || echo 0)
        # Require at least 8 GiB (16777216 512-byte sectors)
        [ "$sectors" -ge 16777216 ] || continue
        printf '%s\n' "$name"
    done
}

media=$(find_media) || die "Cannot find sophon-backup-os.tar.gz on installer media."
archive="$media/sophon-backup-os.tar.gz"

all_disks=$(candidate_disks)
[ -n "$all_disks" ] || die "No candidate target disk (>= 8 GiB) found."

blank_disks=""
blank_count=0
nonblank_count=0
for d in $all_disks; do
    if is_blank_disk "$d"; then
        blank_disks="${blank_disks:+$blank_disks }$d"
        blank_count=$(( blank_count + 1 ))
    else
        nonblank_count=$(( nonblank_count + 1 ))
    fi
done

if [ "$auto" -eq 1 ]; then
    if [ "$blank_count" -ne 1 ] || [ "$nonblank_count" -ne 0 ]; then
        die "Automatic install requires exactly one blank disk and no existing disks (found $blank_count blank, $nonblank_count non-blank). Boot from the installed disk or run /usr/local/libexec/sophon-backup-install manually."
    fi
    target="/dev/$blank_disks"
else
    [ "$blank_count" -ge 1 ] || die "No blank disk found; refusing to overwrite existing storage."
    echo "Blank disks available for Sophon Backup Server installation:"
    for d in $blank_disks; do
        gib=$(( $(cat "/sys/block/$d/size") / 2097152 ))
        printf '  /dev/%s (%s GiB)\n' "$d" "$gib"
    done
    printf 'Target disk (for example /dev/%s): ' "${blank_disks%% *}"
    IFS= read -r chosen || exit 1
    chosen=${chosen#/dev/}
    is_blank_disk "$chosen" || die "Selected disk /dev/$chosen is missing or not blank."
    target="/dev/$chosen"
fi

echo "== Installing Sophon Backup Server onto $target =="
case "$target" in
    *[0-9]) part_prefix="${target}p" ;;
    *) part_prefix="$target" ;;
esac
efi_part="${part_prefix}1"
os_part="${part_prefix}2"
data_part="${part_prefix}3"

sfdisk --quiet --wipe always --wipe-partitions always "$target" <<'EOF'
label: gpt
size=128M, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI-SYSTEM"
size=1536M, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, attrs="LegacyBIOSBootable", name="SOPHON-OS"
type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="SOPHON-DATA"
EOF

ensure_part_nodes() {
    mdev -s 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        if [ -b "$efi_part" ] && [ -b "$os_part" ] && [ -b "$data_part" ]; then
            return 0
        fi
        sleep 1
        mdev -s 2>/dev/null || true
    done
    die "Kernel did not create partition nodes on $target."
}

partx -u "$target" 2>/dev/null || true
ensure_part_nodes

# Syslinux extlinux 6.04 requires ext4 without 64bit/metadata_csum_seed on the OS partition; data partition uses full 64-bit ext4.
# Lower root block reservation to 1% (-m 1) and use 64 KiB/inode (-i 65536) for Kopia pack files on large thin-provisioned disks.
mkfs.ext4 -q -F -O ^64bit,^metadata_csum_seed -L SOPHON_BK_OS "$os_part"
ensure_part_nodes
mkfs.ext4 -q -F -m 1 -i 65536 -L SOPHON_BK_DATA "$data_part"
ensure_part_nodes

mkdir -p /mnt/target
mount -t ext4 "$os_part" /mnt/target
tar -C /mnt/target -xzpf "$archive"

# Format EFI System Partition using dosfstools from the extracted OS rootfs and install UEFI bootloader
ensure_part_nodes
/mnt/target/sbin/mkfs.vfat -F 32 -n SOPHON_EFI "$efi_part" >/dev/null
ensure_part_nodes
mkdir -p /mnt/target/boot/efi
mount -t vfat "$efi_part" /mnt/target/boot/efi
mkdir -p /mnt/target/boot/efi/EFI/BOOT
cp /mnt/target/boot/efi-template/BOOTX64.EFI /mnt/target/boot/efi/EFI/BOOT/BOOTX64.EFI

# Bind /dev, /proc, and /sys before running extlinux and ssh-keygen inside the target chroot
mount --bind /dev /mnt/target/dev
mount --bind /proc /mnt/target/proc
mount --bind /sys /mnt/target/sys

# Install Legacy BIOS bootloader (GPT MBR + extlinux) so both SeaBIOS and UEFI boot seamlessly
dd if=/mnt/target/usr/share/syslinux/gptmbr.bin of="$target" bs=440 count=1 conv=notrunc status=none
cp /mnt/target/usr/share/syslinux/ldlinux.c32 /mnt/target/boot/ 2>/dev/null || true
chroot /mnt/target /sbin/extlinux --install /boot

# Generate unique persistent SSH host keys once at install time
chroot /mnt/target /usr/bin/ssh-keygen -A
chroot /mnt/target /usr/sbin/sshd -t

umount /mnt/target/sys
umount /mnt/target/proc
umount /mnt/target/dev

# Initialize the persistent SFTP chroot partition (root:root 0755 chroot, sophon-backup 0700 /repository)
ensure_part_nodes
mkdir -p /mnt/target/var/lib/sophon-backups
mount -t ext4 "$data_part" /mnt/target/var/lib/sophon-backups
bk_uid=$(awk -F: '$1=="sophon-backup"{print $3}' /mnt/target/etc/passwd)
bk_gid=$(awk -F: '$1=="sophon-backup"{print $4}' /mnt/target/etc/passwd)
chown 0:0 /mnt/target/var/lib/sophon-backups
chmod 0755 /mnt/target/var/lib/sophon-backups
install -d -o "$bk_uid" -g "$bk_gid" -m 0700 /mnt/target/var/lib/sophon-backups/repository

sync
umount /mnt/target/var/lib/sophon-backups
umount /mnt/target/boot/efi
umount /mnt/target

printf '\n== Sophon Backup Server installation complete ==\n'
printf 'Rebooting to installed disk. On first boot choose the admin password, log in, and run: sophon-backup-setup\n'
sleep 2
reboot
