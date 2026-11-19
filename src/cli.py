"""Container entry point: `docker run ... sophon-tools <command> [args]`.

Runs inside the tools image from the mounted project (/workspace), so the same
docker run command works from bash, PowerShell or cmd.
"""
import os
import sys

COMMANDS = {
    'prepare': lambda args: ['ansible-playbook', '1-prepare.yml', *args],
    'secrets': lambda args: ['python3', 'src/secrets_iso.py', *args],
    'provision-backup': lambda args: ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '2-provision-backup.yml'],
    'provision': lambda args: ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '2-provision.yml'],
    'destroy-vm': lambda args: ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '3-destroy-vm.yml'],
    'verify-restore': lambda args: ['python3', 'src/verify_restore.py', *args],
    'certificates': lambda args: ['python3', 'src/certificates.py', *args],
    # </dev/null: the wizard tests assume no terminal, and `docker run -it` would make them wait for keystrokes.
    'test': lambda args: ['sh', '-c', 'exec </dev/null; python3 -m unittest discover -s tests "$@" && '
                          'python3 tests/test_deployment.py >/dev/null && python3 tests/test_steps.py', 'sh', *args],
    'shell': lambda args: ['bash', *args],
}

HELP = """usage: docker run --rm -it -v <project>:/workspace -v <engine socket>:/var/run/docker.sock \\
           -v sophon-state:/state sophon-tools <command> [args]

  prepare                 step 1: cache downloads and build installer, deployment, and backup appliance ISOs
  secrets                 optional: build mode-0600 unattended seed ISOs (sophon-secrets.iso & sophon-backup-secrets.iso)
  provision-backup        optional: create the standalone Alpine SFTP Backup Server VM (Proxmox, vCenter, or Hyper-V)
  provision               steps 1-2: create InfraVM and auto-attach sophon-secrets.iso when present
  destroy-vm              stop and delete a Sophon-managed VM across Proxmox, vCenter, or Hyper-V
  verify-restore          step 7: hypervisor-agnostic restore drill using sophon-secrets.iso (safe standby mode)
  certificates issue ...  air-gapped sites: issue the wildcard certificate and write artifacts/secrets/sophon-certificates.iso
  test | shell            development helpers
Anything else runs as a command in the container."""


def main(argv):
    if not argv or argv[0] in ('help', '-h', '--help'):
        print(HELP)
        return 0
    command = COMMANDS[argv[0]](argv[1:]) if argv[0] in COMMANDS else argv
    os.execvp(command[0], command)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
