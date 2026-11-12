import unittest
from clash_guard.core import Guard
from test_guard import Fake, config, step

class IdentityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000
        self.api = Fake(self.now)
        self.guard = Guard(self.api, config(), switch=True)
        self.ids = {'A': 'a', 'B': 'b'}
        self.guard.node_identity_provider = lambda: dict(self.ids)
        step(self.guard, self.api, self.now)

    def test_changed_identity_archives_and_removes_previous_history_and_primary(self):
        state = self.guard.state
        state['samples']['A'] = [[self.now, 4, 4, 400]]
        state['wins'] = {'A': [self.now - 1], 'B': [self.now - 2]}
        state['selected'], state['primary'] = 'B', 'A'
        self.api.current = 'B'
        self.ids['A'] = 'changed'
        step(self.guard, self.api, self.now + 1)
        self.assertNotIn([self.now, 4, 4, 400], state['samples'].get('A', []))
        self.assertNotEqual(state['primary'], 'A')
        self.assertFalse(any(state['wins'].values()))
        self.assertTrue(any(row['name'] == 'A' for row in state['retired_node_history']))

    def test_removed_node_does_not_keep_old_evidence(self):
        self.guard.state['samples']['B'] = [[self.now, 4, 4, 400]]
        del self.ids['B']
        step(self.guard, self.api, self.now + 1)
        self.assertNotIn('B', self.guard.state['samples'])

    def test_unknown_source_blocks_old_primary_recovery_and_preserves_history(self):
        state = self.guard.state
        state.update(selected='B', primary='A', recovery_since=self.now - 100)
        state['samples']['A'] = [[self.now, 4, 4, 400]]
        self.api.current = 'B'
        self.guard.node_identity_provider = lambda: None
        step(self.guard, self.api, self.now + 1)
        self.assertFalse(self.api.changes)
        self.assertIn('A', state['samples'])
        self.assertFalse(self.guard._upgrade_ready('B', 'A', self.now + 1))

    def test_switch_cancelled_if_identity_changes_during_verification(self):
        self.ids['B'] = 'changed'
        result = self.guard._switch_local('A', 'B', self.now + 1, 'switch')
        self.assertEqual(result, 'identity_changed')
        self.assertFalse(self.api.changes)

    def test_no_provider_retains_legacy_behavior(self):
        guard = Guard(self.api, config(), switch=True)
        step(guard, self.api, self.now)
        self.api.failed.add('A')
        for offset in (5, 20, 35): result = step(guard, self.api, self.now + offset)
        self.assertEqual(result['event'], 'switch')

class SourceFingerprintTests(unittest.TestCase):
    def test_all_parameters_but_name_affect_local_identity(self):
        from clash_guard.identity import local_node_fingerprints
        node = {'name': 'A', 'type': 'ss', 'server': 'example.invalid', 'interface-name': 'one', 'routing-mark': 1}
        original = local_node_fingerprints([node])['A']
        for field in ('server', 'interface-name', 'routing-mark'):
            changed = dict(node, **{field: 'changed'})
            self.assertNotEqual(original, local_node_fingerprints([changed])['A'])
        self.assertEqual(original, local_node_fingerprints([dict(node, name='B')])['B'])

    def test_stale_quality_is_not_used_after_parameter_change(self):
        guard = Guard(Fake(1700000000), config())
        ids = {'A': 'a', 'B': 'b'}
        guard.node_identity_provider = lambda: dict(ids)
        guard.quality_provider = lambda: {'A': {'score': 90, 'observed_at': 1700000000}, 'B': {'score': 80, 'observed_at': 1700000000}}
        guard.step(1700000000)
        ids['A'] = 'new'
        guard.step(1700000001)
        self.assertNotIn('A', guard.quality)
        self.assertIn('B', guard.quality)

class IdentitySafetyTests(unittest.TestCase):
    def test_unknown_identity_keeps_wins_and_allows_verified_fault_recovery(self):
        now = 1700000000
        api = Fake(now); guard = Guard(api, config(), switch=True)
        guard.node_identity_provider = lambda: {'A': 'a', 'B': 'b'}
        step(guard, api, now)
        guard.state['wins']['B'] = [now - 1]
        guard.node_identity_provider = lambda: None
        step(guard, api, now + 1)
        self.assertEqual(guard.state['wins']['B'], [now - 1])
        api.failed.add('A')
        for offset in (5, 20, 35): result = step(guard, api, now + offset)
        self.assertEqual(result['event'], 'switch')
        self.assertEqual(api.current, 'B')

    def test_identity_change_while_away_does_not_contact_broker(self):
        class Coordinator:
            pending = {'id': 'pending'}
            def _home(self): return False
            def _abort(self, guard, identifier): raise AssertionError('must not contact broker')
        now = 1700000000
        guard = Guard(Fake(now), config(), coordination=Coordinator())
        ids = {'A': 'a'}
        guard.node_identity_provider = lambda: dict(ids)
        guard._sync_identity(now)
        guard.coordination.pending = {'id': 'pending'}
        ids['A'] = 'changed'
        guard._sync_identity(now + 1)
        self.assertIsNone(guard.coordination.pending)

    def test_unbound_legacy_history_does_not_inherit_when_enabled(self):
        now = 1700000000
        api = Fake(now)
        state = {'selected': 'A', 'primary': 'A', 'samples': {'A': [[now - 1, 4, 4, 400]]}, 'wins': {'A': [now - 1]}}
        guard = Guard(api, config(), state)
        guard.node_identity_provider = lambda: {'A': 'new', 'B': 'b'}
        result = step(guard, api, now)
        self.assertEqual(result['event'], 'selection_grace')
        self.assertFalse(state['samples'])
        self.assertEqual(state['retired_node_history'][0]['identity'], None)
