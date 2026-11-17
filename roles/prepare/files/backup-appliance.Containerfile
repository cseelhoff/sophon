# Builds the persistent Alpine 3.24.4 rootfs archive installed by sophon-backup-installer.iso.
# Uses only Alpine's signed package repositories (no --allow-untrusted, no proot, no guestfish).
FROM docker.io/library/alpine:3.24.4 AS builder

RUN apk add --no-cache grub grub-efi syslinux

RUN mkdir -p /rootfs /tmp/build \
 && apk --root /rootfs --initdb --keys-dir /etc/apk/keys --repositories-file /etc/apk/repositories \
    add --no-cache \
      alpine-base linux-virt mkinitfs syslinux e2fsprogs dosfstools \
      openssh chrony doas dhcpcd util-linux-misc sfdisk

COPY sophon-backup-set-password /rootfs/usr/local/sbin/sophon-backup-set-password
COPY sophon-backup-apply /rootfs/usr/local/sbin/sophon-backup-apply
COPY sophon-backup-setup /rootfs/usr/local/bin/sophon-backup-setup
COPY sophon-authorize-key /rootfs/usr/local/bin/sophon-authorize-key

RUN set -eu; \
    chmod 0755 \
      /rootfs/usr/local/sbin/sophon-backup-set-password \
      /rootfs/usr/local/sbin/sophon-backup-apply \
      /rootfs/usr/local/bin/sophon-backup-setup \
      /rootfs/usr/local/bin/sophon-authorize-key; \
    echo 'features="ata base ide scsi usb virtio nvme ext4"' > /rootfs/etc/mkinitfs/mkinitfs.conf; \
    KVER=$(ls /rootfs/lib/modules | head -n 1); \
    chroot /rootfs mkinitfs "$KVER"; \
    mkdir -p /rootfs/boot/efi-template; \
    printf '%s\n' \
      'search --no-floppy --label --set=root SOPHON_BK_OS' \
      'linux /boot/vmlinuz-virt root=LABEL=SOPHON_BK_OS modules=sd-mod,usb-storage,ext4,virtio_blk,virtio_scsi,nvme rootfstype=ext4 quiet console=ttyS0,115200n8 console=tty0' \
      'initrd /boot/initramfs-virt' \
      'boot' > /tmp/build/grub.cfg; \
    grub-mkstandalone -O x86_64-efi -o /rootfs/boot/efi-template/BOOTX64.EFI "boot/grub/grub.cfg=/tmp/build/grub.cfg"; \
    printf '%s\n' \
      'DEFAULT sophon' \
      'PROMPT 0' \
      'TIMEOUT 10' \
      'LABEL sophon' \
      '  LINUX /boot/vmlinuz-virt' \
      '  INITRD /boot/initramfs-virt' \
      '  APPEND root=LABEL=SOPHON_BK_OS modules=sd-mod,usb-storage,ext4,virtio_blk,virtio_scsi,nvme rootfstype=ext4 quiet console=ttyS0,115200n8 console=tty0' \
      > /rootfs/boot/extlinux.conf; \
    cp /rootfs/usr/share/syslinux/ldlinux.c32 /rootfs/boot/ 2>/dev/null || true; \
    echo 'sophon-backup' > /rootfs/etc/hostname; \
    echo '127.0.0.1 localhost sophon-backup' > /rootfs/etc/hosts; \
    touch /rootfs/etc/sophon-backup-installed; \
    printf '%s\n' \
      'LABEL=SOPHON_BK_OS    /                        ext4  rw,relatime,errors=remount-ro     0 1' \
      'LABEL=SOPHON_EFI      /boot/efi                vfat  rw,relatime,fmask=0077,dmask=0077 0 2' \
      'LABEL=SOPHON_BK_DATA  /var/lib/sophon-backups  ext4  rw,noatime                        0 2' \
      > /rootfs/etc/fstab; \
    chroot /rootfs sh -eu -c ' \
      adduser -D -G wheel -s /bin/sh admin; \
      addgroup -S sophon-backup; \
      adduser -D -S -G sophon-backup -h /repository -s /sbin/nologin sophon-backup; \
      sed -i "s|^sophon-backup:[^:]*:|sophon-backup:*:|" /etc/shadow; \
      bk_uid=$(awk -F: "\$1==\"sophon-backup\"{print \$3}" /etc/passwd); \
      bk_gid=$(awk -F: "\$1==\"sophon-backup\"{print \$4}" /etc/passwd); \
      printf "sophon-restore:x:%s:%s:Sophon Read-Only Restore:/repository:/sbin/nologin\n" "$bk_uid" "$bk_gid" >> /etc/passwd; \
      printf "sophon-restore:*:19797:0:99999:7:::\n" >> /etc/shadow; \
    '; \
    install -d -m 0750 /rootfs/etc/doas.d; \
    printf '%s\n' \
      'permit persist :wheel' \
      'permit nopass :wheel as root cmd /usr/local/sbin/sophon-backup-apply' > /rootfs/etc/doas.d/sophon-backup.conf; \
    chmod 0600 /rootfs/etc/doas.d/sophon-backup.conf; \
    install -d -m 0755 /rootfs/var/lib/sophon-backups /rootfs/etc/ssh/authorized_keys /rootfs/etc/ssh/sshd_config.d; \
    install -m 0644 /dev/null /rootfs/etc/ssh/authorized_keys/sophon-backup; \
    install -m 0644 /dev/null /rootfs/etc/ssh/authorized_keys/sophon-restore; \
    printf '%s\n' \
      'PermitRootLogin no' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' > /rootfs/etc/ssh/sshd_config.d/00-sophon-hardening.conf; \
    printf '%s\n' \
      'Match User sophon-backup' \
      '    AuthorizedKeysFile /etc/ssh/authorized_keys/sophon-backup' \
      '    ChrootDirectory /var/lib/sophon-backups' \
      '    ForceCommand internal-sftp' \
      '    PasswordAuthentication no' \
      '    KbdInteractiveAuthentication no' \
      '    AllowTcpForwarding no' \
      '    X11Forwarding no' \
      '    PermitTunnel no' \
      '    PermitTTY no' \
      'Match User sophon-restore' \
      '    AuthorizedKeysFile /etc/ssh/authorized_keys/sophon-restore' \
      '    ChrootDirectory /var/lib/sophon-backups' \
      '    ForceCommand internal-sftp -R' \
      '    PasswordAuthentication no' \
      '    KbdInteractiveAuthentication no' \
      '    AllowTcpForwarding no' \
      '    X11Forwarding no' \
      '    PermitTunnel no' \
      '    PermitTTY no' \
      'Match all' > /rootfs/etc/ssh/sshd_config.d/50-sophon-backup.conf; \
    chmod 0644 /rootfs/etc/ssh/sshd_config.d/*.conf; \
    printf '%s\n' \
      '#!/bin/sh' \
      'if [ ! -e /var/lib/sophon-backup-password-set ]; then' \
      '    /usr/local/sbin/sophon-backup-set-password || sleep 2' \
      'fi' \
      'exec /sbin/getty 38400 tty1' > /rootfs/usr/local/sbin/sophon-backup-tty1; \
    chmod 0755 /rootfs/usr/local/sbin/sophon-backup-tty1; \
    sed -i 's|^tty1::respawn:.*|tty1::respawn:/usr/local/sbin/sophon-backup-tty1|' /rootfs/etc/inittab; \
    grep -q '^ttyS0::' /rootfs/etc/inittab || echo 'ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100' >> /rootfs/etc/inittab; \
    printf '%s\n' \
      'Sophon Backup Server (\l)' \
      'Log in as admin, then run: sophon-backup-setup  (or sophon-authorize-key)' \
      '' > /rootfs/etc/issue; \
    printf '' > /rootfs/etc/motd; \
    install -d -m 0755 /rootfs/etc/profile.d; \
    printf '%s\n' \
      'if [ -t 1 ]; then' \
      '    _ip=$(ip -4 -o addr show scope global 2>/dev/null | awk "{split(\$4,a,\"/\"); print a[1]; exit}")' \
      '    printf "\n== Sophon Backup Server ==\n"' \
      '    [ -z "$_ip" ] || printf "  Address : %s (SFTP users: sophon-backup, sophon-restore [ro], path: /repository)\n" "$_ip"' \
      '    for _pub in /etc/ssh/ssh_host_*_key.pub; do' \
      '        [ -f "$_pub" ] || continue' \
      '        printf "  Hostkey : %s %s\n" "$(awk "{print \$1}" "$_pub")" "$(ssh-keygen -lf "$_pub" | awk "{print \$2}")"' \
      '    done' \
      '    printf "  Setup   : sophon-backup-setup    Pair key: sophon-authorize-key\n\n"' \
      '    unset _ip _pub' \
      'fi' > /rootfs/etc/profile.d/sophon-banner.sh; \
    chmod 0644 /rootfs/etc/profile.d/sophon-banner.sh; \
    for lvl_svc in \
      sysinit:devfs sysinit:dmesg sysinit:mdev sysinit:hwdrivers \
      boot:modules boot:hwclock boot:swap boot:seedrng boot:fsck boot:root \
      boot:localmount boot:sysctl boot:hostname boot:loopback boot:bootmisc boot:syslog \
      default:acpid default:dhcpcd default:chronyd default:sshd default:local \
      shutdown:mount-ro shutdown:killprocs shutdown:savecache; do \
        lvl=${lvl_svc%%:*}; svc=${lvl_svc#*:}; \
        mkdir -p "/rootfs/etc/runlevels/$lvl"; \
        ln -sf "/etc/init.d/$svc" "/rootfs/etc/runlevels/$lvl/$svc"; \
    done; \
    rm -f /rootfs/etc/ssh/ssh_host_*; \
    tar -C /rootfs --numeric-owner -czf /sophon-backup-os.tar.gz .

FROM docker.io/library/alpine:3.24.4
COPY --from=builder /sophon-backup-os.tar.gz /sophon-backup-os.tar.gz
