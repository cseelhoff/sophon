# Controller requires direct network access to the vnet

The Ansible controller must have routed L3 access to the Proxmox vnet where the
NFS VM and InfraVM live — not just to the Proxmox API. This is a hard
precondition, verified early with an explicit failure, rather than something
Sophon works around.

## Considered options

- **Proxmox API only, tunnel VM-side work through the node.** Rejected: every
  stack deploy is a Portainer REST call and Keycloak provisioning is a REST
  call against the running service. Proxying HTTP through the QGA exec
  WebSocket, or through the Proxmox node as a jump host, adds a fragile
  transport to the hottest path in the deployment.
- **Probe reachability and branch.** Rejected for the same reason as ADR-0001 —
  the unreachable branch would never be exercised.

## Consequences

- The Cloudflare bootstrap tunnel exists only to bridge this gap and is
  therefore removed: the `portainer-bootstrap.<domain>` ingress rule, its CNAME,
  and the tunnel-hostname default for `infravm_portainer_url`.
- Sophon addresses InfraVM and the NFS VM by IP during Deploy. No name
  resolution is required for Sophon's own API calls.
- A VLAN-tagged or otherwise isolated vnet is an operator problem to solve
  before running `site.yml`, not a Sophon problem.
