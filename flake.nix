{
  description = "Sophon development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # Ansible with required collections
        ansibleEnv = pkgs.ansible.overrideAttrs (old: {
          propagatedBuildInputs = (old.propagatedBuildInputs or []) ++ [
            pkgs.python3Packages.proxmoxer
          ];
        });
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            ansibleEnv
            ansible-lint
            python3Packages.proxmoxer
            python3Packages.requests        # Required by proxmoxer
            python3Packages.websocket-client  # Required by proxmox_shell module
            apacheHttpd
            butane    # Convert Butane YAML to Ignition JSON
            skopeo    # Download container images for NFS content prep
            dnf5      # Download RPM packages with dependency resolution
            qemu      # qemu-img for building Alpine NFS qcow2 images
            libguestfs-with-appliance  # Build VM images without root (guestfish)
            proot     # User-space chroot/root emulation via ptrace (works with musl)
            unixtools.xxd # Hex dump utility for debugging
          ];

          shellHook = ''
            # Install Ansible collections if not present
            if [ ! -d "$HOME/.ansible/collections/ansible_collections/community/proxmox" ]; then
              echo "Installing Ansible collections..."
              ansible-galaxy collection install community.proxmox community.general community.docker ansible.posix --force
            fi
          '';
        };
      });
}
