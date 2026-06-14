FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

ARG USERNAME=vscode

ENV NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV PATH=/home/${USERNAME}/.nix-profile/bin:${PATH}
ENV NIX_CONFIG="experimental-features = nix-command flakes"

RUN apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -m 0755 /nix \
    && chown ${USERNAME}:${USERNAME} /nix

COPY .devcontainer/bin/sophon-tailscale /usr/local/bin/sophon-tailscale

RUN chmod 0755 /usr/local/bin/sophon-tailscale

USER ${USERNAME}

RUN curl --fail --location https://nixos.org/nix/install | sh -s -- --no-daemon \
    && mkdir -p /home/${USERNAME}/.config/nix \
    && printf '%s\n' \
        'experimental-features = nix-command flakes' \
        'sandbox = false' \
        'filter-syscalls = false' \
        > /home/${USERNAME}/.config/nix/nix.conf

COPY --chown=${USERNAME}:${USERNAME} flake.nix flake.lock /tmp/sophon-flake/

WORKDIR /tmp/sophon-flake

RUN nix profile add .#sophon-dev-env \
    && ansible-galaxy collection install community.proxmox community.general community.docker ansible.posix --force \
    && rm -rf /tmp/sophon-flake

WORKDIR /workspaces/sophon
