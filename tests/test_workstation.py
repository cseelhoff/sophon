import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'library'))
from workstation_vm import request_host


class WorkstationBridgeTests(unittest.TestCase):
    def test_host_request_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            parameters = {
                'name': 'sophon-infravm', 'cores': 8, 'memory': 8192, 'disk_gb': 100,
                'network': 'bridged', 'coreos_iso': '/workspace/artifacts/coreos.iso',
                'bundle_iso': '/workspace/artifacts/deployment.iso',
            }

            def respond():
                request_dir = state / 'workstation' / 'requests'
                while not list(request_dir.glob('*.json')):
                    time.sleep(0.01)
                request_path = next(request_dir.glob('*.json'))
                request = json.loads(request_path.read_text(encoding='utf-8'))
                self.assertEqual(request, {
                    'id': request_path.stem, 'action': 'provision',
                    'name': 'sophon-infravm', 'cores': 8, 'memory': 8192,
                    'disk_gb': 100, 'network': 'bridged',
                    'coreos_iso': 'artifacts/coreos.iso',
                    'bundle_iso': 'artifacts/deployment.iso',
                })
                response_path = state / 'workstation' / 'responses' / request_path.name
                response_path.write_text('\ufeff{"ok":true,"changed":true,"created":true}', encoding='utf-8')

            worker = threading.Thread(target=respond)
            worker.start()
            result = request_host('provision', parameters, temporary, timeout=5)
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, {'ok': True, 'changed': True, 'created': True})
            self.assertFalse(list((state / 'workstation').rglob('*.json')))