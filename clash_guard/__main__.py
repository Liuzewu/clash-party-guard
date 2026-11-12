import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from .api import Controller, ControllerError
from .config import load
from .core import Guard


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
    directory = Path(args.state_dir).expanduser() if args.state_dir else Path.home() / '.local' / 'state' / 'clash-party-guard' / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'state.json'
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('state must be an object')
    else:
        state = {}
    guard = Guard(api, cfg, state, switch=args.enable_switch, sleep=time.sleep, clock=time.time)
    while True:
        guard.quality = {}
        if cfg.get('quality_file'):
            try:
                evidence = json.loads(Path(cfg['quality_file']).expanduser().read_text(encoding='utf-8'))
                if isinstance(evidence, dict) and isinstance(evidence.get('nodes'), dict):
                    guard.quality = evidence['nodes']
            except (OSError, ValueError, TypeError):
                # Absent external evidence stays unknown; fault checks continue.
                pass
        result = guard.step(time.time())
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


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (ControllerError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print('clash_guard:', error, file=sys.stderr)
        raise SystemExit(1)
