import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from clash_guard.ip_quality import QualityError, QualitySampler, _assess, _http, _identity, validate_settings


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.node_file = self.root / 'nodes.json'
        self.nodes = [{'name': 'Example A', 'type': 'ss', 'server': 'example.com',
                       'port': 443, 'cipher': 'aes-128-gcm', 'password': 'private-example'},
                      {'name': 'Example B', 'type': 'ss', 'server': 'example.org',
                       'port': 443, 'cipher': 'aes-128-gcm', 'password': 'another-private-example'}]
        self.write_nodes()
        self.now = 1000000
        self.settings = {'enabled': True, 'node_config_path': str(self.node_file),
                         'core_path': '/example/mihomo'}
        self.hashes = {'chatgpt': 'a' * 64, 'claude': 'b' * 64}

    def tearDown(self):
        self.tmp.cleanup()

    def write_nodes(self):
        self.node_file.write_text(json.dumps({'proxies': self.nodes}), encoding='utf-8')

    def sampler(self, assessor, settings=None):
        return QualitySampler(settings or self.settings, self.root / 'state',
                              clock=lambda: self.now, assessor=assessor)

    def success(self, node, settings, observed):
        observed(dict(self.hashes))
        return {'score': 72, 'ip_hash_by_site': dict(self.hashes)}

    def test_success_stays_local_and_preserves_identity_privacy(self):
        sampler = self.sampler(self.success)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.assertEqual(sampler.scores()['Example A']['score'], 72)
        raw = sampler.path.read_text(encoding='utf-8')
        self.assertNotIn('private-example', raw)
        self.assertNotIn('example.com', raw)
        self.assertNotIn('8.8.8.8', raw)
        state = json.loads(raw)
        self.assertEqual(state['nodes']['Example A']['score_kind'], 'third_party_trust_score')

    def test_same_name_changed_node_invalidates_previous_score(self):
        sampler = self.sampler(self.success)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.nodes[0]['password'] = 'new-secret'
        self.write_nodes()
        self.assertEqual(sampler.scores(), {})
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.assertEqual(sampler.scores()['Example A']['score'], 72)

    def test_dynamic_loader_refreshes_new_nodes_and_changed_parameters(self):
        command = [sys.executable, '-c', 'import pathlib,sys;sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())', str(self.node_file)]
        settings = {'enabled': True, 'identity_command': command, 'core_path': '/example/mihomo'}
        sampler = self.sampler(self.success, settings)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.nodes[0]['server'] = 'new.example'
        self.nodes.append({'name': 'Example C', 'type': 'ss', 'server': 'c.example', 'port': 443, 'password': 'secret'})
        self.write_nodes()
        self.assertEqual(sampler.scores(), {})
        self.assertTrue(sampler.sample_blocking(['Example C']))
        self.assertEqual(sampler.scores()['Example C']['score'], 72)

    def test_yaml_subscription_source_when_pyyaml_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML is optional')
        self.node_file.write_text(yaml.safe_dump({'proxies': self.nodes}), encoding='utf-8')
        sampler = self.sampler(self.success)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.assertEqual(sampler.scores()['Example A']['score'], 72)

    def test_identity_excludes_name_but_binds_all_connection_parameters(self):
        one = dict(self.nodes[0], **{'client-fingerprint': 'firefox', 'dialer-proxy': 'relay',
                                    'interface-name': 'en0', 'routing-mark': 1})
        self.assertEqual(_identity(one), _identity(dict(one, name='other device name')))
        self.assertNotEqual(_identity(one), _identity(dict(one, **{'client-fingerprint': 'chrome'})))
        self.assertNotEqual(_identity(one), _identity(dict(one, **{'dialer-proxy': 'other relay'})))
        self.assertNotEqual(_identity(one), _identity(dict(one, **{'interface-name': 'en1'})))
        self.assertNotEqual(_identity(one), _identity(dict(one, **{'routing-mark': 2})))
        self.assertNotEqual(_identity(one), _identity(dict(one, password='changed-secret')))

    def test_node_change_during_sampling_never_commits_old_result(self):
        def changed(node, settings, observed):
            self.nodes[0]['port'] = 8443
            self.write_nodes()
            return self.success(node, settings, observed)
        sampler = self.sampler(changed)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.assertEqual(sampler.scores(), {})

    def test_exit_change_invalidates_old_score_before_lookup_finishes(self):
        sampler = self.sampler(self.success)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.now += 21600
        def changed(node, settings, observed):
            observed({'chatgpt': 'c' * 64, 'claude': 'b' * 64})
            self.assertEqual(sampler.scores(), {})
            raise QualityError('lookup_http_403')
        sampler.assessor = changed
        self.assertTrue(sampler.sample_blocking(['Example A']))
        self.assertEqual(sampler.scores(), {})
        entry = json.loads(sampler.path.read_text())['nodes']['Example A']
        self.assertEqual(entry['reason'], 'lookup_http_403')
        self.assertNotIn('score', entry)

    def test_transient_failures_backoff_and_never_create_zero_score(self):
        def failed(node, settings, observed):
            raise QualityError('request_failed', True)
        sampler = self.sampler(failed)
        for delay in (900, 3600, 21600):
            start = self.now
            self.assertTrue(sampler.sample_blocking(['Example A']))
            self.assertEqual(sampler.scores(), {})
            entry = json.loads(sampler.path.read_text())['nodes']['Example A']
            self.assertEqual(entry['next_due'], start + delay)
            self.assertNotIn('score', entry)
            self.assertFalse(sampler.sample_blocking(['Example A']))
            self.now += delay

    def test_freshness_expiry_and_changed_provider_are_unknown(self):
        sampler = self.sampler(self.success)
        self.assertTrue(sampler.sample_blocking(['Example A']))
        changed = dict(self.settings, lookup_base='https://different.example/lookup/')
        other = self.sampler(self.success, changed)
        self.assertEqual(other.scores(), {})
        self.assertTrue(other.sample_blocking(['Example A']))
        self.assertIn('Example A', other.scores())
        self.now += 86401
        self.assertEqual(other.scores(), {})

    def test_round_robin_recovers_multiple_nodes_without_repeat_until_due(self):
        sampled = []
        def assessor(node, settings, observed):
            sampled.append(node['name'])
            return self.success(node, settings, observed)
        sampler = self.sampler(assessor)
        self.assertTrue(sampler.sample_blocking(['Example A', 'Example B']))
        self.assertTrue(sampler.sample_blocking(['Example A', 'Example B']))
        self.assertFalse(sampler.sample_blocking(['Example A', 'Example B']))
        self.assertEqual(sampled, ['Example A', 'Example B'])

    def test_background_is_one_at_a_time_and_can_be_cleanly_joined(self):
        started, release = threading.Event(), threading.Event()
        def slow(node, settings, observed):
            started.set()
            release.wait(timeout=2)
            return self.success(node, settings, observed)
        sampler = self.sampler(slow)
        self.assertTrue(sampler.sample_async(['Example A', 'Example B']))
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(sampler.sample_async(['Example A', 'Example B']))
        release.set(); sampler._thread.join(); sampler.close()
        self.assertEqual(sampler.scores()['Example A']['score'], 72)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process check')
    def test_pause_reaps_core_then_allows_a_new_sample(self):
        from clash_guard.ip_quality import _isolated_core, _check_cancel
        fake_core = self.root / 'fake_core.py'
        pid_path = self.root / 'fake.pid'
        fake_core.write_text('#!' + sys.executable + '\n' + '''
import json, os, socket, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[sys.argv.index('-f') + 1]).read_text())
sock = socket.socket(); sock.bind(('127.0.0.1', config['mixed-port'])); sock.listen(1)
Path(os.environ['QUALITY_PAUSE_PID']).write_text(str(os.getpid()))
print('proxy listening at: 127.0.0.1:' + str(config['mixed-port']), flush=True)
while True: time.sleep(1)
''', encoding='utf-8')
        fake_core.chmod(0o700)
        started = threading.Event()
        def running(node, settings, observed):
            with _isolated_core(settings, node):
                started.set()
                while True:
                    _check_cancel(settings)
                    time.sleep(.02)
        with mock.patch.dict(os.environ, {'QUALITY_PAUSE_PID': str(pid_path)}):
            sampler = self.sampler(running, dict(self.settings, core_path=str(fake_core)))
            try:
                self.assertTrue(sampler.sample_async(['Example A']))
                self.assertTrue(started.wait(timeout=5))
                pid = int(pid_path.read_text())
                sampler.pause()
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                self.assertEqual(sampler.scores(), {})
                sampler.assessor = self.success
                self.assertTrue(sampler.sample_async(['Example A']))
                sampler._thread.join(timeout=5)
                self.assertEqual(sampler.scores()['Example A']['score'], 72)
                sampler.close()
                sampler.pause()
                self.assertFalse(sampler.sample_async(['Example B']))
            finally:
                sampler.close()

    @unittest.skipUnless(os.name == 'posix', 'POSIX signal semantics')
    def test_sigterm_graceful_shutdown_reaps_owned_core(self):
        fake_core = self.root / 'fake_mihomo.py'
        fake_core.write_text('#!' + sys.executable + '\n' + '''
import json, os, socket, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[sys.argv.index('-f') + 1]).read_text())
sock = socket.socket(); sock.bind(('127.0.0.1', config['mixed-port'])); sock.listen(1)
Path(os.environ['QUALITY_TEST_PID']).write_text(str(os.getpid()))
print('proxy listening at: 127.0.0.1:' + str(config['mixed-port']), flush=True)
while True: time.sleep(1)
''', encoding='utf-8')
        fake_core.chmod(0o700)
        ready, pid_path = self.root / 'ready', self.root / 'child.pid'
        script = '''
import os, signal, sys, time
from pathlib import Path
from clash_guard.ip_quality import QualitySampler, _isolated_core, _check_cancel
settings = {'enabled': True, 'node_config_path': sys.argv[1], 'core_path': sys.argv[2]}
def assessor(node, config, observed):
    with _isolated_core(config, node):
        Path(sys.argv[4]).write_text('ready')
        while True:
            _check_cancel(config)
            time.sleep(.02)
sampler = QualitySampler(settings, sys.argv[3], assessor=assessor)
def terminate(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, terminate)
try:
    sampler.sample_async(['Example A'])
    while True: time.sleep(.1)
except KeyboardInterrupt:
    pass
finally:
    sampler.close()
'''
        env = dict(os.environ, QUALITY_TEST_PID=str(pid_path))
        process = subprocess.Popen([sys.executable, '-c', script, str(self.node_file), str(fake_core),
                                    str(self.root / 'child-state'), str(ready)], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(.02)
            self.assertTrue(ready.exists(), 'isolated child did not become ready')
            child_pid = int(pid_path.read_text())
            os.kill(process.pid, signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            process.stderr.close()

    def test_actual_assessment_routes_both_traces_and_lookup_through_owned_core(self):
        requests = []
        @contextlib.contextmanager
        def fake_core(settings, node):
            self.assertEqual(node['name'], 'Example A')
            yield 'http://127.0.0.1:12345'
        def http(settings, proxy, url, deadline):
            self.assertEqual(proxy, 'http://127.0.0.1:12345')
            requests.append(url)
            if url == 'https://a.example/trace':
                return 200, b'ip=8.8.8.8\n'
            if url == 'https://b.example/trace':
                return 200, b'ip=2001:4860:4860::8888\n'
            if url.endswith('8.8.8.8'):
                return 200, b'{"trust_score": 92}'
            return 200, b'{"trust_score": 63}'
        settings = validate_settings(dict(self.settings, trace_urls={'a': 'https://a.example/trace',
                                                                     'b': 'https://b.example/trace'}))
        observations = []
        with mock.patch('clash_guard.ip_quality._isolated_core', fake_core), mock.patch('clash_guard.ip_quality._http', http):
            result = _assess(self.nodes[0], settings, observations.append)
        self.assertEqual(len(requests), 4)
        self.assertEqual(result['score'], 63)
        self.assertEqual(result['ip_hash_by_site']['a'], hashlib.sha256(b'8.8.8.8').hexdigest())
        self.assertNotEqual(result['ip_hash_by_site']['a'], result['ip_hash_by_site']['b'])
        self.assertEqual(observations, [result['ip_hash_by_site']])

    def test_403_missing_score_and_invalid_ip_remain_unknown(self):
        @contextlib.contextmanager
        def fake_core(settings, node):
            yield 'http://127.0.0.1:12345'
        settings = validate_settings(dict(self.settings, trace_urls={'a': 'https://a.example/trace'}))
        for lookup in ((403, b'{}'), (200, b'{}'), (200, b'{"trust_score":null}')):
            with self.subTest(lookup=lookup), mock.patch('clash_guard.ip_quality._isolated_core', fake_core), mock.patch('clash_guard.ip_quality._http', side_effect=[(200, b'ip=8.8.8.8\n'), lookup]):
                with self.assertRaises(QualityError):
                    _assess(self.nodes[0], settings, lambda _: None)
        with mock.patch('clash_guard.ip_quality._isolated_core', fake_core), mock.patch('clash_guard.ip_quality._http', return_value=(200, b'ip=127.0.0.1\n')):
            with self.assertRaises(QualityError):
                _assess(self.nodes[0], settings, lambda _: None)

    def test_curl_request_forces_owned_proxy_even_with_no_proxy_environment(self):
        if os.name != 'posix':
            self.skipTest('test helper uses a POSIX executable script')
        helper = self.root / 'curl.py'
        helper.write_text('#!' + sys.executable + '\n' + '''
import sys
config = sys.stdin.read()
assert 'noproxy = ""' in config
assert 'proxy = "http://127.0.0.1:12345"' in config
assert 'url = "https://test.example/trace"' in config
sys.stdout.write('ip=8.8.8.8\\n\\n__QUALITY_HTTP__200')
''', encoding='utf-8')
        helper.chmod(0o700)
        settings = validate_settings(dict(self.settings, curl_path=str(helper)))
        with mock.patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'}):
            status, body = _http(settings, 'http://127.0.0.1:12345', 'https://test.example/trace', time.monotonic() + 5)
        self.assertEqual((status, body), (200, b'ip=8.8.8.8\n'))

    def test_invalid_config_and_duplicate_node_are_not_sampled(self):
        for bad in ('http://example.com/', 'https://user:password@example.com/', 'https://example.com/?token=secret'):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                validate_settings(dict(self.settings, lookup_base=bad))
        self.nodes.append(dict(self.nodes[0]))
        self.write_nodes()
        self.assertFalse(self.sampler(self.success).sample_blocking(['Example A']))

    def test_disabled_sampler_has_no_filesystem_or_network_effect(self):
        sampler = QualitySampler({'enabled': False}, self.root / 'disabled')
        self.assertFalse(sampler.sample_async(['Example A']))
        self.assertEqual(sampler.scores(), {})
        self.assertFalse((self.root / 'disabled').exists())


if __name__ == '__main__':
    unittest.main()
