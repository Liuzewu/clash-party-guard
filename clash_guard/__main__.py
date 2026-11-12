import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from .api import Controller, ControllerError
from .config import load
from .core import Guard, eligible
from .instance_lock import InstanceLock, InstanceRunning


def main(argv=None):
    parser = argparse.ArgumentParser(description='Monitor a Mihomo selector and choose a verified backup when necessary.')
    parser.add_argument('--config', required=True, help='path to a JSON configuration')
    parser.add_argument('--state-dir', help='directory for local observation state')
    parser.add_argument('--once', action='store_true', help='run one step and exit')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--read-only', action='store_true', help='read current selector without probes or state writes')
    modes.add_argument('--dry-run', action='store_true', help='probe but do not change selection (default)')
    modes.add_argument('--enable-switch', action='store_true', help='allow selector changes')
    args = parser.parse_args(argv)
    cfg = load(args.config)
    api = Controller(cfg['controller'], cfg.get('secret_env', 'CLASH_CONTROLLER_SECRET'))
    if args.read_only:
        group = api.proxies().get(cfg['group'], {})
        if group.get('type') != 'Selector' or not isinstance(group.get('all'), list):
            raise ControllerError('selector unavailable')
        print(json.dumps({'event': 'read_only', 'group': cfg['group'], 'selected': group.get('now'), 'candidates': len(group['all'])}, ensure_ascii=False))
        return 0
    name = hashlib.sha256(cfg['group'].encode('utf-8')).hexdigest()[:16]
    default_name = name if args.enable_switch else name + '-preview'
    directory = Path(args.state_dir).expanduser() if args.state_dir else Path.home() / '.local' / 'state' / 'clash-party-guard' / default_name
    directory.mkdir(parents=True, exist_ok=True)
    with InstanceLock(directory):
        return _run(args, cfg, api, directory)


def _run(args, cfg, api, directory):
    path = directory / 'state.json'
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('state must be an object')
    else:
        state = {}
    settings = cfg.get('ip_quality')
    if settings is not None and (not isinstance(settings, dict) or not isinstance(settings.get('enabled', False), bool)):
        raise ValueError('ip_quality must be an object with a boolean enabled flag')
    coordination_config = cfg.get('coordination')
    if coordination_config is not None and (not isinstance(coordination_config, dict) or not isinstance(coordination_config.get('enabled', True), bool)):
        raise ValueError('coordination must be an object with a boolean enabled flag')
    coordinator = None
    if args.enable_switch and coordination_config and coordination_config.get('enabled', True):
        from .coordination import Coordination
        coordinator = Coordination(coordination_config)
    sampler = None
    if settings and settings.get('enabled'):
        from .ip_quality import QualitySampler
        sampler = QualitySampler(settings, directory)
    guard = Guard(api, cfg, state, switch=args.enable_switch, sleep=time.sleep, clock=time.time, coordination=coordinator)
    identity_sources = [source for source in (settings if settings and settings.get('enabled') else None,
                         coordination_config if coordination_config and coordination_config.get('enabled', True) else None)
                        if source and (source.get('node_config_path') or source.get('identity_command'))]
    if identity_sources:
        from .identity import load_nodes, local_node_fingerprints
        def local_identities():
            values = [local_node_fingerprints(load_nodes(source)) for source in identity_sources]
            if any(value != values[0] for value in values[1:]):
                raise ValueError('node identity sources disagree')
            return values[0]
        guard.node_identity_provider = local_identities
    def manual_quality():
        try:
            evidence = json.loads(Path(cfg['quality_file']).expanduser().read_text(encoding='utf-8'))
            return evidence['nodes'] if isinstance(evidence, dict) and isinstance(evidence.get('nodes'), dict) else {}
        except (OSError, ValueError, TypeError):
            return {}
    # Refresh before final decisions as background observations can invalidate
    # a score after an earlier check. Never substitute old manual data for a
    # missing automatic result.
    guard.quality_provider = sampler.scores if sampler else (manual_quality if cfg.get('quality_file') else None)
    previous_term = None
    if sampler and threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)
        def terminate(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            guard._refresh_quality()
            result = guard.step(time.time())
            if sampler and result['event'] in ('following_parent', 'follow_parent_unavailable'):
                sampler.pause()
            if sampler and not args.once and result['event'] not in ('monitor_error', 'following_parent', 'follow_parent_unavailable'):
                try:
                    proxies = api.proxies()
                    group = proxies.get(cfg['group'], {})
                    names = group.get('all', []) if isinstance(group, dict) else []
                    if isinstance(names, list):
                        now = time.time()
                        names = [name for name in names if isinstance(name, str) and eligible(
                            name, proxies, now, cfg.get('health_url', 'https://www.gstatic.com/generate_204'),
                            cfg['excluded_name_patterns'], cfg.get('health_max_age', 300))]
                        sampler.sample_async(names)
                except ControllerError:
                    pass
            fd, tmp = tempfile.mkstemp(prefix='.guard-', dir=str(directory))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(state, f, ensure_ascii=False)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return 1 if result['event'] == 'monitor_error' else 0
            time.sleep(max(1, result.get('wait', cfg['normal_interval'])))
    finally:
        if sampler:
            sampler.close()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (ControllerError, InstanceRunning, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print('clash_guard:', error, file=sys.stderr)
        raise SystemExit(1)
