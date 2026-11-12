"""Load local node data and derive non-reversible comparison identifiers."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import threading


EXCLUDED_FIELDS = {'name', 'interface-name', 'routing-mark'}
MAX_NODE_SOURCE = 4 * 1024 * 1024


def node_fingerprint(node, key=None):
    if not isinstance(node, dict) or not isinstance(node.get('name'), str) or not node['name'].strip():
        raise ValueError('invalid node')
    value = {k: v for k, v in node.items() if k not in EXCLUDED_FIELDS}
    if not value:
        raise ValueError('empty node identity')
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hmac.new(key, raw, hashlib.sha256).hexdigest() if key is not None else hashlib.sha256(raw).hexdigest()


def node_fingerprints(nodes, key=None):
    if not isinstance(nodes, list):
        raise ValueError('proxies must be a list')
    result = {}
    for node in nodes:
        fingerprint = node_fingerprint(node, key)
        if node['name'] in result:
            raise ValueError('duplicate proxy name')
        result[node['name']] = fingerprint
    counts = {}
    for value in result.values(): counts[value] = counts.get(value, 0) + 1
    return {name: value for name, value in result.items() if counts[value] == 1}


def local_node_fingerprints(nodes):
    """Bind local history to every node parameter except its display name."""
    if not isinstance(nodes, list):
        raise ValueError('proxies must be a list')
    result = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get('name'), str) or not node['name'].strip():
            raise ValueError('invalid node')
        name = node['name']
        if name in result:
            raise ValueError('duplicate proxy name')
        value = {k: v for k, v in node.items() if k != 'name'}
        if not value:
            raise ValueError('empty node identity')
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


def identity_key(config):
    name = config.get('identity_key_env')
    if not name: return None
    secret = os.environ.get(name)
    if not secret: raise ValueError('identity key environment variable is unavailable')
    return secret.encode('utf-8')


def _command_output(command):
    """Bound source output without keeping its contents in errors or logs."""
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise ValueError('identity command failed') from error
    chunks, size, exceeded = [], [0], [False]
    def read():
        while True:
            chunk = child.stdout.read(65536)
            if not chunk: break
            size[0] += len(chunk)
            if size[0] > MAX_NODE_SOURCE:
                exceeded[0] = True
                try: child.kill()
                except OSError: pass
                break
            chunks.append(chunk)
    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    try:
        status = child.wait(timeout=5)
    except subprocess.TimeoutExpired as error:
        child.kill(); child.wait()
        worker.join(timeout=1)
        if not worker.is_alive(): child.stdout.close()
        raise ValueError('identity command timed out') from error
    worker.join(timeout=1)
    if not worker.is_alive(): child.stdout.close()
    if worker.is_alive() or exceeded[0] or status != 0:
        raise ValueError('identity command failed or exceeded output limit')
    try: return b''.join(chunks).decode('utf-8')
    except UnicodeDecodeError as error: raise ValueError('identity command output is not UTF-8') from error


def load_nodes(config):
    """Read a local JSON/YAML config or one caller-supplied JSON loader command.

    Returns nodes in memory only. The optional PyYAML parser uses safe_load.
    Raw node values must never be logged or sent to the coordination broker.
    """
    if config.get('node_config_path'):
        with Path(config['node_config_path']).expanduser().open('rb') as source:
            raw = source.read(MAX_NODE_SOURCE + 1)
        if len(raw) > MAX_NODE_SOURCE: raise ValueError('node source exceeds size limit')
        try: text = raw.decode('utf-8')
        except UnicodeDecodeError as error: raise ValueError('node source is not UTF-8') from error
        try: data = json.loads(text)
        except ValueError:
            try: import yaml
            except ImportError as error: raise ValueError('YAML input requires PyYAML') from error
            try: data = yaml.safe_load(text)
            except yaml.YAMLError as error: raise ValueError('invalid YAML node source') from error
    elif config.get('identity_command'):
        command = config['identity_command']
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError('identity_command must be an argument list')
        data = json.loads(_command_output(command))
    else:
        raise ValueError('node_config_path or identity_command is required')
    if not isinstance(data, dict) or not isinstance(data.get('proxies'), list):
        raise ValueError('node source must contain a proxies list')
    return data['proxies']
