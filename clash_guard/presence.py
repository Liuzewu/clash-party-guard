"""Local gateway fingerprint check for the optional coordination presence command."""
import argparse
import ipaddress
import json
import re
import subprocess
import sys


def normalize_mac(value):
    if not isinstance(value, str):
        return None
    parts = re.split('[:-]', value.strip())
    if len(parts) != 6 or any(not re.fullmatch(r'[0-9a-fA-F]{1,2}', p) for p in parts):
        return None
    value = ':'.join(p.zfill(2).lower() for p in parts)
    return value if value not in ('00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff') else None


def _ip(value):
    try:
        address = ipaddress.ip_address(value)
        return str(address) if address.version == 4 and not address.is_unspecified else None
    except ValueError:
        return None


def _rows(text):
    data = json.loads(text.lstrip('\ufeff'))
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


def parse_route(platform, text):
    """Return (gateway, interface) only when all usable defaults agree."""
    routes = set()
    if platform == 'linux':
        for row in _rows(text):
            if isinstance(row, dict) and row.get('dst') in (None, 'default'):
                gateway, interface = _ip(row.get('gateway', '')), row.get('dev')
                if gateway and isinstance(interface, str) and interface:
                    routes.add((gateway, interface))
                else:
                    return None
    elif platform == 'darwin':
        fields = dict(re.findall(r'^\s*(gateway|interface):\s*(\S+)', text, re.MULTILINE))
        gateway, interface = _ip(fields.get('gateway', '')), fields.get('interface')
        if gateway and interface:
            routes.add((gateway, interface))
    elif platform == 'win32':
        for row in _rows(text):
            if isinstance(row, dict):
                gateway, interface = _ip(row.get('NextHop', '')), row.get('InterfaceIndex')
                if gateway and isinstance(interface, int) and not isinstance(interface, bool):
                    routes.add((gateway, str(interface)))
                else:
                    return None
    return next(iter(routes)) if len(routes) == 1 else None


def parse_neighbor(platform, text, gateway, interface):
    found = set()
    if platform == 'linux':
        for row in _rows(text):
            if isinstance(row, dict) and row.get('dst') == gateway and row.get('dev') == interface:
                state = row.get('state', [])
                if isinstance(state, str):
                    state = [state]
                if not isinstance(state, list) or any(str(s).upper() in ('FAILED', 'INCOMPLETE') for s in state):
                    continue
                mac = normalize_mac(row.get('lladdr'))
                if mac:
                    found.add(mac)
    elif platform == 'darwin':
        for ip, mac, dev in re.findall(r'\(([^)]+)\)\s+at\s+(\S+)\s+on\s+(\S+)', text):
            if ip == gateway and dev == interface:
                value = normalize_mac(mac)
                if value:
                    found.add(value)
    elif platform == 'win32':
        for row in _rows(text):
            if isinstance(row, dict) and row.get('IPAddress') == gateway and str(row.get('InterfaceIndex')) == interface:
                mac = normalize_mac(row.get('LinkLayerAddress'))
                if mac:
                    found.add(mac)
    return next(iter(found)) if len(found) == 1 else None


def _run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=1.8).stdout


def detect(gateway, mac, platform=None, run=_run):
    """Return True, False, or None without sending any packets."""
    expected_ip, expected_mac = _ip(gateway), normalize_mac(mac)
    if not expected_ip or not expected_mac:
        raise ValueError('gateway must be IPv4 and mac must be a six-byte MAC address')
    platform = platform or sys.platform
    if platform == 'linux':
        route_cmd = ['ip', '-j', '-4', 'route', 'show', 'default']
    elif platform == 'darwin':
        route_cmd = ['/sbin/route', '-n', 'get', 'default']
    elif platform == 'win32':
        route_cmd = ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                     'Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0 | Select-Object NextHop,InterfaceIndex | ConvertTo-Json -Compress']
    else:
        return None
    try:
        route = parse_route(platform, run(route_cmd))
        if not route:
            return None
        actual_ip, interface = route
        if actual_ip != expected_ip:
            return False
        if platform == 'linux':
            neighbor_cmd = ['ip', '-j', 'neigh', 'show', 'to', actual_ip, 'dev', interface]
        elif platform == 'darwin':
            neighbor_cmd = ['/usr/sbin/arp', '-n', actual_ip]
        else:
            neighbor_cmd = ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            "Get-NetNeighbor -AddressFamily IPv4 -IPAddress '%s' | Where-Object { $_.State -ne 'Unreachable' -and $_.State -ne 'Incomplete' } | Select-Object IPAddress,InterfaceIndex,LinkLayerAddress | ConvertTo-Json -Compress" % actual_ip]
        actual_mac = parse_neighbor(platform, run(neighbor_cmd), actual_ip, interface)
        return actual_mac == expected_mac if actual_mac else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check the local default gateway against a home fingerprint.')
    parser.add_argument('--gateway', required=True, help='home default gateway IPv4 address')
    parser.add_argument('--mac', required=True, help='home gateway MAC address')
    args = parser.parse_args(argv)
    try:
        home = detect(args.gateway, args.mac)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({'home': home}, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
