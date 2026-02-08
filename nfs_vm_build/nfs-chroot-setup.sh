#!/bin/sh
set -ex

# Enable core services
rc-update add qemu-guest-agent default
rc-update add rpcbind default
rc-update add nfs default
rc-update add networking boot
rc-update add hostname boot

# Create export mount point
mkdir -p /export
chmod 755 /export

# Setup default exports file
echo "/export *(rw,sync,no_subtree_check,no_root_squash)" > /etc/exports

# Configure hostname
echo "sophon-nfs" > /etc/hostname

# Configure network interface (DHCP initially, static via QGA later)
cat > /etc/network/interfaces <<'IFACE'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
IFACE
