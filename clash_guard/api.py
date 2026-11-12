"""Mihomo controller client."""
import datetime as dt
import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class ControllerError(RuntimeError):
    pass


class Controller:
    def __init__(self, address, secret_env='CLASH_CONTROLLER_SECRET', timeout=6):
        self.address = address.rstrip('/')
        p = urllib.parse.urlsplit(self.address)
        try:
            local = p.hostname == 'localhost' or ipaddress.ip_address(p.hostname).is_loopback
        except ValueError:
            local = False
        if p.scheme != 'http' or not local or p.path not in ('', '/') or p.query or p.username:
            raise ValueError('controller must be a local HTTP address')
        self.secret_env, self.timeout = secret_env, timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        secret = os.environ.get(self.secret_env, '') if self.secret_env else ''
        if secret:
            headers['Authorization'] = 'Bearer ' + secret
        raw = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(self.address + path, data=raw, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                result = response.read()
                return response.status, json.loads(result) if result else {}
        except urllib.error.HTTPError as e:
            if e.code in (503, 504):
                try:
                    return e.code, json.loads(e.read())
                except (ValueError, OSError):
                    raise ControllerError('invalid failure response') from e
            raise ControllerError('controller rejected request') from e
        except (OSError, ValueError) as e:
            raise ControllerError('controller unavailable') from e

    def proxies(self):
        code, data = self._request('GET', '/proxies')
        if code != 200 or not isinstance(data.get('proxies'), dict):
            raise ControllerError('invalid proxy response')
        return data['proxies']

    def probe(self, name, target):
        node = urllib.parse.quote(name, safe='')
        query = urllib.parse.urlencode({'url': target['url'], 'expected': str(target['expected']), 'timeout': 4000})
        started = dt.datetime.now(dt.timezone.utc).timestamp()
        code, result = self._request('GET', '/proxies/' + node + '/delay?' + query)
        if (code, result.get('message')) in ((504, 'Timeout'), (503, 'An error occurred in the delay test')):
            return None
        if code != 200 or isinstance(result.get('delay'), bool) or not isinstance(result.get('delay'), (int, float)):
            raise ControllerError('invalid delay response')
        code, details = self._request('GET', '/proxies/' + node)
        try:
            evidence = details['extra'][target['url']]
            last = evidence['history'][-1]
            stamp = re.sub(r'(\.\d{6})\d+(?=(?:Z|[+-]\d\d:\d\d)$)', r'\1', last['time'])
            when = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            if code != 200 or details['name'] != name or not started - 1 <= when <= dt.datetime.now(dt.timezone.utc).timestamp() + 1:
                raise ValueError('stale probe')
            delay = last['delay']
            if evidence.get('alive') is True and isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay > 0:
                return float(delay)
            if evidence.get('alive') is False and delay == 0:
                return None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ControllerError('missing probe evidence') from e
        raise ControllerError('inconsistent probe evidence')

    def select(self, group, node):
        code, _ = self._request('PUT', '/proxies/' + urllib.parse.quote(group, safe=''), {'name': node})
        if code != 204:
            raise ControllerError('selection was not accepted')
