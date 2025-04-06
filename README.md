# sophon

<img src="./sophon.drawio.svg">

Prereqs:
- Laptop (bootstrap)
  - Ansible
  - Internet connection for initial image downloads
- Hypervisor (QEMU or vCenter)

Running Deployment Scripts:
  1. (optional) `read -s PROXMOX_API_PASSWORD && export PROXMOX_API_PASSWORD`
  1. `cd ansible/`
  1. `ansible-playbook -i inventories/development coreos.yml`
  1. `ansible-playbook -i inventories/development portainer.yml`
  1. `ansible-playbook -i inventories/development coredns.yml`
