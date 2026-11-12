"""Load user supplied, per-service settings."""
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit


def load(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('configuration must be an object')
    if not isinstance(data.get('group'), str) or not data['group'].strip():
        raise ValueError('group must name a selector')
    targets = data.get('targets')
    if not isinstance(targets, list) or len(targets) != 2:
        raise ValueError('provide exactly two targets')
    hosts = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError('invalid target')
        url, status = target.get('url'), target.get('expected')
        if not isinstance(url, str) or not re.fullmatch(r'\d{3}(?:-\d{3})?', str(status)):
            raise ValueError('target requires URL and expected HTTP status')
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password:
            raise ValueError('target URL must be HTTPS without credentials')
        hosts.add(p.hostname)
    if len(hosts) != 2:
        raise ValueError('targets must use different hosts')
    parents = data.setdefault('follow_parent_groups', [])
    if (not isinstance(parents, list) or any(not isinstance(value, str) or not value.strip() for value in parents)
            or len(set(parents)) != len(parents) or data['group'] in parents):
        raise ValueError('follow_parent_groups must contain distinct parent names other than the current group')
    for field, default in [('normal_interval', 15), ('failure_interval', 5), ('minimum_failure_duration', 30), ('startup_grace', 60), ('sampling_interval', 120), ('hold_days', 30), ('min_sample_count', 100), ('min_sample_span_hours', 72), ('review_wins', 3), ('review_span_hours', 48), ('recovery_seconds', 1800), ('quality_max_age', 86400)]:
        v = data.setdefault(field, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(field + ' must be a nonnegative number')
    if data['normal_interval'] < 1 or data['failure_interval'] < 1 or data['sampling_interval'] < 1:
        raise ValueError('intervals must be at least 1 second')
    patterns = data.setdefault('excluded_name_patterns', [])
    if not isinstance(patterns, list) or not all(isinstance(v, str) for v in patterns):
        raise ValueError('excluded_name_patterns must be a list of regular expressions')
    for pattern in patterns:
        re.compile(pattern)
    quality_file = data.get('quality_file')
    if quality_file is not None and (not isinstance(quality_file, str) or not quality_file):
        raise ValueError('quality_file must be a path')
    if 'coordination' in data:
        from .coordination import validate_coordination_config
        validate_coordination_config(data['coordination'])
    if 'ip_quality' in data:
        from .ip_quality import validate_settings
        validate_settings(data['ip_quality'])
    data.setdefault('controller', 'http://127.0.0.1:9090')
    data.setdefault('secret_env', 'CLASH_CONTROLLER_SECRET')
    return data
