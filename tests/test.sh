#!/bin/bash
#
# Sophon role tester using Molecule
#

set -e

# Colors
red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
neutral='\033[0m'

echo -e "${green}Starting Sophon Molecule tests...${neutral}"

cd "$(dirname "$0")/.."

for f in ./roles/*; do
  if [[ -d "$f/molecule" ]]; then
    role_name=$(basename "$f")
    echo -e "${yellow}Testing role: ${role_name}${neutral}"
    pushd "$f" > /dev/null
      molecule -c ../../tests/molecule/base.yml test
    popd > /dev/null
    echo -e "${green}✓ ${role_name} passed${neutral}"
  fi
done

echo -e "${green}All Molecule tests passed!${neutral}"
