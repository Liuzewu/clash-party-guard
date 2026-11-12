import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from clash_guard.coordination import Broker, CommandTransport, Coordination, exchange_file, validate_coordination_config
from clash_guard.core import Guard
from clash_guard.identity import identity_key, load_nodes, node_fingerprint, node_fingerprints
from test_guard import Fake, config


class Clock:
    def __init__(self, now=1700000000): self.now = now
    def __call__(self): return self.now


class Transport:
    def __init__(self, broker): self.broker, self.calls, self.offline = broker, [], False
    def exchange(self, request):
        self.calls.append(request.copy())
        if self.offline: raise ConnectionError('test')
        return self.broker.exchange(request)


def instance(clock, member, transport, *, home=True, enabled=True, required=None, ids=None):
    conf = {'member': member, 'group_id': 'shared', 'required_members': required or ['one', 'two'],
            'node_ids': ids or {'A': 'a' * 64, 'B': 'b' * 64}, 'failure_wait': 20}
    coord = Coordination(conf, transport=transport, presence=lambda: home)
    api = Fake(clock.now)
    return api, Guard(api, config(), switch=enabled, coordination=coord, clock=clock)


def step(clock, api, guard, t):
    clock.now = t; api.now = t
    return guard.step(t)


class IdentityTests(unittest.TestCase):
    def test_matching_configuration_and_meaningful_changes(self):
        node = {'name': 'one', 'type': 'vless', 'server': 'example.invalid', 'port': 443,
                'password': 'not-a-real-secret', 'client-fingerprint': 'chrome', 'dialer-proxy': 'chain-a'}
        same = dict(node, name='another name', **{'interface-name': 'en1', 'routing-mark': 123})
        self.assertEqual(node_fingerprint(node), node_fingerprint(same))
        for changed in (dict(node, port=444), dict(node, **{'client-fingerprint': 'firefox'}),
                        dict(node, **{'dialer-proxy': 'chain-b'}), dict(node, password='changed')):
            self.assertNotEqual(node_fingerprint(node), node_fingerprint(changed))
        self.assertNotEqual(node_fingerprint(node, b'key-one'), node_fingerprint(node, b'key-two'))

    def test_dynamic_source_and_ambiguous_aliases(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'nodes.json'
            path.write_text(json.dumps({'proxies': [{'name': 'A', 'type': 'ss', 'server': 'example.invalid', 'port': 1}]}))
            cfg = {'node_config_path': str(path)}
            first = node_fingerprints(load_nodes(cfg))['A']
            path.write_text(json.dumps({'proxies': [{'name': 'A', 'type': 'ss', 'server': 'example.invalid', 'port': 2}]}))
            self.assertNotEqual(first, node_fingerprints(load_nodes(cfg))['A'])
            self.assertEqual(node_fingerprints([{'name': 'A', 'server': 'x'}, {'name': 'B', 'server': 'x'}]), {})

    def test_key_env_missing_is_not_unkeyed_fallback(self):
        with self.assertRaises(ValueError): identity_key({'identity_key_env': 'CLASH_TEST_NONEXISTENT_SECRET'})

    def test_loader_command_and_source_size_are_bounded(self):
        out = '{"proxies":[{"name":"A","server":"example.invalid"}]}'
        nodes = load_nodes({'identity_command': [sys.executable, '-c', 'print(' + repr(out) + ')']})
        self.assertEqual(nodes[0]['name'], 'A')
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'large.json'; source.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'size limit'): load_nodes({'node_config_path': str(source)})
        with self.assertRaisesRegex(ValueError, 'output limit'):
            load_nodes({'identity_command': [sys.executable, '-c', 'import sys; sys.stdout.write("x" * (5*1024*1024))']})

    def test_configuration_checks_unknown_home_and_conflicting_sources(self):
        base = {'member': 'one', 'group_id': 'shared', 'required_members': ['one'],
                'node_ids': {'A': 'a' * 64}, 'command': ['true']}
        with self.assertRaisesRegex(ValueError, 'presence'):
            Coordination(base)
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            Coordination(dict(base, fixed_home=True, node_config_path='nodes.json'))
        with self.assertRaisesRegex(ValueError, 'failure_wait'):
            Coordination(dict(base, fixed_home=True, failure_wait=100))

    def test_pure_validation_does_not_run_commands_or_read_files(self):
        validate_coordination_config({'enabled': False})
        conf = {'member': 'one', 'group_id': 'shared', 'required_members': ['one'], 'fixed_home': True,
                'command': ['/not/a/real/executable'], 'node_config_path': '/not/a/real/file.json'}
        validate_coordination_config(conf)
        for file in ('examples/coordination-local.example.json', 'examples/coordination-peer.example.json'):
            validate_coordination_config(json.loads(Path(file).read_text())['coordination'])


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.broker = Broker(clock=self.clock); self.transport = Transport(self.broker)
        self.a, self.ga = instance(self.clock, 'one', self.transport)
        self.b, self.gb = instance(self.clock, 'two', self.transport)
        self.t = self.clock.now
        step(self.clock, self.a, self.ga, self.t); step(self.clock, self.b, self.gb, self.t)
        step(self.clock, self.a, self.ga, self.t + 1); step(self.clock, self.b, self.gb, self.t + 1)

    def plan_fault(self):
        self.a.failed.add('A')
        for t in (5, 20, 35): result = step(self.clock, self.a, self.ga, self.t + t)
        return result

    def test_two_devices_ack_and_commit(self):
        self.assertEqual(self.plan_fault()['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 36)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 39)['event'], 'coordinated_switch')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 40)['event'], 'coordinated_switch')
        self.assertEqual((self.a.current, self.b.current), ('B', 'B'))
        self.assertEqual(self.broker.state['plans']['shared']['status'], 'done')

    def test_healthy_upgrade_waits_for_both_devices(self):
        self._ready_for_upgrade()
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 2)['event'], 'coordination_pending')
        self.assertEqual(self.a.current, 'A')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 3)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 6)['event'], 'coordinated_upgrade')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 7)['event'], 'coordinated_upgrade')
        self.assertEqual((self.a.current, self.b.current), ('B', 'B'))

    def _ready_for_upgrade(self):
        for guard in (self.ga, self.gb):
            guard.config.update(hold_days=0, min_sample_count=1, min_sample_span_hours=0,
                                review_wins=1, review_span_hours=0, sampling_interval=1)
            guard.state['wins']['B'] = [self.t + 1]

    def test_quality_revocation_after_ack_cancels_upgrade(self):
        self._ready_for_upgrade()
        live = {'data': {'A': {'score': 70, 'observed_at': self.t}, 'B': {'score': 90, 'observed_at': self.t}}}
        self.ga.quality_provider = lambda: live['data']
        self.gb.quality = live['data']
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 2)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 3)['event'], 'coordination_pending')
        live['data'] = {}
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 6)['event'], 'upgrade_cancelled')
        self.assertEqual((self.a.current, self.b.current), ('A', 'A'))
        self.assertEqual(self.broker.state['plans']['shared']['status'], 'aborted')

    def test_quality_decline_after_ack_cancels_upgrade(self):
        self._ready_for_upgrade()
        live = {'data': {'A': {'score': 70, 'observed_at': self.t}, 'B': {'score': 90, 'observed_at': self.t}}}
        self.ga.quality_provider = lambda: live['data']; self.gb.quality = live['data']
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 2)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 3)['event'], 'coordination_pending')
        live['data'] = {'A': {'score': 70, 'observed_at': self.t}, 'B': {'score': 50, 'observed_at': self.t}}
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 6)['event'], 'upgrade_cancelled')
        self.assertEqual((self.a.current, self.b.current), ('A', 'A'))

    def test_quality_provider_revocation_immediately_before_single_device_switch(self):
        api = Fake(self.t); local = config()
        local.update(hold_days=0, min_sample_count=1, min_sample_span_hours=0,
                     review_wins=1, review_span_hours=0, sampling_interval=1)
        guard = Guard(api, local, switch=True, clock=self.clock)
        calls = []
        def provider():
            calls.append(1)
            return ({'A': {'score': 70, 'observed_at': self.t}, 'B': {'score': 90, 'observed_at': self.t}}
                    if len(calls) < 3 else {})
        guard.quality_provider = provider
        step(self.clock, api, guard, self.t)
        result = step(self.clock, api, guard, self.t + 1)
        self.assertEqual(result['event'], 'upgrade_cancelled')
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(api.current, 'A')

    def test_peer_without_sustained_evidence_rejects_upgrade(self):
        self.ga.config.update(hold_days=0, min_sample_count=1, min_sample_span_hours=0,
                              review_wins=1, review_span_hours=0, sampling_interval=1)
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 2)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 3)['event'], 'upgrade_cancelled')
        self.assertEqual((self.a.current, self.b.current), ('A', 'A'))

    def test_different_local_names_match_same_endpoint_ids(self):
        class Aliased(Fake):
            def __init__(self, now): super().__init__(now); self.current = 'X'
            def proxies(self):
                p = super().proxies()
                for old, new in [('A', 'X'), ('B', 'Y')]:
                    p[new] = dict(p.pop(old), name=new)
                p['Test']['all'] = ['X', 'Y', 'test-only']
                p['Test']['now'] = self.current
                return p
            def probe(self, name, target): return super().probe({'X': 'A', 'Y': 'B'}.get(name, name), target)
        api = Aliased(self.t)
        coordinator = Coordination({'member': 'two', 'group_id': 'shared', 'required_members': ['one', 'two'],
                                    'node_ids': {'X': 'a' * 64, 'Y': 'b' * 64}},
                                   transport=self.transport, presence=lambda: True)
        guard = Guard(api, config(), switch=True, coordination=coordinator, clock=self.clock)
        step(self.clock, api, guard, self.t + 2); step(self.clock, api, guard, self.t + 3)
        self.plan_fault()
        self.assertEqual(step(self.clock, api, guard, self.t + 36)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 39)['event'], 'coordinated_switch')
        self.assertEqual(step(self.clock, api, guard, self.t + 40)['event'], 'coordinated_switch')
        self.assertEqual((self.a.current, api.current), ('B', 'Y'))

    def test_failed_candidate_aborts_before_commit(self):
        self.plan_fault(); self.b.failed.add('B')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 36)['event'], 'coordination_candidate_unready')
        self.assertEqual(self.broker.state['plans']['shared']['status'], 'aborted')
        self.assertEqual(self.b.current, 'A')
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 37)['event'], 'switch')
        self.assertEqual((self.a.current, self.b.current), ('B', 'A'))

    def test_manual_change_cancels_plan_without_overriding_user(self):
        self.plan_fault(); self.b.current = 'B'
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 36)['event'], 'selection_grace')
        self.assertEqual(self.broker.state['plans']['shared']['status'], 'aborted')
        self.assertEqual(self.b.changes, [])
        self.assertEqual(self.gb.state['primary'], 'B')

    def test_offline_member_healthy_upgrade_blocked_fault_recovers(self):
        self.clock.now = self.t + 50
        self.a.now = self.clock.now
        step(self.clock, self.a, self.ga, self.clock.now)
        self._ready_for_upgrade()
        self.assertEqual(self.ga.coordination.propose(self.ga, 'A', 'B', self.clock.now, 'upgrade'), 'coordination_unavailable')
        self.a.failed.add('A')
        for t in (55, 70, 85): result = step(self.clock, self.a, self.ga, self.t + t)
        self.assertEqual(result['event'], 'switch')
        self.assertEqual(self.a.current, 'B')

    def test_away_operates_without_contacting_broker(self):
        api, guard = instance(self.clock, 'away', self.transport, home=False, required=['away'])
        before = len(self.transport.calls)
        step(self.clock, api, guard, self.t); api.failed.add('A')
        for t in (5, 20, 35): result = step(self.clock, api, guard, self.t + t)
        self.assertEqual(result['event'], 'switch')
        self.assertEqual(len(self.transport.calls), before)

    def test_departure_during_prepare_sends_no_abort(self):
        self.assertEqual(self.plan_fault()['event'], 'coordination_pending')
        presence = iter((True, False))
        self.gb.coordination.presence = lambda: next(presence, False)
        before = len(self.transport.calls)
        result = step(self.clock, self.b, self.gb, self.t + 36)
        self.assertEqual(result['event'], 'coordination_candidate_unready')
        self.assertEqual([v['op'] for v in self.transport.calls[before:]], ['heartbeat'])
        self.assertEqual(self.b.changes, [])
        self.assertIsNone(self.gb.coordination.pending)

    def test_departure_during_commit_sends_no_abort(self):
        self.assertEqual(self.plan_fault()['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 36)['event'], 'coordination_pending')
        presence = iter((True, False))
        self.ga.coordination.presence = lambda: next(presence, False)
        before = len(self.transport.calls)
        result = step(self.clock, self.a, self.ga, self.t + 39)
        self.assertEqual(result['event'], 'coordination_candidate_unready')
        self.assertEqual([v['op'] for v in self.transport.calls[before:]], ['heartbeat'])
        self.assertEqual(self.a.changes, [])
        self.assertIsNone(self.ga.coordination.pending)

    def test_unknown_presence_during_prepare_sends_no_abort(self):
        self.assertEqual(self.plan_fault()['event'], 'coordination_pending')
        presence = iter((True, None))
        self.gb.coordination.presence = lambda: next(presence, None)
        before = len(self.transport.calls)
        step(self.clock, self.b, self.gb, self.t + 36)
        self.assertEqual([v['op'] for v in self.transport.calls[before:]], ['heartbeat'])

    def test_unknown_presence_during_commit_sends_no_abort(self):
        self.assertEqual(self.plan_fault()['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 36)['event'], 'coordination_pending')
        presence = iter((True, None))
        self.ga.coordination.presence = lambda: next(presence, None)
        before = len(self.transport.calls)
        step(self.clock, self.a, self.ga, self.t + 39)
        self.assertEqual([v['op'] for v in self.transport.calls[before:]], ['heartbeat'])
        self.assertEqual(self.a.changes, [])
        self.assertIsNone(self.ga.coordination.pending)

    def test_unknown_presence_blocks_healthy_but_not_failure_recovery(self):
        api, guard = instance(self.clock, 'unknown', self.transport, home=None, required=['unknown'])
        step(self.clock, api, guard, self.t)
        self.assertEqual(guard.coordination.propose(guard, 'A', 'B', self.t + 1, 'upgrade'), 'coordination_unavailable')
        api.failed.add('A')
        for t in (5, 20, 35): result = step(self.clock, api, guard, self.t + t)
        self.assertEqual(result['event'], 'switch')

    def test_broker_failure_during_plan_allows_fault_fallback(self):
        self.plan_fault(); self.transport.offline = True
        result = step(self.clock, self.a, self.ga, self.t + 36)
        self.assertEqual(result['event'], 'switch'); self.assertEqual(self.a.current, 'B')
        self.assertEqual(self.b.current, 'A')

    def test_proposer_recovers_before_commit(self):
        self.plan_fault(); self.a.failed.clear()
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 36)['event'], 'current_recovered')
        self.assertEqual(self.broker.state['plans']['shared']['status'], 'aborted')
        self.assertEqual(self.a.current, 'A')

    def test_dry_run_never_contacts_broker_or_commits(self):
        api, guard = instance(self.clock, 'dry', self.transport, enabled=False, required=['dry'])
        before = len(self.transport.calls)
        step(self.clock, api, guard, self.t); api.failed.add('A')
        for t in (5, 20, 35): result = step(self.clock, api, guard, self.t + t)
        self.assertEqual(result['event'], 'would_switch')
        self.assertEqual(len(self.transport.calls), before)
        self.assertIsNone(guard.coordination.tick(guard, self.t + 36))
        self.assertIsNone(guard.coordination.propose(guard, 'A', 'B', self.t + 36, 'upgrade'))
        self.assertEqual(len(self.transport.calls), before)

    def test_recovery_converges_when_healthy_peer_already_on_primary(self):
        self.a.failed.add('A'); self.transport.offline = True
        for t in (5, 20, 35): result = step(self.clock, self.a, self.ga, self.t + t)
        self.assertEqual(result['event'], 'switch'); self.assertEqual(self.a.current, 'B')
        self.transport.offline = False; self.a.failed.clear()
        step(self.clock, self.b, self.gb, self.t + 40)
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 40)['event'], 'healthy')
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 70)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 71)['event'], 'coordination_pending')
        self.assertEqual(step(self.clock, self.a, self.ga, self.t + 74)['event'], 'coordinated_recover')
        self.assertEqual(step(self.clock, self.b, self.gb, self.t + 75)['event'], 'coordinated_recover')
        self.assertEqual((self.a.current, self.b.current), ('A', 'A'))
        self.assertEqual(self.b.changes, [])

    def test_expired_or_invalid_plan_never_commits(self):
        self.plan_fault(); self.clock.now = self.t + 70
        reply = self.broker.exchange({'op': 'poll', 'group': 'shared', 'member': 'one'})
        self.assertEqual(reply['plan']['status'], 'expired')
        reply = self.broker.exchange({'op': 'ack', 'group': 'shared', 'member': 'two', 'id': 'not-the-plan', 'ready': True})
        self.assertEqual(reply['status'], 'rejected'); self.assertEqual(self.b.current, 'A')


class FileTransportTests(unittest.TestCase):
    def test_one_shot_process_transport_with_private_state(self):
        with tempfile.TemporaryDirectory() as d:
            command = [sys.executable, '-m', 'clash_guard.coordination', '--state', str(Path(d) / 'broker.json')]
            transport = CommandTransport(command)
            result = transport.exchange({'op': 'heartbeat', 'member': 'one', 'group': 'service',
                                         'selected': 'a' * 64, 'eligible': ['b' * 64], 'ready': True})
            self.assertEqual(result['status'], 'ok')
            state = json.loads((Path(d) / 'broker.json').read_text())
            self.assertEqual(state['members']['service']['one']['selected'], 'a' * 64)
            self.assertFalse((Path(d) / 'broker.json').read_text().find('example.invalid') >= 0)

    def test_concurrent_broker_processes_create_only_one_plan(self):
        with tempfile.TemporaryDirectory() as d:
            command = [sys.executable, '-m', 'clash_guard.coordination', '--state', str(Path(d) / 'broker.json')]
            transport = CommandTransport(command)
            for member in ('one', 'two'):
                transport.exchange({'op': 'heartbeat', 'member': member, 'group': 'service',
                                    'selected': 'a' * 64, 'eligible': ['b' * 64], 'ready': True})
            def propose(member):
                return transport.exchange({'op': 'propose', 'member': member, 'group': 'service',
                                           'current': 'a' * 64, 'target': 'b' * 64, 'purpose': 'upgrade',
                                           'required_members': ['one', 'two']})['status']
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(propose, ('one', 'two')))
            self.assertEqual(sorted(results), ['busy', 'ok'])


if __name__ == '__main__': unittest.main()
