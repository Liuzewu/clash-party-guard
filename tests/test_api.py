import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from clash_guard.api import Controller, ControllerError


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, code, data=None):
        raw = b'' if data is None else json.dumps(data).encode()
        self.send_response(code); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        p = urlsplit(self.path)
        if p.path == '/proxies': return self.send(200, {'proxies': {'group': {'type': 'Selector', 'now': self.server.current, 'all': ['node A']}}})
        if p.path == '/proxies/node%20A/delay':
            q = parse_qs(p.query); self.server.last_url = q['url'][0]; self.server.expected = q['expected'][0]
            return self.send(200, {'delay': 123})
        if p.path == '/proxies/node%20A':
            return self.send(200, {'name': 'node A', 'extra': {self.server.last_url: {'alive': True, 'history': [{'delay': 123, 'time': dt.datetime.now(dt.timezone.utc).isoformat()}]}}})
        return self.send(404, {})
    def do_PUT(self):
        value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.current = value['name']; self.send(204)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.current = 'node A'; cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def test_local_controller_request_probe_and_selection(self):
        c = Controller('http://127.0.0.1:' + str(self.server.server_port))
        self.assertEqual(c.proxies()['group']['now'], 'node A')
        self.assertEqual(c.probe('node A', {'url': 'https://one.example/', 'expected': '204'}), 123)
        self.assertEqual(self.server.expected, '204')
        c.select('group', 'node A')
        self.assertEqual(self.server.current, 'node A')
    def test_unknown_path_is_controller_error(self):
        c = Controller('http://127.0.0.1:' + str(self.server.server_port))
        with self.assertRaises(ControllerError): c.probe('missing', {'url': 'https://one.example/', 'expected': '200'})
