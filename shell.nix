{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = with pkgs; [
    ansible
    apacheHttpd  # Provides htpasswd
  ];
}
