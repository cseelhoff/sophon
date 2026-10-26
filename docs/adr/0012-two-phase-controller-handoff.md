# ADR-0012: Bootstrap through management, then hand off to a site-local Controller

## Status

Superseded by [ADR-0013](0013-two-iso-infravm-installation.md).
The historical decision below superseded the single-controller assumptions of
[ADR-0002](0002-controller-requires-direct-vnet-access.md) and the bootstrap
artifact transport in [ADR-0005](0005-artifacts-reach-the-site-over-a-fuse-mounted-nfs-export.md).

## Context

The bootstrap laptop can reach Proxmox management without being able to reach
the VM network. Requiring NFS and Portainer access from that laptop forces
network-routing decisions unrelated to provisioning. A manual network change
halfway through one playbook also obscures which credentials belong where.

## Decision

Use two deployment phases, with connected Prestage before provisioning.

1. The laptop prepares the Alpine Controller disk, CoreOS disk, offline runner,
   Ansible collections, service images, and an allowlisted deployment ISO.
2. Through Proxmox management only, bootstrap creates a combined Controller/NFS
   VM and attaches the ISO. The VM verifies checksums and copies the bundle to
   persistent storage, loads the runner, and prepares its export.
3. Bootstrap waits for guest-agent readiness before creating and starting
   InfraVM. It does not wait for or configure the Portainer API.
4. The operator establishes access to the Controller VM by their chosen means.
   Sophon neither selects nor advertises a network route.
5. A Podman-hosted Python/Ansible runner deploys services locally. Its task graph
   contains no Proxmox, guest-agent, bridge-discovery, or VM-lifecycle operations.

The initial operator SSH public key and independent console password are
configured through the guest agent. The Proxmox password is not reused as a VM
password. Service passwords and Cloudflare tokens are supplied only after
handoff. The ISO contains no credentials or generated private keys.

The service domain is also a Phase 2 input: it is neither required by bootstrap
nor serialized into the ISO or InfraVM's initial configuration. The dashboard
starts without it. Before service work, the playbook validates the entered
domain and refreshes container-local service-name entries, and the Traefik
deployment configures Portainer's hostname route. Direct-IP Portainer API
access remains the domain-independent bootstrap mechanism.

New Controller deployments are supported first. Existing legacy NFS VMs and
unmarked conflicting VMs are preserved and rejected; no automatic migration,
replacement, or deletion is attempted.

## Consequences

The laptop needs management access only, and Phase 2 needs no Proxmox access.
The persistent Controller stores both deployment files and backup identity.
The operator remains responsible for access, service-name resolution, and
retaining passwords. Offline runner and collection preparation make Prestage
larger, while making the service phase independent of package downloads.
Existing deployments require an explicit future migration design.
