"""Selector monitoring, fallback, recovery and evidence-based review."""
import datetime as dt
import re
import statistics
from .api import ControllerError

VIRTUAL = {'selector', 'urltest', 'fallback', 'loadbalance', 'relay', 'smart', 'direct', 'reject', 'rejectdrop', 'compatible', 'pass', 'dns'}


def _stamp(value):
    value = re.sub(r'(\.\d{6})\d+(?=(?:Z|[+-]\d\d:\d\d)$)', r'\1', value)
    date = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('timestamp has no timezone')
    return date.timestamp()


def eligible(name, proxies, now, health_url, excluded, max_age=300):
    node = proxies.get(name, {})
    kind = re.sub(r'[-_ ]', '', str(node.get('type', ''))).lower()
    if not kind or kind in VIRTUAL or 'all' in node or node.get('name') != name:
        return False
    if any(re.search(pattern, name, re.I) for pattern in excluded):
        return False
    try:
        evidence = node['extra'][health_url]
        last = evidence['history'][-1]
        delay, age = last['delay'], now - _stamp(last['time'])
        return evidence.get('alive') is True and isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay > 0 and 0 <= age <= max_age
    except (KeyError, TypeError, IndexError, ValueError):
        return False


def metrics(samples, now, window=30 * 86400):
    """Aggregate request counts and latency sums from bounded half-hour buckets."""
    valid = []
    for row in samples:
        if len(row) == 4:
            stamp, count, successes, total_ms = row
        elif len(row) == 2:  # legacy example fixtures
            stamp, value = row
            count, successes, total_ms = 1, int(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0), value or 0
        else:
            continue
        if 0 <= now - stamp <= window:
            valid.append((stamp, count, successes, total_ms))
    if not valid:
        return {'count': 0, 'success_rate': None, 'mean_ms': None, 'span': 0}
    count = sum(row[1] for row in valid)
    successes = sum(row[2] for row in valid)
    return {'count': count, 'success_rate': successes / count if count else None,
            'mean_ms': sum(row[3] for row in valid) / successes if successes else None,
            'span': max(row[0] for row in valid) - min(row[0] for row in valid)}


def quality_score(name, reports, now, max_age=86400):
    """Accept a user-provided 0-100 score while it is fresh; missing is neutral."""
    item = reports.get(name, {}) if isinstance(reports, dict) else {}
    try:
        score, observed = item['score'], item['observed_at']
        if isinstance(observed, str):
            observed = _stamp(observed)
        if (isinstance(score, bool) or not isinstance(score, (int, float)) or
                not 0 <= score <= 100 or isinstance(observed, bool) or
                not isinstance(observed, (int, float)) or not 0 <= now - observed <= max_age):
            return None
        return float(score)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def rank(names, history, now, quality=None, quality_max_age=86400):
    """Sort by success rate, comparable fresh quality, then mean delay."""
    tiers = {}
    for index, name in enumerate(names):
        m = metrics(history.get(name, []), now)
        tier = (m['success_rate'] is None, -(m['success_rate'] or 0))
        tiers.setdefault(tier, []).append((index, name, m, quality_score(name, quality or {}, now, quality_max_age)))
    result = []
    for tier in sorted(tiers):
        items = tiers[tier]
        comparable = all(item[3] is not None for item in items)
        def key(item):
            index, name, m, score = item
            delay = m['mean_ms'] if m['mean_ms'] is not None else float('inf')
            return ((-score,) if comparable else ()) + (delay, index)
        result.extend(item[1] for item in sorted(items, key=key))
    return result


class Guard:
    def __init__(self, controller, config, state=None, *, switch=False, sleep=None, clock=None):
        self.api, self.config, self.switch = controller, config, switch
        self.state = state if isinstance(state, dict) else {}
        self.sleep = sleep or (lambda _: None)
        self.clock = clock
        self.quality = {}
        self.state.setdefault('samples', {})
        self.state.setdefault('wins', {})
        self.state.setdefault('failures', 0)

    def _now(self, fallback):
        return self.clock() if self.clock is not None else fallback

    def _group(self, proxies):
        group = proxies.get(self.config['group'], {})
        if group.get('type') != 'Selector' or not isinstance(group.get('all'), list) or not isinstance(group.get('now'), str):
            raise ControllerError('selector unavailable')
        return group

    def _probe(self, name, now):
        delays = [self.api.probe(name, target) for target in self.config['targets']]
        ok = all(isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0 for d in delays)
        value = sum(delays) / len(delays) if ok else None
        samples = self.state['samples'].setdefault(name, [])
        bucket = int(now // 1800) * 1800
        if samples and len(samples[-1]) == 4 and samples[-1][0] == bucket:
            row = samples[-1]
            row[1] += 1
            row[2] += int(ok)
            row[3] += value if ok else 0
        else:
            samples.append([bucket, 1, int(ok), value if ok else 0])
        self.state['samples'][name] = [s for s in samples if 0 <= now - s[0] <= 30 * 86400][-1441:]
        return ok

    def _allowed(self, name, proxies, now):
        return eligible(name, proxies, now, self.config.get('health_url', 'https://www.gstatic.com/generate_204'), self.config['excluded_name_patterns'], self.config.get('health_max_age', 300))

    def _verified(self, candidate, current, now):
        for attempt in range(2):
            proxies = self.api.proxies(); group = self._group(proxies)
            if group['now'] != current or candidate not in group['all'] or not self._allowed(candidate, proxies, self._now(now)):
                return False
            if not self._probe(candidate, now):
                return False
            if attempt == 0:
                self.sleep(3)
        return True

    def _switch(self, current, candidate, now, event):
        proxies = self.api.proxies()
        group = self._group(proxies)
        if group['now'] != current or candidate not in group['all'] or not self._allowed(candidate, proxies, self._now(now)):
            return 'selection_changed'
        if not self.switch:
            return 'would_' + event
        self.api.select(self.config['group'], candidate)
        if self._group(self.api.proxies())['now'] != candidate:
            raise ControllerError('selection not confirmed')
        self.state.update(selected=candidate, selected_since=self._now(now), failures=0, failure_since=None, recovery_since=None)
        self.state['wins'] = {}
        if event == 'upgrade':
            self.state['primary'] = candidate
        return event

    def step(self, now):
        try:
            return self._step(now)
        except (ControllerError, OSError, ValueError, KeyError, TypeError, IndexError):
            self.state['failures'] = 0
            self.state['failure_since'] = None
            self.state['recovery_since'] = None
            return {'event': 'monitor_error', 'wait': self.config['normal_interval']}

    def _step(self, now):
        proxies = self.api.proxies(); group = self._group(proxies); current = group['now']
        last = self.state.get('last_step')
        self.state['last_step'] = now
        if isinstance(last, (int, float)) and (now < last or now - last > max(120, self.config['normal_interval'] * 2)):
            self.state.update(failures=0, failure_since=None, recovery_since=None, grace_until=now + self.config['startup_grace'])
        if current not in group['all'] or current not in proxies or 'all' in proxies[current]:
            raise ControllerError('selected item is not a node')
        if not self.state.get('selected') or self.state['selected'] != current:
            self.state.update(selected=current, primary=current, selected_since=now, failures=0, failure_since=None, recovery_since=None, grace_until=now + self.config['startup_grace'], wins={})
            return {'event': 'selection_grace', 'wait': self.config['normal_interval'], 'selected': current}
        if now < self.state.get('grace_until', 0):
            return {'event': 'selection_grace', 'wait': self.config['normal_interval'], 'selected': current}
        if not self._probe(current, now):
            if not self.state.get('failures'):
                self.state['failure_since'] = now
            self.state['failures'] += 1
            if self.state['failures'] < 3 or now - self.state['failure_since'] < self.config['minimum_failure_duration']:
                return {'event': 'node_failed', 'wait': self.config['failure_interval'], 'selected': current}
            names = [name for name in group['all'] if name != current and self._allowed(name, proxies, now)]
            ordered = rank(names, self.state['samples'], now, self.quality, self.config.get('quality_max_age', 86400))
            start = self.state.get('candidate_cursor', 0) % len(ordered) if ordered else 0
            ordered = ordered[start:] + ordered[:start]
            for candidate in ordered[:4]:
                self.state['candidate_cursor'] = (self.state.get('candidate_cursor', 0) + 1) % len(ordered)
                if not self._verified(candidate, current, now):
                    continue
                if self._probe(current, now):
                    self.state.update(failures=0, failure_since=None)
                    return {'event': 'current_recovered', 'wait': self.config['normal_interval'], 'selected': current}
                latest = self.api.proxies()
                if not self._allowed(candidate, latest, self._now(now)):
                    continue
                event = self._switch(current, candidate, now, 'switch')
                return {'event': event, 'wait': self.config['normal_interval'], 'selected': current, 'candidate': candidate}
            return {'event': 'no_candidate', 'wait': self.config['failure_interval'], 'selected': current}
        self.state.update(failures=0, failure_since=None)
        primary = self.state.get('primary', current)
        if primary != current and primary in group['all'] and self._allowed(primary, proxies, now):
            if self._probe(primary, now):
                if self.state.get('recovery_since') is None:
                    self.state['recovery_since'] = now
                if now - self.state['recovery_since'] >= self.config['recovery_seconds'] and self._verified(primary, current, now):
                    event = self._switch(current, primary, now, 'recover')
                    return {'event': event, 'wait': self.config['normal_interval'], 'selected': current, 'candidate': primary}
            else:
                self.state['recovery_since'] = None
        elif primary != current:
            self.state['recovery_since'] = None
        names = [name for name in group['all'] if name != current and self._allowed(name, proxies, now)]
        if names and now - self.state.get('last_sample', 0) >= self.config['sampling_interval']:
            index = self.state.get('sample_index', 0) % len(names); candidate = names[index]
            self._probe(candidate, now)
            self.state['sample_index'] = index + 1; self.state['last_sample'] = now
            old, new = metrics(self.state['samples'].get(current, []), now), metrics(self.state['samples'].get(candidate, []), now)
            ready = all(m['count'] >= self.config['min_sample_count'] and m['span'] >= self.config['min_sample_span_hours'] * 3600 and m['mean_ms'] is not None for m in (old, new))
            wins = self.state['wins'].setdefault(candidate, [])
            current_quality = quality_score(current, self.quality, now, self.config.get('quality_max_age', 86400))
            candidate_quality = quality_score(candidate, self.quality, now, self.config.get('quality_max_age', 86400))
            quality_ok = current_quality is None or candidate_quality is None or candidate_quality >= current_quality
            better = ready and quality_ok and new['success_rate'] >= old['success_rate'] and new['mean_ms'] <= old['mean_ms'] * 0.8
            if better:
                # Count durable wins across the review window, not every rapid sample.
                window = self.config['review_span_hours'] * 3600
                wins[:] = [value for value in wins if 0 <= now - value <= max(window * 2, 3600)]
                spacing = max(self.config['sampling_interval'], window / max(1, self.config['review_wins'] - 1))
                if not wins or now - wins[-1] >= spacing:
                    wins.append(now)
                self.state['wins'][candidate] = wins[-100:]
            else:
                self.state['wins'].pop(candidate, None)
            if (better and now - self.state['selected_since'] >= self.config['hold_days'] * 86400
                    and len(wins) >= self.config['review_wins'] and wins[-1] - wins[0] >= self.config['review_span_hours'] * 3600
                    and self._verified(candidate, current, now)):
                event = self._switch(current, candidate, now, 'upgrade')
                return {'event': event, 'wait': self.config['normal_interval'], 'selected': current, 'candidate': candidate}
        return {'event': 'healthy', 'wait': self.config['normal_interval'], 'selected': current}
