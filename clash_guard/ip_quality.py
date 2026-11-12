"""Optional, isolated per-node exit-IP assessment.

The score is an estimate from an external service, not an official service
rating. Raw addresses and node settings stay out of the evidence cache.
"""
import contextlib
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
from .identity import load_nodes


TRACES = {'chatgpt': 'https://chatgpt.com/cdn-cgi/trace',
          'claude': 'https://claude.ai/cdn-cgi/trace'}
LOOKUP = 'https://ip.net.coffee/api/ip/lookup/'
MAX_RESPONSE = 256 * 1024
RETRY_DELAYS = (900, 3600, 21600)


class QualityError(Exception):
    def __init__(self, reason, transient=False):
        super().__init__(reason)
        self.reason, self.transient = reason, transient


def _number(value, low, high):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and low <= value <= high)


def _https(value):
    if not isinstance(value, str) or '\n' in value or '\r' in value:
        raise ValueError('an HTTPS URL is required')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.query):
        raise ValueError('an HTTPS URL without credentials or query is required')
    return value


def validate_settings(settings):
    if not isinstance(settings, dict) or type(settings.get('enabled', False)) is not bool:
        raise ValueError('ip_quality must be an object with a boolean enabled flag')
    result = dict(settings)
    if not result.get('enabled', False):
        return result
    if not isinstance(result.get('core_path'), str) or not result['core_path'].strip():
        raise ValueError('ip_quality requires core_path')
    path, command = result.get('node_config_path'), result.get('identity_command')
    if bool(path) == bool(command):
        raise ValueError('ip_quality requires exactly one node_config_path or identity_command')
    if path and (not isinstance(path, str) or not path.strip()):
        raise ValueError('invalid node_config_path')
    if command and (not isinstance(command, list) or any(not isinstance(value, str) or not value for value in command)):
        raise ValueError('identity_command must be a nonempty argument list')
    result['lookup_base'] = _https(result.get('lookup_base', LOOKUP)).rstrip('/') + '/'
    traces = result.get('trace_urls', TRACES)
    if not isinstance(traces, dict) or not traces or len(traces) > 4:
        raise ValueError('trace_urls must contain one to four service URLs')
    if any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', key) for key in traces):
        raise ValueError('invalid trace service name')
    result['trace_urls'] = {key: _https(value) for key, value in traces.items()}
    for key, default, lower, upper in (('interval_seconds', 21600, 900, 604800),
                                        ('quality_max_age', 86400, 900, 604800),
                                        ('request_timeout_seconds', 8, 1, 20)):
        value = result.get(key, default)
        if not _number(value, lower, upper):
            raise ValueError('invalid ip_quality ' + key)
        result[key] = value
    curl = result.get('curl_path')
    if curl is not None and (not isinstance(curl, str) or not curl):
        raise ValueError('invalid curl_path')
    return result


def _nodes(settings):
    try:
        nodes = load_nodes(settings)
    except Exception:
        # Never expose loader output or private subscription data in errors.
        raise QualityError('node_config_unavailable') from None
    found = {}
    duplicates = set()
    for node in nodes:
        if isinstance(node, dict) and isinstance(node.get('name'), str) and node['name']:
            if node['name'] in found:
                duplicates.add(node['name'])
            found[node['name']] = node
    for name in duplicates:
        del found[name]
    return found


def _identity(node):
    # Names vary by device; all other fields, including credentials, stay bound.
    params = {key: value for key, value in node.items() if key != 'name'}
    return hashlib.sha256(json.dumps(params, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _node_allowed(node):
    if (not isinstance(node, dict) or not isinstance(node.get('name'), str)
            or not isinstance(node.get('server'), str) or not node['server']
            or str(node.get('type', '')).lower() in
            ('', 'direct', 'reject', 'select', 'url-test', 'fallback', 'relay', 'load-balance', 'smart')
            or node.get('dialer-proxy') or node.get('plugin')):
        raise QualityError('node_not_reproducible')
    server = node['server'].strip('[]')
    try:
        address = ipaddress.ip_address(server)
        if address.is_loopback or address.is_unspecified:
            raise QualityError('node_not_reproducible')
    except ValueError:
        if server.lower() == 'localhost':
            raise QualityError('node_not_reproducible')


def _check_cancel(settings):
    event = settings.get('_cancel_event')
    if event is not None and event.is_set():
        raise QualityError('cancelled')


class _Capture:
    def __init__(self, argv, limit, input_bytes=None, merge_stderr=False):
        self.data, self.limit, self.overflow = bytearray(), limit, False
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        if input_bytes is not None:
            try:
                self.process.stdin.write(input_bytes)
                self.process.stdin.close()
            except OSError:
                self.stop()
                raise QualityError('child_input_failed', True) from None

    def _read(self):
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), min(8192, self.limit - len(self.data) + 1))
                if not chunk:
                    return
                remaining = self.limit - len(self.data)
                self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.overflow = True
                    self.process.kill()
                    return
        except (OSError, ValueError):
            return

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.thread.join(timeout=1)
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.stdout.close()


def _http(settings, proxy, url, deadline):
    _check_cancel(settings)
    _https(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise QualityError('deadline_exceeded', True)
    curl = settings.get('curl_path') or shutil.which('curl')
    if not curl:
        raise QualityError('curl_missing')
    timeout = min(settings['request_timeout_seconds'], remaining)
    options = ['url = ' + json.dumps(url), 'proxy = ' + json.dumps(proxy), 'noproxy = ""',
               'silent', 'request = "GET"', 'proto = "=https"', 'max-redirs = 0',
               'max-time = ' + str(timeout), 'connect-timeout = ' + str(min(3, timeout)),
               'max-filesize = ' + str(MAX_RESPONSE), 'write-out = "\\n__QUALITY_HTTP__%{http_code}"']
    child = _Capture([curl, '-q', '--config', '-'], MAX_RESPONSE + 128,
                     ('\n'.join(options) + '\n').encode('utf-8'))
    try:
        while child.process.poll() is None:
            _check_cancel(settings)
            if child.overflow:
                raise QualityError('response_too_large')
            if time.monotonic() >= deadline:
                raise QualityError('deadline_exceeded', True)
            time.sleep(.02)
        child.thread.join(timeout=1)
        if child.overflow:
            raise QualityError('response_too_large')
        if child.process.returncode:
            raise QualityError('request_failed', True)
        try:
            body, code = bytes(child.data).rsplit(b'\n__QUALITY_HTTP__', 1)
            if len(body) > MAX_RESPONSE:
                raise QualityError('response_too_large')
            return int(code.strip()), body
        except (TypeError, ValueError):
            raise QualityError('invalid_http_response') from None
    finally:
        child.stop()


@contextlib.contextmanager
def _isolated_core(settings, node):
    _check_cancel(settings)
    _node_allowed(node)
    with tempfile.TemporaryDirectory(prefix='clash-guard-quality-') as directory:
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        group = 'QualitySampler'
        config = {'mode': 'rule', 'allow-lan': False, 'bind-address': '127.0.0.1', 'ipv6': True,
                  'log-level': 'info', 'mixed-port': port, 'port': 0, 'socks-port': 0,
                  'redir-port': 0, 'tproxy-port': 0, 'tun': {'enable': False}, 'dns': {'enable': False},
                  'external-controller': '', 'external-controller-unix': '', 'external-controller-pipe': '',
                  'profile': {'store-selected': False}, 'proxies': [node],
                  'proxy-groups': [{'name': group, 'type': 'select', 'proxies': [node['name']], 'interval': 0}],
                  'rules': ['MATCH,' + group]}
        target = Path(directory) / 'config.yaml'
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(config, stream, ensure_ascii=False)
        try:
            child = _Capture([str(Path(settings['core_path']).expanduser()), '-d', directory, '-f', str(target)],
                             64 * 1024, merge_stderr=True)
        except OSError:
            raise QualityError('core_start_failed', True) from None
        try:
            until = time.monotonic() + 5
            marker = ('proxy listening at: 127.0.0.1:' + str(port)).encode()
            while time.monotonic() < until:
                _check_cancel(settings)
                if child.process.poll() is not None or child.overflow:
                    raise QualityError('core_exited', True)
                if marker in bytes(child.data):
                    break
                time.sleep(.03)
            else:
                raise QualityError('owned_listener_unproven', True)
            yield 'http://127.0.0.1:' + str(port)
            _check_cancel(settings)
            if child.process.poll() is not None or child.overflow:
                raise QualityError('core_exited', True)
        finally:
            child.stop()


def _assess(node, settings, observed):
    """Read each service's own exit through an isolated node and assess each IP."""
    _node_allowed(node)
    ips, hashes, scores, lookup_cache = {}, {}, {}, {}
    deadline = time.monotonic() + 55
    with _isolated_core(settings, node) as proxy:
        for site, url in settings['trace_urls'].items():
            status, body = _http(settings, proxy, url, deadline)
            if status != 200:
                raise QualityError('trace_http_' + str(status), status in (408, 429, 500, 502, 503, 504))
            try:
                match = re.search(r'^ip=([^\s]+)\s*$', body.decode('utf-8', errors='strict'), re.M)
                if match is None:
                    raise ValueError('missing IP')
                ip = ipaddress.ip_address(match.group(1))
                if not ip.is_global:
                    raise ValueError('not a public IP')
            except (ValueError, UnicodeError):
                raise QualityError('invalid_trace') from None
            ips[site] = ip.compressed
            hashes[site] = hashlib.sha256(ip.compressed.encode('ascii')).hexdigest()
        observed(hashes)
        for site, address in ips.items():
            if address not in lookup_cache:
                status, body = _http(settings, proxy, settings['lookup_base'] + quote(address, safe=''), deadline)
                if status != 200:
                    raise QualityError('lookup_http_' + str(status), status in (408, 429, 500, 502, 503, 504))
                try:
                    value = json.loads(body)
                    score = value['trust_score']
                except (ValueError, UnicodeError, KeyError, TypeError):
                    raise QualityError('score_unavailable') from None
                if not _number(score, 0, 100):
                    raise QualityError('score_unavailable')
                lookup_cache[address] = float(score)
            scores[site] = lookup_cache[address]
    return {'score': min(scores.values()), 'ip_hash_by_site': hashes,
            'score_kind': 'third_party_trust_score', 'aggregation': 'minimum_across_services',
            'assessed_services': list(settings['trace_urls'])}


@contextlib.contextmanager
def _file_lock(path):
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise QualityError('sampler_busy') from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise QualityError('sampler_busy') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class QualitySampler:
    """Bounded background sampler; use scores() before sample_async(names)."""
    def __init__(self, settings, state_dir, *, clock=None, assessor=None):
        self.settings = validate_settings(settings)
        self.enabled = self.settings.get('enabled', False)
        self.directory = Path(state_dir).expanduser()
        self.path = self.directory / 'ip-quality.json'
        self.clock = clock or time.time
        self.assessor = assessor or _assess
        self._cancel = threading.Event()
        self._closed = False
        self.settings['_cancel_event'] = self._cancel
        if self.enabled:
            policy = {'trace_urls': self.settings['trace_urls'], 'lookup_base': self.settings['lookup_base']}
            self.policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self._thread = None
        self._thread_lock = threading.Lock()
        self._result_lock = threading.Lock()
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _read(self):
        if not self.path.exists():
            return {'version': 1, 'nodes': {}, 'cursor': 0}
        with self.path.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise QualityError('state_too_large')
        try:
            state = json.loads(raw)
        except (ValueError, UnicodeError):
            raise QualityError('state_invalid') from None
        if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('nodes'), dict):
            raise QualityError('state_invalid')
        return state

    def _write(self, state):
        raw = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(raw) > 1024 * 1024:
            raise QualityError('state_too_large')
        fd, temporary = tempfile.mkstemp(prefix='.quality-', dir=str(self.directory))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def scores(self):
        if not self.enabled:
            return {}
        try:
            nodes = _nodes(self.settings)
            with _file_lock(self.directory / 'ip-quality.lock'):
                state = self._read()
            now, result = self.clock(), {}
            for name, entry in state['nodes'].items():
                node = nodes.get(name)
                if not isinstance(entry, dict) or node is None:
                    continue
                try:
                    matching = (entry.get('node_hash') == _identity(node)
                                and entry.get('policy_hash') == self.policy_hash)
                    age = now - entry['observed_at']
                    if (matching and entry.get('status') == 'ok' and _number(entry.get('score'), 0, 100)
                            and _number(age, 0, self.settings['quality_max_age'])):
                        result[name] = {'score': entry['score'], 'observed_at': entry['observed_at']}
                except (ValueError, TypeError, KeyError):
                    continue
            return result
        except (OSError, ValueError, QualityError):
            return {}

    def _reserve(self, names):
        nodes = _nodes(self.settings)
        names = list(dict.fromkeys(name for name in names if isinstance(name, str) and name in nodes))[:512]
        if not names:
            return None
        with _file_lock(self.directory / 'ip-quality.lock'):
            state = self._read(); now = self.clock()
            cursor = state.get('cursor', 0)
            cursor = cursor if type(cursor) is int and cursor >= 0 else 0
            for offset in range(len(names)):
                index = (cursor + offset) % len(names); name = names[index]; node = nodes[name]
                identity = _identity(node); entry = state['nodes'].get(name, {})
                if not isinstance(entry, dict):
                    entry = {}
                if entry.get('node_hash') != identity or entry.get('policy_hash') != self.policy_hash:
                    entry = {'node_hash': identity, 'policy_hash': self.policy_hash,
                             'status': 'unknown', 'failures': 0}
                next_due = entry.get('next_due', 0)
                if not _number(next_due, 0, 1e12) or next_due > now + 7 * 86400:
                    next_due = now
                if now < next_due:
                    continue
                entry.update(node_hash=identity, status=entry.get('status', 'unknown'), next_due=now + 70)
                state['nodes'][name] = entry; state['cursor'] = (index + 1) % len(names)
                if len(state['nodes']) > 512:
                    keep = {n: state['nodes'][n] for n in names if n in state['nodes']}
                    state['nodes'] = keep
                self._write(state)
                return name, node, identity
        return None

    def _invalidate_changed(self, name, identity, hashes):
        with _file_lock(self.directory / 'ip-quality.lock'):
            state = self._read(); entry = state['nodes'].get(name, {})
            if (entry.get('node_hash') == identity and entry.get('policy_hash') == self.policy_hash
                    and entry.get('status') == 'ok'
                    and entry.get('ip_hash_by_site') != hashes):
                entry.pop('score', None); entry['status'] = 'unknown'; entry['reason'] = 'exit_changed'
                entry['ip_hash_by_site'] = hashes
                self._write(state)

    def _execute(self, reservation):
        name, node, identity = reservation
        error, result = None, None
        try:
            result = self.assessor(node, self.settings, lambda hashes: self._invalidate_changed(name, identity, hashes))
            _check_cancel(self.settings)
            hashes = result['ip_hash_by_site']
            if (not isinstance(hashes, dict) or set(hashes) != set(self.settings['trace_urls'])
                    or any(not isinstance(v, str) or re.fullmatch('[0-9a-f]{64}', v) is None for v in hashes.values())
                    or not _number(result.get('score'), 0, 100)):
                raise QualityError('invalid_assessment')
        except QualityError as exc:
            error = exc
        except Exception:
            error = QualityError('assessment_unavailable')
        try:
            current = _nodes(self.settings).get(name)
            matches = current is not None and _identity(current) == identity
            with self._result_lock, _file_lock(self.directory / 'ip-quality.lock'):
                state = self._read(); entry = state['nodes'].get(name, {})
                if entry.get('node_hash') != identity or entry.get('policy_hash') != self.policy_hash:
                    return
                if self._cancel.is_set():
                    error = QualityError('cancelled')
                now = self.clock()
                if not matches:
                    entry.pop('score', None); entry.update(status='unknown', reason='node_changed', next_due=now)
                elif error and error.reason == 'cancelled':
                    # An exit change was already invalidated by the observation
                    # callback; otherwise keep a valid old score and retry later.
                    entry['next_due'] = now
                elif error:
                    failures = min(3, (entry.get('failures', 0) if type(entry.get('failures', 0)) is int else 0) + 1) if error.transient else 0
                    delay = RETRY_DELAYS[failures - 1] if failures else RETRY_DELAYS[-1]
                    entry.pop('score', None); entry.update(status='unknown', reason=error.reason,
                                                          failures=failures, next_due=now + delay)
                else:
                    entry.update(status='ok', observed_at=now, score=float(result['score']),
                                 ip_hash_by_site=result['ip_hash_by_site'],
                                 assessed_services=list(self.settings['trace_urls']),
                                 score_kind='third_party_trust_score',
                                 aggregation='minimum_across_services', failures=0,
                                 next_due=now + self.settings['interval_seconds'])
                    entry.pop('reason', None)
                self._write(state)
        except (OSError, ValueError, TypeError, QualityError):
            return

    def sample_async(self, names):
        if not self.enabled or self._cancel.is_set():
            return False
        with self._thread_lock:
            if self._closed or self._cancel.is_set():
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            try:
                reservation = self._reserve(names)
            except (OSError, ValueError, TypeError, QualityError):
                return False
            if reservation is None:
                return False
            # A non-daemon thread allows its owned subprocesses to be cleaned up
            # before a normal interpreter exit, even after KeyboardInterrupt.
            self._thread = threading.Thread(target=self._execute, args=(reservation,), daemon=False)
            self._thread.start()
            return True

    def pause(self):
        """Cancel and reap any in-flight assessment, allowing later samples."""
        with self._result_lock:
            self._cancel.set()
        with self._thread_lock:
            if self._thread is not None:
                self._thread.join()
                self._thread = None
            if not self._closed:
                self._cancel.clear()

    def close(self):
        """Wait for a bounded in-flight sample and its owned processes to stop."""
        self._closed = True
        with self._result_lock:
            self._cancel.set()
        with self._thread_lock:
            thread = self._thread
        if thread is not None:
            thread.join()

    def sample_blocking(self, names):
        """Run at most one due assessment; useful for explicit one-shot checks."""
        if not self.enabled or self._cancel.is_set():
            return False
        with self._thread_lock:
            if self._closed or self._cancel.is_set():
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            try:
                reservation = self._reserve(names)
            except (OSError, ValueError, TypeError, QualityError):
                return False
            if reservation is None:
                return False
            self._execute(reservation)
            return True
