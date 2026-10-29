# Sophon's only tooling requirement is a container engine. This one file defines two images:
#   runner  Ansible and Python: the image InfraVM runs in step 4 (saved into the deployment ISO).
#   tools   runner + everything the laptop needs for steps 1-2 and certificates (the default target).
# Build it with:  docker build -t sophon-tools -f Containerfile .   (README). Versions are pinned; bump them here and rebuild.
# Nothing is copied from the build context, so .dockerignore excludes everything.

FROM docker.io/library/python:3.14.10-slim-trixie AS runner
RUN pip install --no-cache-dir ansible-core==2.21.5 cryptography==50.0.2 requests==2.34.2
WORKDIR /workspace

# Builds the official CoreOS installer so `prepare` can bake the unattended installer into the live ISO.
FROM docker.io/library/rust:1.99.1-slim-trixie AS coreos-installer
RUN apt-get update \
 && apt-get install -y --no-install-recommends pkg-config libssl-dev libzstd-dev libudev-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN cargo install coreos-installer --version 0.27.0 --locked --root /out

FROM runner AS tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl jq xorriso gdisk busybox-static libudev1 libzstd1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=coreos-installer /out/bin/coreos-installer /usr/local/bin/coreos-installer
RUN pip install --no-cache-dir proxmoxer==2.3.0 pypsrp==0.9.1 \
 && ansible-galaxy collection install community.proxmox:==2.1.0 -p /usr/share/ansible/collections
# docker drives the host engine through its socket; traefik issues certificates offline-capable.
COPY --from=docker.io/library/docker:29.9.2-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker.io/library/traefik:v3.7.16 /usr/local/bin/traefik /usr/local/bin/traefik
ARG GOVC_VERSION=0.56.2
ARG GOVC_SHA256=12889f30e42e868487041f6628e9789606c80e13a194fb37c099abffc92d5a59
RUN curl -fsSL -o /tmp/govc.tgz "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/govc_Linux_x86_64.tar.gz" \
 && echo "${GOVC_SHA256}  /tmp/govc.tgz" | sha256sum -c - \
 && tar -xzf /tmp/govc.tgz -C /usr/local/bin govc \
 && rm /tmp/govc.tgz
# Runs from the mounted project (-v <project>:/workspace); state lives in the sophon-state volume.
ENV SOPHON_STATE_DIR=/state
ENTRYPOINT ["python3", "src/cli.py"]
