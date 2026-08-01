# Sophon Prestaging Runbook

Prestage is the only phase that touches the internet. It pulls container
images, builds the CoreDNS Docker Discovery image, and builds the NFS VM disk,
writing everything into `artifacts/`.

**Run it every time, including when the target site has full internet access.**
Deploy fetches nothing — see
[ADR-0001](docs/adr/0001-air-gapped-is-the-only-deployment-mode.md).

This repo intentionally ignores `artifacts/`. The directory is a local cache
and secret/artifact staging area, not source. Run these commands from the repo
root.

## Enter the Tool Shell

```bash
nix develop
```

The shell provides Ansible, Podman/Buildah-related tools, `guestfish`,
`qemu-img`, `proot`, `butane`, and the other utilities used below.

## 1. Run the Prestage Playbook

This runs the checked-in Ansible prestage playbook. It pulls standard container
images, builds the CoreDNS Docker Discovery image tar, and builds the NFS VM
qcow2 cache.

```bash
ansible-playbook prestage.yml
```

Expected output includes at least these files, depending on variable overrides
such as `portainer_image_repo` and `portainer_image_tag`:

```text
artifacts/containers/portainer-portainer-ee_2.41.0.tar
artifacts/containers/library-traefik_v3.2.tar
artifacts/containers/gitea-gitea_1.22.tar
artifacts/containers/library-postgres_16-alpine.tar
artifacts/containers/kopia-kopia_0.21.1.tar
artifacts/containers/osixia-openldap_1.5.0.tar
artifacts/containers/osixia-phpldapadmin_0.9.0.tar
artifacts/containers/cloudflare-cloudflared_latest.tar
artifacts/containers/coredns-dockerdiscovery.tar
artifacts/nfs-vm-build/sophon-nfs-alpine.qcow2
```

## 2. Manual CoreDNS Docker Discovery Build

The playbook already performs this build. Use these commands only when you want
to rebuild the artifact by hand.

The CoreDNS role expects this exact local artifact:

```text
artifacts/containers/coredns-dockerdiscovery.tar
```

Build it from `github.com/cseelhoff-ms/coredns-dockerdiscovery`:

```bash
mkdir -p artifacts/containers

COREDNS_SRC="${XDG_CACHE_HOME:-$HOME/.cache}/sophon/coredns-dockerdiscovery"

if [ ! -d "$COREDNS_SRC/.git" ]; then
  git clone https://github.com/cseelhoff-ms/coredns-dockerdiscovery.git "$COREDNS_SRC"
else
  git -C "$COREDNS_SRC" pull --ff-only
fi

podman build -t coredns-dockerdiscovery:latest "$COREDNS_SRC"
podman save -o artifacts/containers/coredns-dockerdiscovery.tar coredns-dockerdiscovery:latest
```

The upstream Dockerfile builds CoreDNS with the `docker` plugin registered and
packages the runtime image as `coredns-dockerdiscovery:latest`, which matches the
image name used by `roles/coredns/templates/docker-compose.yml.j2`.

## 3. Manual NFS VM Image Build

The playbook already performs this build. Use these commands only when you want
to rebuild the artifact by hand or debug the image builder.

The NFS role consumes this cache path:

```text
artifacts/nfs-vm-build/sophon-nfs-alpine.qcow2
```

The repo has a standalone builder under `nfs_vm_build/`. The currently active
NFS role also requires `openssh` in the image so Kopia can provision an SFTP
backend, so the command below writes a chroot setup script that mirrors the
current role behavior.

```bash
mkdir -p artifacts/nfs-vm-build

cp nfs_vm_build/build-alpine-image artifacts/nfs-vm-build/build-alpine-image
chmod +x artifacts/nfs-vm-build/build-alpine-image

cat > artifacts/nfs-vm-build/nfs-chroot-setup.sh <<'EOF'
#!/bin/sh
set -ex

rc-update add qemu-guest-agent default
rc-update add rpcbind default
rc-update add nfs default
rc-update add sshd default
rc-update add networking boot
rc-update add hostname boot

mkdir -p /export
chmod 755 /export

echo "/export *(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=100)" > /etc/exports
echo "sophon-nfs" > /etc/hostname

cat > /etc/network/interfaces <<'IFACE'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
IFACE
EOF

chmod +x artifacts/nfs-vm-build/nfs-chroot-setup.sh

LIBGUESTFS_BACKEND=direct \
artifacts/nfs-vm-build/build-alpine-image \
  --image-size 10G \
  --image-format qcow2 \
  --branch v3.21 \
  --packages "qemu-guest-agent nfs-utils openssh parted e2fsprogs openrc util-linux" \
  --script-chroot artifacts/nfs-vm-build/nfs-chroot-setup.sh \
  artifacts/nfs-vm-build/sophon-nfs-alpine.qcow2
```

You can also skip this manual build if you are running `site.yml` online. The
NFS role will build the same cache automatically when Proxmox does not already
have the `sophon-nfs-alpine.qcow2` image.

## 4. How Artifacts Reach the Site

You do not copy anything by hand. During `site.yml`, the `nfs` role mounts the
NFS export on the Controller with `fuse-nfs` (unprivileged, vendored in
`flake.nix`) and copies every tar from `artifacts/containers/` into
`/export/containers`.

On first boot, InfraVM's `sophon-init.service` runs `podman load` over every
tar under `/var/mnt/nfs/containers` before any stack starts, so compose files
resolve their images locally.

This means the Controller needs routed access to `nfs_ip`. If the fuse mount
fails, the deploy should stop there — a missing tar surfaces several roles
later as an unexplained image pull. See
[ADR-0005](docs/adr/0005-artifacts-reach-the-site-over-a-fuse-mounted-nfs-export.md).

> Not all of this is implemented yet. See
> [docs/known-gaps.md](docs/known-gaps.md).

## 5. Deployment-Generated Kopia Artifacts

These artifacts are generated only after the NFS VM exists, because the role
creates a matching SFTP user on that VM and records its SSH host key:

```text
artifacts/kopia/id_ed25519
artifacts/kopia/id_ed25519.pub
artifacts/kopia/known_hosts
```

For the default NFS-backed Kopia setup, run the normal deployment. Include the
usual variables for your environment:

```bash
ansible-playbook site.yml \
  -e proxmox_host=<proxmox-host> \
  -e proxmox_password=<proxmox-password> \
  -e domain_name=<domain> \
  -e nfs_ip=<nfs-vm-ip> \
  -e infravm_ip=<infravm-ip>
```

If you use a different SFTP backend, set `nfs_provision_kopia_user=false` and
create `artifacts/kopia/id_ed25519` plus `artifacts/kopia/known_hosts` yourself
before running the Kopia role.

## 6. Deployment-Generated Traefik ACME Artifact

Traefik writes this secret backup after certificates have actually been issued:

```text
artifacts/traefik/acme.json
```

The Traefik role backs it up after deploy when `traefik_acme_backup_after_deploy`
is true. To force an on-demand snapshot after Traefik has issued certs, run:

```bash
ansible-playbook traefik-backup-acme.yml \
  -e domain_name=<domain> \
  -e infravm_ip=<infravm-ip> \
  -e portainer_admin_password=<portainer-admin-password>
```

Treat `artifacts/traefik/acme.json` as a secret. It contains ACME account and
certificate private keys.

## 7. Optional Verification

```bash
find artifacts -maxdepth 3 -type f -print | sort

test -f artifacts/containers/coredns-dockerdiscovery.tar
test -f artifacts/nfs-vm-build/sophon-nfs-alpine.qcow2
```

After deployment has provisioned Kopia and Traefik has issued certs, these should
also exist:

```bash
test -f artifacts/kopia/id_ed25519
test -f artifacts/kopia/known_hosts
test -f artifacts/traefik/acme.json
```
