# Addresses are supplied by the operator, not discovered

`nfs_ip` and `infravm_ip` are inputs. Sophon prompts for them alongside
`proxmox_host` and `domain_name`, validates them, and persists them under
`artifacts/` per ADR-0008 so later runs do not ask again. It does not scan the
network to pick them.

## Why

The existing discovery is unsound in both directions. `roles/vnet/tasks/main.yml`
runs `apt-get update && apt-get install -y arp-scan` on the Proxmox node, which
requires internet during Deploy and contradicts ADR-0001. The command swallows
its own failure with `;` and `2>&1`, so when the install fails `arp-scan` is
simply absent, `_used_ips` comes back empty, and every address in the subnet
appears free.

Even working, ARP only sees hosts that are currently up. A powered-off VM, a
sleeping laptop, or an idle DHCP reservation all look available, and the logic
takes the first apparently-free address in `range(2, 255)`.

An operator provisioning a homelab knows which addresses are theirs to use.
Guessing on their behalf trades a genuine correctness problem for a small
amount of typing.

## Consequences

- The `apt-get` on the Proxmox node is removed, and with it the last thing in
  Deploy that needs internet on Proxmox.
- The `vnet` role narrows to deriving `vnet_gateway`, `vnet_cidr` and
  `vnet_dns` from the Proxmox API, and validating that the supplied addresses
  are inside the subnet and not already answering. A ping from the Proxmox node
  through `proxmox_shell` is enough for that, with nothing to install.
- Validation failure is fatal. Silently deploying onto an occupied address is
  worse than stopping.
