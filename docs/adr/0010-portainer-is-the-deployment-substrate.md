# Portainer is both a product feature and Sophon's deployment substrate

Portainer is the management UI Sophon delivers to the operator, and it is also
the only channel Ansible uses to act on InfraVM. Every stack is deployed
through `roles/portainer_stack`, and `roles/traefik/tasks/restore_acme.yml`
goes through Portainer's Docker-API proxy to create a helper container and push
a tar into a volume. There is no SSH path; `infravm_ssh_public_key` is baked
into Ignition and never used by Sophon.

This is deliberate. An API-only channel is why the repo needs no SSH plumbing,
no inventory of remote hosts, and no bastion.

## Consequences

- Portainer's own image cannot arrive through Portainer. That ordering
  constraint is the reason `sophon-init.service` exists and the reason
  artifacts stage onto NFS before InfraVM first boots (ADR-0005).
- Sophon requires Portainer Business Edition. `group_vars/all.yml` defaults to
  the `portainer-ee` image with an empty `portainer_license_key`, which is a
  configuration that appears to work and then does not. The licence must be an
  explicit requirement. A free Business Starter licence covers a homelab.
- Community Edition would deploy stacks correctly but silently lose the SSO
  group-to-admin mapping, which depends on `OAuthAutoMapTeamMemberships` and
  `AdminGroupClaimsRegexList` — both Business Edition features.
- Portainer owns reconciliation, not Ansible. Stack updates are
  `PUT /api/stacks/{id}?prune=true`.

## Considered options

- **Portainer as a product feature only, deploying via Podman Quadlets over
  SSH.** Removes the bootstrap ordering problem and the Business Edition
  coupling, but rewrites six roles and reintroduces SSH host management.
- **Community Edition.** Gives up SSO group mapping, which is most of the point
  of running Keycloak.
