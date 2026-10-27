# Sophon's only tooling requirement is a container engine. This one file defines two images:
#   runner  Ansible and Python: the image InfraVM runs in step 4 (saved into the deployment ISO).
#   tools   runner + everything the laptop needs for steps 1-2 and certificates (the default target).
# Build it with:  docker build -t sophon-tools -f Containerfile .   (README). Versions are pinned; bump them here and rebuild.
# Nothing is copied from the build context, so .dockerignore excludes everything.

FROM docker.io/library/python:3.13-slim AS runner
RUN pip install --no-cache-dir ansible-core==2.20.0 cryptography==46.0.3 requests==2.32.5
WORKDIR /workspace

FROM runner AS tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl jq xorriso gdisk busybox-static \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir proxmoxer==2.2.0 pypsrp==0.9.1 \
 && ansible-galaxy collection install community.proxmox:==1.6.0 -p /usr/share/ansible/collections
# docker drives the host engine through its socket; traefik issues certificates offline-capable.
COPY --from=docker.io/library/docker:cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker.io/library/traefik:v3.2 /usr/local/bin/traefik /usr/local/bin/traefik
ARG GOVC_VERSION=0.52.0
ARG GOVC_SHA256=cd18ceff7418189ddf96851c410713cfc38f5b79560fdea831f1bfcc74d09c3f
RUN curl -fsSL -o /tmp/govc.tgz "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/govc_Linux_x86_64.tar.gz" \
 && echo "${GOVC_SHA256}  /tmp/govc.tgz" | sha256sum -c - \
 && tar -xzf /tmp/govc.tgz -C /usr/local/bin govc \
 && rm /tmp/govc.tgz
# Runs from the mounted project (-v <project>:/workspace); state lives in the sophon-state volume.
ENV SOPHON_STATE_DIR=/state
ENTRYPOINT ["python3", "src/cli.py"]
