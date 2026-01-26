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
        lib = nixpkgs.lib;

        ansibleEnv = pkgs.ansible.overrideAttrs (old: {
          propagatedBuildInputs = (old.propagatedBuildInputs or []) ++ [
            pkgs.python3Packages.proxmoxer
          ];
        });

        fuseNfs = pkgs.stdenv.mkDerivation rec {
          pname = "fuse-nfs";
          version = "unstable-2025-02-25";

          src = pkgs.fetchFromGitHub {
            owner = "sahlberg";
            repo = "fuse-nfs";
            rev = "75827244f1615be20da880cbc68665416131088d";
            sha256 = "sha256-QmsC0FLbSHko9Pfe6Nk2p1xyViUbqY6lCiGgn1J1KeA=";
          };

          nativeBuildInputs = with pkgs; [
            pkg-config
            autoconf
            automake
            libtool
            m4
            libxslt
            fuse.dev      # Critical: provides fuse.h and pkg-config info
            libnfs        # Provides headers and .pc file (no separate .dev output)
          ];

          buildInputs = with pkgs; [
            fuse          # Runtime library (libfuse.so.2)
            libnfs
          ];

          preConfigure = "./setup.sh";
        };
      in
      {
        packages.fuseNfs = fuseNfs;

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            ansibleEnv
            ansible-lint
            python3Packages.proxmoxer
            python3Packages.requests
            python3Packages.websocket-client
            apacheHttpd
            butane
            skopeo
            dnf5
            qemu
            libguestfs-with-appliance
            proot
            unixtools.xxd
            nfs-utils
            fuse          # Provides fusermount + runtime libfuse2
            libnfs
            fuseNfs
            cloudflared
          ];

          shellHook = ''
            # Install Ansible collections if not present
            if [ ! -d "$HOME/.ansible/collections/ansible_collections/community/proxmox" ]; then
              echo "Installing Ansible collections..."
              ansible-galaxy collection install community.proxmox community.general community.docker ansible.posix --force
            fi

            echo "fuse-nfs is available at $(which fuse-nfs)"
            echo "To unmount: fusermount -u ~/nfs   (or fusermount -z -u ~/nfs if busy)"
          '';
        };
      });
}