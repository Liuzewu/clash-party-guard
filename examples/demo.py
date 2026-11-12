"""Offline demonstration: no local controller or network is used."""
import datetime as dt
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clash_guard.config import load
from clash_guard.core import Guard


class DemoController:
    now = 1700000000
    def proxies(self):
        stamp = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()
        def node(name):
            return {'name': name, 'type': 'Vless', 'extra': {'https://www.gstatic.com/generate_204': {'alive': True, 'history': [{'time': stamp, 'delay': 80}]}}}
        return {'Example Selector': {'type': 'Selector', 'now': 'Node A', 'all': ['Node A', 'Node B']}, 'Node A': node('Node A'), 'Node B': node('Node B')}
    def probe(self, node, target):
        return None if node == 'Node A' else 80
    def select(self, group, node):
        raise RuntimeError('offline demonstration never changes selection')


cfg = load(Path(__file__).with_name('config.json'))
cfg['startup_grace'] = 0
api = DemoController()
guard = Guard(api, cfg)
for offset in (0, 5, 20, 35):
    api.now = 1700000000 + offset
    print(json.dumps(guard.step(api.now), ensure_ascii=False))
