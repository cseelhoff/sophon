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

        ansibleEnv = pkgs.python3.withPackages (pythonPackages: with pythonPackages; [
          ansible-core
          proxmoxer
          requests
          websocket-client
        ]);

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
            fuse.dev
            libnfs
          ];

          buildInputs = with pkgs; [
            fuse
            libnfs
          ];

          preConfigure = "./setup.sh";
        };

        sophonDevPackages = with pkgs; [
          ansibleEnv
          ansible-lint
          apacheHttpd
          butane
          skopeo
          dnf5
          qemu
          libguestfs-with-appliance
          proot
          unixtools.xxd
          nfs-utils
          fuse
          libnfs
          fuseNfs
          cloudflared
          gnumake
          go
          git
          cacert
          buildah
          mkpasswd
          dig
          bubblewrap
          socat
          tailscale
        ];

        sophonRunnerImagePackages = sophonDevPackages ++ (with pkgs; [
          bashInteractive
          coreutils
          findutils
          gawk
          gnugrep
          gnused
          gnutar
          gzip
          nix
          which
        ]);
      in
      {
        packages = {
          fuseNfs = fuseNfs;

          sophon-dev-env = pkgs.buildEnv {
            name = "sophon-dev-env";
            paths = sophonDevPackages;
          };

          sophon-runner-image = pkgs.dockerTools.buildLayeredImage {
            name = "sophon-nix-runner";
            tag = "latest";
            contents = sophonRunnerImagePackages;
            extraCommands = ''
              mkdir -p workspace tmp etc/nix
              chmod 1777 tmp
              cat > etc/nix/nix.conf <<'EOF'
              experimental-features = nix-command flakes
              sandbox = false
              filter-syscalls = false
              EOF
            '';
            config = {
              Cmd = [ "${pkgs.bashInteractive}/bin/bash" ];
              WorkingDir = "/workspace";
              Env = [
                "HOME=/tmp"
                "NIX_CONFIG=experimental-features = nix-command flakes"
                "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
                "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
                "PATH=${lib.makeBinPath sophonRunnerImagePackages}"
              ];
            };
          };
        };

        devShells.default = pkgs.mkShell {
          packages = sophonDevPackages;

          shellHook = ''
            if [ ! -d "$HOME/.ansible/collections/ansible_collections/community/proxmox" ]; then
              ansible-galaxy collection install community.proxmox community.general community.docker ansible.posix --force
            fi
          '';
        };
      });
}