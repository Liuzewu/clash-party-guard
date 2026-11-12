import unittest

from clash_guard.coordination import Broker
from test_coordination import Clock, Transport, instance, step
from test_guard import Fake, config
from clash_guard.core import Guard


class ParentFake(Fake):
    def __init__(self, now):
        super().__init__(now)
        self.parent_selected = 'A'
        self.probes = []

    def proxies(self):
        result = super().proxies()
        result['Test']['all'].append('Parent')
        result['Parent'] = {'type': 'Selector', 'now': self.parent_selected, 'all': ['A', 'B', 'Test']}
        return result

    def probe(self, name, target):
        self.probes.append(name)
        return super().probe(name, target)


class ParentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = ParentFake(self.clock.now)
        self.conf = config()
        self.conf.update(follow_parent_groups=['Parent'], startup_grace=30)
        self.guard = Guard(self.api, self.conf, switch=True, clock=self.clock)

    def run_at(self, offset):
        return step(self.clock, self.api, self.guard, 1700000000 + offset)

    def test_follow_changes_parent_leaf_without_probing_or_selection(self):
        self.run_at(0)
        self.api.current = 'Parent'
        self.assertEqual(self.run_at(1)['event'], 'following_parent')
        self.api.parent_selected = 'B'
        self.assertEqual(self.run_at(2)['event'], 'following_parent')
        self.assertEqual(self.api.probes, [])
        self.assertEqual(self.api.changes, [])

    def test_exit_even_to_previous_leaf_starts_new_grace(self):
        self.run_at(0)
        self.api.current = 'Parent'; self.run_at(1)
        self.api.current = 'A'
        self.assertEqual(self.run_at(2)['event'], 'selection_grace')
        self.assertEqual(self.guard.state['primary'], 'A')
        self.assertEqual(self.guard.state['grace_until'], 1700000032)
        self.assertEqual(self.run_at(3)['event'], 'selection_grace')

    def test_parent_cycle_is_unavailable_and_never_probed(self):
        self.api.current = 'Parent'; self.api.parent_selected = 'Test'
        self.assertEqual(self.run_at(0)['event'], 'follow_parent_unavailable')
        self.assertFalse(self.api.probes)
        self.assertFalse(self.api.changes)

    def test_follow_during_plan_aborts_and_does_not_force_selection(self):
        clock = self.clock; broker = Broker(clock=clock); transport = Transport(broker)
        a, ga = instance(clock, 'one', transport)
        _, gb = instance(clock, 'two', transport)
        b = self.api
        gb.api = b; gb.config['follow_parent_groups'] = ['Parent']
        t = clock.now
        for at in (t, t + 1):
            step(clock, a, ga, at); step(clock, b, gb, at)
        a.failed.add('A')
        for off in (5, 20, 35): result = step(clock, a, ga, t + off)
        self.assertEqual(result['event'], 'coordination_pending')
        b.current = 'Parent'
        self.assertEqual(step(clock, b, gb, t + 36)['event'], 'following_parent')
        self.assertEqual(broker.state['plans']['shared']['status'], 'aborted')
        self.assertEqual(b.changes, [])

    def test_fresh_follower_opt_out_then_stale_is_required_offline(self):
        clock = self.clock; broker = Broker(clock=clock)
        def exchange(op, member, **values):
            return broker.exchange(dict(op=op, member=member, group='shared', **values))
        selected, target = 'a' * 64, 'b' * 64
        exchange('heartbeat', 'one', selected=selected, eligible=[target], ready=True)
        exchange('follow', 'two')
        proposal = dict(current=selected, target=target, purpose='upgrade', required_members=['one', 'two'])
        reply = exchange('propose', 'one', **proposal)
        self.assertEqual(reply['status'], 'ok')
        self.assertEqual(reply['plan']['participants'], ['one'])
        exchange('abort', 'one', id=reply['plan']['id'])
        clock.now += 46
        exchange('heartbeat', 'one', selected=selected, eligible=[target], ready=True)
        reply = exchange('propose', 'one', **proposal)
        self.assertEqual(reply['reason'], 'required_member_offline')

    def test_away_and_dry_run_do_not_contact_broker(self):
        clock = self.clock; transport = Transport(Broker(clock=clock))
        for home, enabled in ((False, True), (True, False)):
            _, guard = instance(clock, 'away', transport, home=home, enabled=enabled, required=['away'])
            guard.api = self.api; guard.config['follow_parent_groups'] = ['Parent']
            self.api.current = 'Parent'
            before = len(transport.calls)
            self.assertEqual(step(clock, self.api, guard, clock.now)['event'], 'following_parent')
            self.assertEqual(len(transport.calls), before)


if __name__ == '__main__': unittest.main()

class ReturningFollowerTests(unittest.TestCase):
    def test_returning_independent_member_aborts_prepared_and_committed_plans(self):
        for commit in (False, True):
            clock = Clock(); broker = Broker(clock=clock)
            def exchange(op, member, **values):
                return broker.exchange(dict(op=op, member=member, group='shared', **values))
            current, target = 'a' * 64, 'b' * 64
            hb = dict(selected=current, eligible=[target], ready=True)
            exchange('heartbeat', 'one', **hb)
            exchange('follow', 'two')
            plan = exchange('propose', 'one', current=current, target=target, purpose='upgrade', required_members=['one', 'two'])['plan']
            if commit:
                plan = exchange('ack', 'one', id=plan['id'], ready=True)['plan']
                self.assertEqual(plan['status'], 'committed')
            plan = exchange('heartbeat', 'two', **hb)['plan']
            self.assertEqual(plan['status'], 'aborted')
            self.assertEqual(plan['reason'], 'member_joined')
