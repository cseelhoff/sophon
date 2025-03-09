{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = with pkgs; [
    ansible
    python3Packages.proxmoxer
    apacheHttpd  # Provides htpasswd
  ];
}
