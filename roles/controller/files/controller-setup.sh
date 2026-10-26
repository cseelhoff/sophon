#!/bin/sh
# Execute INSIDE the Alpine rootfs, after copying this directory to the path below.
set -eu
source_dir=/usr/local/share/sophon-controller
mkdir -p /usr/local/bin /usr/local/sbin /var/lib/sophon /etc/sophon /export /media/sophon-deploy
install -m 755 "$source_dir/sophon-controller" /usr/local/bin/sophon-controller
install -m 755 "$source_dir/sophon-import" /usr/local/sbin/sophon-import
install -m 755 "$source_dir/sophon-import.initd" /etc/init.d/sophon-import
install -m 755 "$source_dir/sophon-podman.initd" /etc/init.d/sophon-podman
printf '%s\n' sophon-controller-v1 > /etc/sophon-controller-marker
printf '%s\n' sophon-controller > /etc/hostname
printf '%s\n' 'auto lo' 'iface lo inet loopback' '' 'auto eth0' 'iface eth0 inet dhcp' > /etc/network/interfaces
addgroup -g 1000 operator
adduser -D -u 1000 -G operator -s /bin/sh operator
# Root remains locked. The independently supplied password is set only over QGA.
passwd -l root
{
    printf 'permit nopass setenv { '
    tr '\n' ' ' < "$source_dir/service-env"
    printf '} operator as root cmd /usr/local/bin/sophon-controller\n'
} > /etc/doas.conf
chmod 600 /etc/doas.conf
mkdir -p /etc/containers /etc/ssh/sshd_config.d
cat > /etc/containers/storage.conf <<'EOF'
[storage]
driver = "overlay"
runroot = "/run/containers/storage"
graphroot = "/export/containers-storage"
EOF
cat > /etc/containers/containers.conf <<'EOF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
EOF
# Disable password SSH entirely; operator's password is for the VM console.
cat >> /etc/ssh/sshd_config <<'EOF'

PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
Match User kopia
    ForceCommand internal-sftp
    DisableForwarding yes
    PermitTTY no
Match all
EOF
rc-update add devfs sysinit
rc-update add procfs sysinit
rc-update add sysfs sysinit
rc-update add cgroups boot
rc-update add localmount boot
rc-update add hostname boot
rc-update add networking boot
rc-update add qemu-guest-agent default
rc-update add sshd default
rc-update add rpcbind default
rc-update add sophon-import default
# NFS and Podman readiness start only after /export has been safely mounted.
# The importer starts them on every boot, including boots with an already-ready disk.
