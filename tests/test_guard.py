import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from clash_guard.api import Controller, ControllerError
from clash_guard.config import load
from clash_guard.core import Guard, eligible, metrics, rank, quality_score

HEALTH = 'https://health.example/check'


def config():
    return {'controller': 'http://127.0.0.1:9090', 'secret_env': 'TEST_UNUSED', 'group': 'Test',
            'targets': [{'url': 'https://one.example/', 'expected': '200'}, {'url': 'https://two.example/', 'expected': '204'}],
            'health_url': HEALTH, 'excluded_name_patterns': ['test-only'], 'normal_interval': 15,
            'failure_interval': 5, 'minimum_failure_duration': 30, 'startup_grace': 0,
            'sampling_interval': 10, 'hold_days': 30, 'min_sample_count': 100,
            'min_sample_span_hours': 72, 'review_wins': 3, 'review_span_hours': 48, 'recovery_seconds': 30}


class Fake:
    def __init__(self, now):
        self.now = now; self.current = 'A'; self.failed = set(); self.error = False; self.changes = []
    def proxies(self):
        stamp = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()
        def node(name):
            return {'name': name, 'type': 'Vless', 'extra': {HEALTH: {'alive': True, 'history': [{'time': stamp, 'delay': 55}]}}}
        return {'Test': {'type': 'Selector', 'now': self.current, 'all': ['A', 'B', 'test-only']}, 'A': node('A'), 'B': node('B'), 'test-only': node('test-only')}
    def probe(self, name, target):
        if self.error:
            raise ControllerError('unavailable')
        return None if name in self.failed else (60 if name == 'B' else 120)
    def select(self, group, node):
        self.changes.append((group, node)); self.current = node


def step(guard, api, when):
    api.now = when
    return guard.step(when)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000
        self.api = Fake(self.now)
        self.guard = Guard(self.api, config(), switch=True)
        self.assertEqual(step(self.guard, self.api, self.now)['event'], 'selection_grace')
    def test_short_failure_does_not_switch(self):
        self.api.failed.add('A')
        for t in (5, 10, 20):
            self.assertEqual(step(self.guard, self.api, self.now + t)['event'], 'node_failed')
        self.assertEqual(self.api.changes, [])
    def test_persistent_failure_switches_only_verified_node(self):
        self.api.failed.add('A')
        for t in (5, 20, 35):
            result = step(self.guard, self.api, self.now + t)
        self.assertEqual(result['event'], 'switch')
        self.assertEqual(self.api.changes, [('Test', 'B')])
        self.assertEqual(self.guard.state['primary'], 'A')
    def test_failed_backup_never_switches(self):
        self.api.failed.update(('A', 'B'))
        for t in (5, 20, 35):
            result = step(self.guard, self.api, self.now + t)
        self.assertEqual(result['event'], 'no_candidate')
        self.assertFalse(self.api.changes)
    def test_dry_run_reports_without_writing_selection(self):
        self.guard.switch = False
        self.api.failed.add('A')
        for t in (5, 20, 35):
            result = step(self.guard, self.api, self.now + t)
        self.assertEqual(result['event'], 'would_switch')
        self.assertFalse(self.api.changes)
    def test_controller_failure_resets_outage_evidence(self):
        self.api.failed.add('A')
        step(self.guard, self.api, self.now + 5)
        self.api.error = True
        self.assertEqual(step(self.guard, self.api, self.now + 10)['event'], 'monitor_error')
        self.assertEqual(self.guard.state['failures'], 0)
    def test_manual_selection_resets_primary_and_grants_grace(self):
        self.api.current = 'B'
        result = step(self.guard, self.api, self.now + 5)
        self.assertEqual(result['event'], 'selection_grace')
        self.assertEqual(self.guard.state['primary'], 'B')
    def test_original_primary_returns_after_continuous_recovery(self):
        self.api.failed.add('A')
        for t in (5, 20, 35): step(self.guard, self.api, self.now + t)
        self.api.failed.clear()
        self.assertEqual(step(self.guard, self.api, self.now + 40)['event'], 'healthy')
        self.assertEqual(step(self.guard, self.api, self.now + 69)['event'], 'healthy')
        self.assertEqual(step(self.guard, self.api, self.now + 70)['event'], 'recover')
        self.assertEqual(self.api.current, 'A')
    def test_recovered_original_cancels_failure_switch(self):
        class Recovering(Fake):
            def __init__(self, now): super().__init__(now); self.counter = 0
            def probe(self, name, target):
                if name == 'A':
                    self.counter += 1
                    if self.counter > 6: return 100
                    return None
                return 50
        api = Recovering(self.now); guard = Guard(api, config(), switch=True)
        step(guard, api, self.now)
        for t in (5, 20, 35): result = step(guard, api, self.now + t)
        self.assertEqual(result['event'], 'current_recovered')
        self.assertFalse(api.changes)
    def test_quality_ranking_and_stale_samples(self):
        h = {'A': [[self.now - 1, 100], [self.now - 2, None]], 'B': [[self.now - 1, 120], [self.now - 2, 120]]}
        self.assertEqual(rank(['A', 'B'], h, self.now), ['B', 'A'])
        self.assertEqual(metrics(h['A'], self.now)['success_rate'], .5)
        self.assertEqual(metrics(h['A'], self.now + 31 * 86400)['count'], 0)
    def test_healthy_upgrade_requires_history_and_wins(self):
        c = config(); c.update(hold_days=0, min_sample_count=2, min_sample_span_hours=0, review_wins=3, review_span_hours=0, sampling_interval=1)
        api = Fake(self.now); guard = Guard(api, c, switch=True)
        step(guard, api, self.now)
        for t in (1, 2, 3, 4, 5):
            result = step(guard, api, self.now + t)
            if result['event'] == 'upgrade': break
        self.assertEqual(result['event'], 'upgrade')
        self.assertEqual(api.current, 'B')
        self.assertEqual(guard.state['primary'], 'B')
    def test_fault_fallback_rotates_past_failed_candidates(self):
        class Many(Fake):
            def proxies(self):
                p = super().proxies()
                for name in ('C', 'D', 'E', 'F'):
                    p[name] = dict(p['B'], name=name)
                p['Test']['all'] = ['A', 'B', 'C', 'D', 'E', 'F']
                return p
        api = Many(self.now); api.failed.update(('A', 'B', 'C', 'D', 'E'))
        guard = Guard(api, config(), switch=True); step(guard, api, self.now)
        for t in (5, 20, 35): result = step(guard, api, self.now + t)
        self.assertEqual(result['event'], 'no_candidate')
        result = step(guard, api, self.now + 40)
        self.assertEqual(result['event'], 'switch')
        self.assertEqual(api.current, 'F')
    def test_monitor_error_resets_recovery_timer(self):
        self.api.failed.add('A')
        for t in (5, 20, 35): step(self.guard, self.api, self.now + t)
        self.api.failed.clear(); step(self.guard, self.api, self.now + 40)
        self.api.error = True
        self.assertEqual(step(self.guard, self.api, self.now + 45)['event'], 'monitor_error')
        self.assertIsNone(self.guard.state['recovery_since'])
        self.api.error = False
        self.assertEqual(step(self.guard, self.api, self.now + 80)['event'], 'healthy')
        self.assertEqual(self.api.current, 'B')
    def test_half_hour_buckets_preserve_72_hours_of_samples(self):
        guard = Guard(self.api, config())
        for offset in range(0, 73 * 3600 + 1, 15):
            guard._probe('A', self.now + offset)
        report = metrics(guard.state['samples']['A'], self.now + 73 * 3600)
        self.assertGreater(report['count'], 100)
        self.assertGreaterEqual(report['span'], 72 * 3600)
        self.assertLess(len(guard.state['samples']['A']), 150)
    def test_review_wins_span_full_period_despite_frequent_sampling(self):
        c = config(); c.update(hold_days=0, min_sample_count=1, min_sample_span_hours=0,
                               review_wins=3, review_span_hours=48, sampling_interval=1, normal_interval=86400)
        api = Fake(self.now); guard = Guard(api, c, switch=True)
        step(guard, api, self.now)
        self.assertEqual(step(guard, api, self.now + 1)['event'], 'healthy')
        for hour in range(1, 48):
            self.assertEqual(step(guard, api, self.now + hour * 3600 + 1)['event'], 'healthy')
        self.assertEqual(step(guard, api, self.now + 48 * 3600 + 1)['event'], 'upgrade')

    def test_health_url_can_be_a_probed_service(self):
        class Updating(Fake):
            def probe(self, name, target):
                if name == 'B': self.now += 2
                return super().probe(name, target)
        api = Updating(self.now); api.failed.add('A'); c = config()
        c['targets'][0]['url'] = HEALTH
        guard = Guard(api, c, switch=True, clock=lambda: api.now)
        step(guard, api, self.now)
        for t in (5, 20, 35): result = step(guard, api, self.now + t)
        self.assertEqual(result['event'], 'switch')
        self.assertEqual(api.current, 'B')
    def test_external_quality_only_breaks_stability_ties(self):
        history = {'A': [[self.now - 1, 100]], 'B': [[self.now - 1, 100]]}
        quality = {'A': {'score': 70, 'observed_at': self.now - 10}, 'B': {'score': 90, 'observed_at': self.now - 10}}
        self.assertEqual(rank(['A', 'B'], history, self.now, quality), ['B', 'A'])
        self.assertIsNone(quality_score('missing', quality, self.now))
        self.assertIsNone(quality_score('B', quality, self.now + 86401))
        history['B'].append([self.now - 2, None])
        self.assertEqual(rank(['A', 'B'], history, self.now, quality), ['A', 'B'])
    def test_unknown_quality_does_not_imply_a_score(self):
        h = {'A': [[self.now - 1, 100]], 'B': [[self.now - 1, 80]]}
        for score in (5, 49, 99):
            reports = {'A': {'score': score, 'observed_at': self.now}}
            self.assertEqual(rank(['A', 'B'], h, self.now, reports), ['B', 'A'])
        reports = {'A': {'score': 99, 'observed_at': self.now}, 'B': {'score': 5, 'observed_at': self.now}}
        self.assertEqual(rank(['A', 'B'], h, self.now, reports), ['A', 'B'])

    def test_quality_can_block_degraded_healthy_upgrade(self):
        c = config(); c.update(hold_days=0, min_sample_count=2, min_sample_span_hours=0, review_wins=3, review_span_hours=0, sampling_interval=1)
        api = Fake(self.now); guard = Guard(api, c, switch=True)
        guard.quality = {'A': {'score': 90, 'observed_at': self.now}, 'B': {'score': 30, 'observed_at': self.now}}
        step(guard, api, self.now)
        for t in (1, 2, 3, 4, 5, 6):
            self.assertEqual(step(guard, api, self.now + t)['event'], 'healthy')
        self.assertEqual(api.current, 'A')

    def test_unknown_or_expired_health_is_not_eligible(self):
        p = self.api.proxies()
        self.assertTrue(eligible('B', p, self.now, HEALTH, []))
        self.assertFalse(eligible('B', p, self.now + 301, HEALTH, []))
        self.assertFalse(eligible('test-only', p, self.now, HEALTH, ['test-only']))
        p['B']['extra'][HEALTH]['alive'] = False
        self.assertFalse(eligible('B', p, self.now, HEALTH, []))


class ConfigTests(unittest.TestCase):
    def test_example_loads_and_cli_target_does_not_leak_secrets(self):
        c = load('examples/config.json')
        self.assertEqual(len(c['targets']), 2)
        self.assertNotIn('secret', c)
    def test_non_loopback_controller_is_rejected(self):
        with self.assertRaises(ValueError): Controller('http://example.com:9090')
    def test_target_credentials_rejected(self):
        c = config(); c['targets'][0]['url'] = 'https://user:pw@one.example/'
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x.json'; p.write_text(json.dumps(c))
            with self.assertRaises(ValueError): load(p)


if __name__ == '__main__': unittest.main()
