from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from test_deployment import ROOT, run_fixture


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required')
class PortainerSetupTests(unittest.TestCase):
    def test_setup_token_is_required_only_for_initial_admin_and_sent_as_a_header(self):
        requests = []
        initialized = False

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/api/users/admin/check':
                    self.respond(204 if initialized else 404, {})
                elif self.path == '/api/endpoints':
                    self.respond(200, [{'Id': 1, 'Type': 1}])
                else:
                    self.respond(200, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('X-Setup-Token'), body))
                self.respond(200, {'jwt': 'fixture-jwt'})

            def respond(self, status, value):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if status != 204:
                    self.wfile.write(json.dumps(value).encode())

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory() as temporary:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                for initialized, token, succeeds in (
                        (False, 'FIXTURE_ONE_TIME_TOKEN', True), (False, '', False), (True, '', True)):
                    with self.subTest(initialized=initialized, token_supplied=bool(token)):
                        requests.clear()
                        result = run_fixture(Path(temporary), [{
                            'ansible.builtin.include_role': {'name': str(ROOT / 'roles/portainer')},
                            'vars': {
                                'infravm_portainer_url': f'http://127.0.0.1:{server.server_port}',
                                'portainer_admin_password': 'Fixture-password-only',
                                'portainer_setup_token': token,
                                'portainer_license_key': '',
                            },
                        }])
                        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
                        setup = [request for request in requests if request[0] == '/api/users/admin/init']
                        self.assertEqual(len(setup), int(succeeds and not initialized))
                        if setup:
                            self.assertEqual(setup[0][1], token)
                            self.assertNotIn(token, result.stdout + result.stderr)
                        if not succeeds:
                            self.assertIn('one-time setup token', result.stdout)
            finally:
                server.shutdown()
                worker.join()


if __name__ == '__main__':
    unittest.main()
