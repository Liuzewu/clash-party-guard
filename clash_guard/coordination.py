"""Coordinated decisions over an existing authenticated command transport.

Only caller-provided endpoint fingerprints travel between devices. A broker
is a short-lived stdin/stdout process, typically invoked through SSH; it never
opens a network listener or reads proxy subscriptions.
"""
import argparse
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from .identity import identity_key, load_nodes, node_fingerprints

LEASE = 45
PREPARE_TTL = 30
COMMIT_DELAY = 2
COMMIT_WINDOW = 20
TERMINAL = {'done', 'aborted', 'expired'}


def _fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _name(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 120


class Broker:
    """Serialize prepare/ack/commit decisions for independent service groups."""
    def __init__(self, state=None, clock=time.time):
        self.state = state if state is not None else {'version': 1, 'members': {}, 'plans': {}}
        if (not isinstance(self.state, dict) or self.state.get('version') != 1 or
                not isinstance(self.state.get('members'), dict) or not isinstance(self.state.get('plans'), dict)):
            raise ValueError('invalid broker state')
        self.clock = clock

    def _online(self, group, member, now):
        value = self.state['members'].get(group, {}).get(member)
        return bool(value and value.get('mode') != 'following' and 0 <= now - value['seen'] <= LEASE)

    def _following(self, group, member, now):
        value = self.state['members'].get(group, {}).get(member)
        return bool(value and value.get('mode') == 'following' and 0 <= now - value['seen'] <= LEASE)

    def _expire(self, group, now):
        plan = self.state['plans'].get(group)
        if not plan or plan['status'] in TERMINAL:
            return
        deadline = plan['prepare_until'] if plan['status'] == 'preparing' else plan['execute_until']
        if now > deadline:
            plan.update(status='expired', reason='deadline')
            return
        for member in plan['participants']:
            if not self._online(group, member, now):
                plan.update(status='aborted', reason='member_offline'); return
            state = self.state['members'][group][member]
            valid_selection = (state['selected'] == plan['expected'][member] or
                               member in plan['applied'] and state['selected'] == plan['target'])
            if not state['ready'] or not valid_selection or plan['target'] not in state['eligible']:
                plan.update(status='aborted', reason='member_changed'); return

    def exchange(self, request):
        try:
            if not isinstance(request, dict) or not _name(request.get('group')) or not _name(request.get('member')):
                raise ValueError('invalid request')
            now = self.clock(); group = request['group']; member = request['member']; op = request.get('op')
            if op == 'heartbeat':
                selected, eligible, ready = request.get('selected'), request.get('eligible'), request.get('ready')
                if (not _fingerprint(selected) or not isinstance(eligible, list) or len(eligible) > 1000 or
                        any(not _fingerprint(item) for item in eligible) or not isinstance(ready, bool)):
                    raise ValueError('invalid heartbeat')
                plan = self.state['plans'].get(group)
                if plan and plan['status'] not in TERMINAL and member not in plan['participants']:
                    plan.update(status='aborted', reason='member_joined')
                self.state['members'].setdefault(group, {})[member] = {
                    'selected': selected, 'eligible': sorted(set(eligible)), 'ready': ready, 'seen': now}
            elif op == 'follow':
                self.state['members'].setdefault(group, {})[member] = {
                    'mode': 'following', 'seen': now}
            self._expire(group, now)
            plan = self.state['plans'].get(group)
            if op in ('heartbeat', 'poll', 'follow'):
                return {'status': 'ok', 'server_now': now, 'plan': copy.deepcopy(plan)}
            if op == 'propose':
                current, target, purpose = request.get('current'), request.get('target'), request.get('purpose')
                required = request.get('required_members', [])
                if (not _fingerprint(current) or not _fingerprint(target) or current == target or
                        purpose not in ('switch', 'upgrade', 'recover') or not isinstance(required, list) or
                        any(not _name(v) for v in required) or len(set(required)) != len(required) or member not in required):
                    raise ValueError('invalid proposal')
                if plan and plan['status'] not in TERMINAL:
                    return {'status': 'busy', 'server_now': now, 'plan': copy.deepcopy(plan)}
                if any(not self._online(group, v, now) and not self._following(group, v, now) for v in required):
                    return {'status': 'rejected', 'reason': 'required_member_offline', 'server_now': now}
                participants = sorted(v for v in self.state['members'].get(group, {}) if self._online(group, v, now))
                if any(not self.state['members'][group][v]['ready'] or
                       (self.state['members'][group][v]['selected'] != current and
                        not (purpose == 'recover' and self.state['members'][group][v]['selected'] == target)) or
                       target not in self.state['members'][group][v]['eligible'] for v in participants):
                    return {'status': 'rejected', 'reason': 'member_unready', 'server_now': now}
                plan = {'id': uuid.uuid4().hex, 'group': group, 'proposer': member, 'purpose': purpose,
                        'current': current, 'target': target, 'participants': participants,
                        'expected': {v: self.state['members'][group][v]['selected'] for v in participants},
                        'acks': [], 'applied': [], 'status': 'preparing', 'prepare_until': now + PREPARE_TTL}
                self.state['plans'][group] = plan
                return {'status': 'ok', 'server_now': now, 'plan': copy.deepcopy(plan)}
            if not plan or request.get('id') != plan['id'] or member not in plan['participants']:
                return {'status': 'rejected', 'reason': 'unknown_plan', 'server_now': now}
            if plan['status'] in TERMINAL:
                return {'status': 'ok', 'server_now': now, 'plan': copy.deepcopy(plan)}
            if op == 'abort':
                plan.update(status='aborted', reason='participant_cancelled')
            elif op == 'ack' and plan['status'] == 'preparing':
                if request.get('ready') is not True:
                    plan.update(status='aborted', reason='candidate_unready')
                else:
                    if member not in plan['acks']: plan['acks'].append(member)
                    if set(plan['acks']) == set(plan['participants']):
                        plan.update(status='committed', commit_at=now + COMMIT_DELAY,
                                    execute_until=now + COMMIT_DELAY + COMMIT_WINDOW)
            elif op == 'applied' and plan['status'] == 'committed':
                if not plan['commit_at'] <= now <= plan['execute_until']:
                    return {'status': 'rejected', 'reason': 'outside_window', 'server_now': now}
                if member not in plan['applied']: plan['applied'].append(member)
                self.state['members'][group][member]['selected'] = plan['target']
                if set(plan['applied']) == set(plan['participants']): plan['status'] = 'done'
            else:
                return {'status': 'rejected', 'reason': 'invalid_phase', 'server_now': now}
            return {'status': 'ok', 'server_now': now, 'plan': copy.deepcopy(plan)}
        except (TypeError, KeyError, ValueError):
            return {'status': 'rejected', 'reason': 'invalid_request', 'server_now': self.clock()}


@contextlib.contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        if not os.fstat(stream.fileno()).st_size: stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def exchange_file(path, request, clock=time.time):
    path = Path(path).expanduser()
    with _lock(path.with_suffix(path.suffix + '.lock')):
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        broker = Broker(state, clock=clock)
        response = broker.exchange(request)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(path.parent), delete=False) as f:
                name = f.name
                if os.name != 'nt': os.fchmod(f.fileno(), 0o600)
                json.dump(broker.state, f, ensure_ascii=False, separators=(',', ':'))
                f.flush(); os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if name and os.path.exists(name): os.unlink(name)
        return response


class CommandTransport:
    def __init__(self, command, timeout=8):
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError('coordination command must be an argument list')
        self.command, self.timeout = list(command), timeout

    def exchange(self, request):
        try:
            result = subprocess.run(self.command, input=json.dumps(request), capture_output=True,
                                    text=True, timeout=self.timeout, check=True)
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or data.get('status') not in ('ok', 'busy', 'rejected'):
                raise ValueError('invalid response')
            return data
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            raise ConnectionError('coordination unavailable') from e


def validate_coordination_config(config, *, presence_override=False, transport_override=False):
    """Validate syntax only; do not read node data or execute any command."""
    if not isinstance(config, dict) or not isinstance(config.get('enabled', True), bool):
        raise ValueError('coordination must be an object with a boolean enabled flag')
    if config.get('enabled') is False:
        return
    if not _name(config.get('member')):
        raise ValueError('coordination requires member')
    if config.get('group_id') is not None and not _name(config['group_id']):
        raise ValueError('coordination group_id must be a nonempty name')
    if 'fixed_home' in config and not isinstance(config['fixed_home'], bool):
        raise ValueError('fixed_home must be a boolean')
    required = config.get('required_members')
    if (not isinstance(required, list) or config['member'] not in required or
            len(required) > 64 or any(not _name(v) for v in required) or len(set(required)) != len(required)):
        raise ValueError('required_members must contain this member')
    wait = config.get('failure_wait', 20)
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= PREPARE_TTL:
        raise ValueError('failure_wait must be between 0 and 30 seconds')
    sources = [key for key in ('node_config_path', 'identity_command', 'identity_file', 'node_ids') if config.get(key)]
    if len(sources) != 1:
        raise ValueError('coordination requires exactly one node identity source')
    for key in ('node_config_path', 'identity_file'):
        if key in config and (not isinstance(config[key], str) or not config[key]):
            raise ValueError(key + ' must be a nonempty path')
    for key in ('command', 'identity_command', 'presence_command'):
        if key in config and (not isinstance(config[key], list) or not config[key] or
                              any(not isinstance(x, str) or not x for x in config[key])):
            raise ValueError(key + ' must be an argument list')
    if not transport_override and 'command' not in config:
        raise ValueError('coordination requires a command')
    if 'node_ids' in config:
        ids = config['node_ids']
        if (not isinstance(ids, dict) or any(not _name(k) or not _fingerprint(v) for k, v in ids.items()) or
                len(set(ids.values())) != len(ids)):
            raise ValueError('node_ids must map names to unique fingerprints')
    if config.get('identity_key_env') and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(config['identity_key_env'])):
        raise ValueError('identity_key_env must name an environment variable')
    if config.get('fixed_home') is not True and not presence_override and not config.get('presence_command'):
        raise ValueError('coordination requires a presence command or fixed_home')


class Coordination:
    """Optional home coordination for one guard; local fault recovery wins.

    ``node_ids`` or ``identity_file`` maps local selector names to SHA-256
    endpoint fingerprints. Names and subscription data are never transmitted.
    ``presence_command`` must return JSON ``{\"home\": true|false}``; an
    unavailable detector is unknown, not affirmative evidence of being away.
    """
    def __init__(self, config, transport=None, presence=None):
        validate_coordination_config(config, presence_override=presence is not None,
                                     transport_override=transport is not None)
        self.config = config
        self.transport = transport or CommandTransport(config.get('command'))
        self.presence = presence
        self.pending = None
        self.started = None
        self.upgrade_evidence = {}

    def _home(self):
        if self.config.get('fixed_home') is True:
            return True
        try:
            if self.presence is not None:
                result = self.presence()
            else:
                value = subprocess.run(self.config['presence_command'], capture_output=True, text=True,
                                       timeout=5, check=True)
                result = json.loads(value.stdout)
            if isinstance(result, dict): result = result.get('home')
            return result if isinstance(result, bool) else None
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return None

    def _ids(self):
        try:
            if self.config.get('node_config_path') or self.config.get('identity_command'):
                ids = node_fingerprints(load_nodes(self.config), identity_key(self.config))
            elif self.config.get('identity_file'):
                ids = json.loads(Path(self.config['identity_file']).expanduser().read_text(encoding='utf-8'))
            else:
                ids = self.config.get('node_ids', {})
            if (not isinstance(ids, dict) or any(not _name(k) or not _fingerprint(v) for k, v in ids.items()) or
                    len(set(ids.values())) != len(ids)):
                return {}
            return ids
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            return {}

    def _request(self, guard, op, **values):
        return self.transport.exchange({'op': op, 'member': self.config['member'],
                                        'group': self.config.get('group_id', guard.config['group']), **values})

    def _abort(self, guard, identifier):
        if not identifier: return
        self.upgrade_evidence.pop(identifier, None)
        if self._home() is not True:
            return
        try: self._request(guard, 'abort', id=identifier)
        except (ConnectionError, OSError, ValueError): pass

    def _snapshot(self, guard, now, ids):
        proxies = guard.api.proxies(); group = guard._group(proxies); current = group['now']
        mapped = ids.get(current)
        if not mapped: return None
        last = guard.state.get('last_step')
        gap = isinstance(last, (int, float)) and (now < last or now - last > max(120, guard.config['normal_interval'] * 2))
        ready = (guard.state.get('selected') == current and not gap and
                 guard.identity_available and not guard.identity_changed and
                 now >= guard.state.get('grace_until', 0) and
                 now >= self.started + guard.config['startup_grace'])
        available = [ids[name] for name in group['all'] if name in ids and guard._allowed(name, proxies, guard._now(now))]
        return {'current': current, 'selected': mapped, 'eligible': available, 'ready': ready}

    def _heartbeat(self, guard, snapshot):
        return self._request(guard, 'heartbeat', selected=snapshot['selected'],
                             eligible=snapshot['eligible'], ready=bool(snapshot['ready']))

    def follow(self, guard):
        """Explicitly suspend participation while a user-selected parent is active."""
        self.pending = None
        self.upgrade_evidence.clear()
        if self._home() is not True:
            return
        try:
            self._request(guard, 'follow')
        except (ConnectionError, OSError, ValueError):
            pass

    def _local_fallback(self, guard, now, pending):
        current, target = pending['current'], pending['target']
        self._abort(guard, pending.get('id'))
        self.pending = None
        if guard._probe(current, now):
            guard.state.update(failures=0, failure_since=None)
            return {'event': 'current_recovered', 'selected': current, 'wait': guard.config['normal_interval']}
        if guard._verified(target, current, now) and not guard._probe(current, now):
            event = guard._switch_local(current, target, now, 'switch')
            return {'event': event, 'selected': current, 'candidate': target, 'wait': guard.config['failure_interval']}
        return {'event': 'coordination_candidate_unready', 'selected': current, 'wait': guard.config['failure_interval']}

    def tick(self, guard, now):
        if not guard.switch:
            return None
        if self.started is None: self.started = now
        home = self._home()
        if home is not True:
            self.pending = None
            self.upgrade_evidence.clear()
            return None
        ids = self._ids()
        snapshot = self._snapshot(guard, now, ids)
        if snapshot is None:
            self.pending = None
            return None
        try:
            response = self._heartbeat(guard, snapshot)
        except (ConnectionError, OSError, ValueError):
            if self.pending and self.pending['purpose'] == 'switch':
                return self._local_fallback(guard, now, self.pending)
            self.pending = None
            return None
        plan = response.get('plan')
        if not plan or self.config['member'] not in plan.get('participants', []):
            self.pending = None
            return None
        identifier = plan.get('id')
        if plan.get('status') in TERMINAL:
            if self.pending and self.pending.get('id') == identifier and self.pending['purpose'] == 'switch':
                return self._local_fallback(guard, now, self.pending)
            if self.pending and self.pending.get('id') == identifier and self.pending['purpose'] == 'upgrade':
                guard.state['wins'].pop(self.pending['target'], None)
            self.upgrade_evidence.pop(identifier, None)
            self.pending = None
            return None
        expected = plan.get('expected', {}).get(self.config['member'])
        if (not snapshot['ready'] or snapshot['selected'] != expected or
                plan.get('target') not in snapshot['eligible']):
            self._abort(guard, identifier)
            self.pending = None
            return None
        reverse = {value: key for key, value in ids.items()}
        current, target = snapshot['current'], reverse.get(plan.get('target'))
        if not target:
            self._abort(guard, identifier); self.pending = None
            return None
        is_proposer = plan.get('proposer') == self.config['member']
        if is_proposer and self.pending and self.pending.get('id') == identifier and plan['purpose'] == 'switch':
            if guard._probe(current, now):
                self._abort(guard, identifier); self.pending = None
                guard.state.update(failures=0, failure_since=None)
                return {'event': 'current_recovered', 'selected': current, 'wait': guard.config['normal_interval']}
            if now - self.pending['started'] >= self.config.get('failure_wait', 20):
                return self._local_fallback(guard, now, self.pending)
        if plan['status'] == 'preparing' and self.config['member'] not in plan.get('acks', []):
            current_ok = guard._probe(current, now) if plan['purpose'] == 'upgrade' else True
            verified = current_ok and guard._verified(target, current, now)
            home = self._home()
            if not verified or home is not True:
                if home is True:
                    self._abort(guard, identifier)
                self.pending = None
                return {'event': 'coordination_candidate_unready', 'selected': current, 'wait': guard.config['normal_interval']}
            if plan['purpose'] == 'upgrade':
                guard._refresh_quality()
                if not guard._upgrade_ready(current, target, guard._now(now)):
                    self._abort(guard, identifier)
                    return {'event': 'upgrade_cancelled', 'selected': current, 'wait': guard.config['normal_interval']}
                self.upgrade_evidence[identifier] = guard._quality_snapshot(current, target, guard._now(now))
            try: response = self._request(guard, 'ack', id=identifier, ready=True)
            except (ConnectionError, OSError, ValueError):
                return {'event': 'coordination_unavailable', 'selected': current, 'wait': guard.config['failure_interval']}
            plan = response.get('plan') or {}
        if plan.get('status') == 'committed' and self.config['member'] not in plan.get('applied', []):
            server_now = response.get('server_now')
            if not isinstance(server_now, (int, float)) or server_now < plan['commit_at']:
                return {'event': 'coordination_pending', 'selected': current, 'wait': min(guard.config['failure_interval'], 2)}
            baseline = self.upgrade_evidence.get(identifier)
            if plan['purpose'] == 'upgrade':
                guard._refresh_quality()
                if (baseline is None or not guard._quality_not_worse(baseline, guard._quality_snapshot(current, target, guard._now(now))) or
                        not guard._probe(current, now) or not guard._upgrade_ready(current, target, guard._now(now))):
                    self._abort(guard, identifier); self.pending = None
                    guard.state['wins'].pop(target, None)
                    return {'event': 'upgrade_cancelled', 'selected': current, 'wait': guard.config['normal_interval']}
            verified = guard._verified(target, current, now)
            home = self._home()
            if not verified or home is not True:
                if home is True:
                    self._abort(guard, identifier)
                self.pending = None
                return {'event': 'coordination_candidate_unready', 'selected': current, 'wait': guard.config['normal_interval']}
            if is_proposer and plan['purpose'] == 'switch' and guard._probe(current, guard._now(now)):
                self._abort(guard, identifier); self.pending = None
                guard.state.update(failures=0, failure_since=None)
                return {'event': 'current_recovered', 'selected': current, 'wait': guard.config['normal_interval']}
            fresh = self._snapshot(guard, guard._now(now), self._ids())
            if not fresh or not fresh['ready'] or fresh['selected'] != expected:
                self._abort(guard, identifier); self.pending = None
                return {'event': 'coordination_cancelled', 'selected': current, 'wait': guard.config['normal_interval']}
            try: check = self._heartbeat(guard, fresh)
            except (ConnectionError, OSError, ValueError):
                if self.pending and plan['purpose'] == 'switch': return self._local_fallback(guard, now, self.pending)
                self.pending = None
                return {'event': 'coordination_unavailable', 'selected': current, 'wait': guard.config['normal_interval']}
            latest = check.get('plan') or {}
            if (latest.get('id') != identifier or latest.get('status') != 'committed' or
                    not latest['commit_at'] <= check.get('server_now', 0) <= latest['execute_until']):
                self.pending = None
                return {'event': 'coordination_cancelled', 'selected': current, 'wait': guard.config['normal_interval']}
            result = (plan['purpose'] if current == target else
                      guard._switch_local(current, target, guard._now(now), plan['purpose'], quality_baseline=baseline))
            self.pending = None
            if result != plan['purpose']:
                self._abort(guard, identifier)
                return {'event': result, 'selected': current, 'wait': guard.config['normal_interval']}
            try: acknowledged = self._request(guard, 'applied', id=identifier)
            except (ConnectionError, OSError, ValueError): acknowledged = {'status': 'unavailable'}
            self.upgrade_evidence.pop(identifier, None)
            return {'event': 'coordinated_' + result if acknowledged.get('status') == 'ok' else 'coordination_unconfirmed',
                    'selected': target, 'wait': guard.config['normal_interval']}
        return {'event': 'coordination_pending', 'selected': current, 'wait': min(guard.config['failure_interval'], 2)}

    def propose(self, guard, current, candidate, now, purpose, quality_baseline=None):
        if not guard.switch:
            return None
        home = self._home()
        if home is False: return None
        if home is not True: return None if purpose == 'switch' else 'coordination_unavailable'
        ids = self._ids()
        if current not in ids or candidate not in ids:
            return None if purpose == 'switch' else 'coordination_unavailable'
        if purpose == 'upgrade':
            guard._refresh_quality()
            if (not guard._quality_not_worse(quality_baseline, guard._quality_snapshot(current, candidate, guard._now(now))) or
                    not guard._upgrade_ready(current, candidate, guard._now(now))):
                return 'upgrade_cancelled'
        try:
            response = self._request(guard, 'propose', current=ids[current], target=ids[candidate], purpose=purpose,
                                     required_members=self.config['required_members'])
            plan = response.get('plan') or {}
            if response.get('status') != 'ok' or plan.get('proposer') != self.config['member']:
                return None if purpose == 'switch' else 'coordination_unavailable'
            self.pending = {'id': plan['id'], 'current': current, 'target': candidate,
                            'purpose': purpose, 'started': now}
            if purpose == 'upgrade':
                guard._refresh_quality()
                current_quality = guard._quality_snapshot(current, candidate, guard._now(now))
                if (not guard._quality_not_worse(quality_baseline, current_quality) or
                        not guard._upgrade_ready(current, candidate, guard._now(now))):
                    self._abort(guard, plan['id']); self.pending = None
                    return 'upgrade_cancelled'
                self.upgrade_evidence[plan['id']] = current_quality
            ack = self._request(guard, 'ack', id=plan['id'], ready=True)
            if ack.get('status') != 'ok':
                self.pending = None
                return None if purpose == 'switch' else 'coordination_unavailable'
            return 'coordination_pending'
        except (ConnectionError, OSError, ValueError, KeyError, TypeError):
            self.pending = None
            return None if purpose == 'switch' else 'coordination_unavailable'


def main():
    parser = argparse.ArgumentParser(description='Exchange one coordination message using a private local state file.')
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        print(json.dumps(exchange_file(args.state, request), ensure_ascii=False, separators=(',', ':')))
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({'status': 'unavailable'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
