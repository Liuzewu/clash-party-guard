"""Optional local integration: set MIHOMO_BIN to a Mihomo binary to run."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get('MIHOMO_BIN'), 'set MIHOMO_BIN to run the isolated core check')
class IsolatedMihomoTest(unittest.TestCase):
    def test_read_only_with_real_core(self):
        binary = os.environ['MIHOMO_BIN']
        with tempfile.TemporaryDirectory(prefix='clash-guard-test-') as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            root = Path(directory)
            config = f'''mixed-port: 0
allow-lan: false
bind-address: 127.0.0.1
external-controller: 127.0.0.1:{port}
secret: isolated-test
mode: rule
ipv6: false
tun:
  enable: false
dns:
  enable: false
proxies:
  - name: Test Node
    type: ss
    server: 127.0.0.1
    port: 9
    cipher: aes-128-gcm
    password: isolated-test
proxy-groups:
  - name: Example Selector
    type: select
    proxies: [Test Node]
rules:
  - MATCH,DIRECT
'''
            (root / 'config.yaml').write_text(config, encoding='utf-8')
            (root / 'controller.json').write_text(json.dumps({'controller': f'http://127.0.0.1:{port}', 'secret_env': 'ISOLATED_CLASH_SECRET', 'group': 'Example Selector', 'targets': [{'url': 'https://one.example/', 'expected': '200'}, {'url': 'https://two.example/', 'expected': '200'}]}), encoding='utf-8')
            child = subprocess.Popen([binary, '-d', directory, '-f', str(root / 'config.yaml')], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                env = dict(os.environ, ISOLATED_CLASH_SECRET='isolated-test')
                for _ in range(40):
                    result = subprocess.run([sys.executable, '-m', 'clash_guard', '--config', str(root / 'controller.json'), '--read-only'], capture_output=True, text=True, env=env, timeout=3)
                    if result.returncode == 0 or child.poll() is not None:
                        break
                    time.sleep(.1)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['selected'], 'Test Node')
                self.assertEqual(report['candidates'], 1)
            finally:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
