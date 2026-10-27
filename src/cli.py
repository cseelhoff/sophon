"""Container entry point: `docker run ... sophon-tools <command> [args]`.

Runs inside the tools image from the mounted project (/workspace), so the same
docker run command works from bash, PowerShell or cmd.
"""
import os
import sys

COMMANDS = {
    'prepare': lambda args: ['ansible-playbook', '1-prepare.yml', *args],
    'provision': lambda args: ['python3', 'src/deployment.py', '--step', 'provision', *args, '--', '2-provision.yml'],
    'certificates': lambda args: ['python3', 'src/certificates.py', *args],
    'test': lambda args: ['sh', '-c', 'python3 -m unittest discover -s tests "$@" && python3 tests/test_deployment.py '
                          '>/dev/null && python3 tests/test_steps.py', 'sh', *args],
    'shell': lambda args: ['bash', *args],
}

HELP = """usage: docker run --rm -it -v <project>:/workspace -v <engine socket>:/var/run/docker.sock \\
           -v sophon-state:/state sophon-tools <command> [args]

  prepare                 step 1: cache downloads, build runner image and deployment ISO
  provision               steps 1-2: create the blank InfraVM (settings via -e SOPHON_*)
  certificates issue ...  make the base64 certificate payload (--output FILE)
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
