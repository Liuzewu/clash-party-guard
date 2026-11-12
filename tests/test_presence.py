import json
import subprocess
import unittest

from clash_guard.presence import detect, normalize_mac, parse_neighbor, parse_route


GATEWAY = '192.0.2.1'
MAC = '02:11:22:33:44:55'
FIXTURES = {
    'linux': ('[{"dst":"default","gateway":"192.0.2.1","dev":"wlan0"}]',
              '[{"dst":"192.0.2.1","dev":"wlan0","lladdr":"02:11:22:33:44:55","state":["REACHABLE"]}]'),
    'darwin': ('   route to: default\n  gateway: 192.0.2.1\n  interface: en0\n',
               '? (192.0.2.1) at 2:11:22:33:44:55 on en0 ifscope [ethernet]\n'),
    'win32': ('{"NextHop":"192.0.2.1","InterfaceIndex":9}',
              '{"IPAddress":"192.0.2.1","InterfaceIndex":9,"LinkLayerAddress":"02-11-22-33-44-55"}'),
}


class PresenceTests(unittest.TestCase):
    def fake(self, outputs):
        calls = []
        def run(argv):
            calls.append(argv)
            value = outputs[len(calls) - 1]
            if isinstance(value, Exception):
                raise value
            return value
        return run, calls

    def test_home_on_all_three_platform_fixtures(self):
        for platform, outputs in FIXTURES.items():
            with self.subTest(platform=platform):
                run, calls = self.fake(outputs)
                self.assertIs(detect(GATEWAY, MAC, platform, run), True)
                self.assertEqual(len(calls), 2)

    def test_known_other_gateway_returns_away_without_neighbor_query(self):
        for platform, outputs in FIXTURES.items():
            with self.subTest(platform=platform):
                run, calls = self.fake(outputs)
                self.assertIs(detect('192.0.2.2', MAC, platform, run), False)
                self.assertEqual(len(calls), 1)

    def test_same_gateway_wrong_mac_is_away(self):
        for platform, outputs in FIXTURES.items():
            with self.subTest(platform=platform):
                run, _ = self.fake(outputs)
                self.assertIs(detect(GATEWAY, '02:11:22:33:44:66', platform, run), False)

    def test_missing_data_and_command_failure_are_unknown(self):
        for platform, outputs in FIXTURES.items():
            with self.subTest(platform=platform):
                run, _ = self.fake([outputs[0], '[]' if platform != 'darwin' else '? (192.0.2.1) at (incomplete) on en0'])
                self.assertIsNone(detect(GATEWAY, MAC, platform, run))
                run, _ = self.fake([subprocess.TimeoutExpired('command', 1)])
                self.assertIsNone(detect(GATEWAY, MAC, platform, run))

    def test_ambiguous_routes_or_neighbors_are_unknown(self):
        routes = json.dumps([{'dst': 'default', 'gateway': GATEWAY, 'dev': 'a'},
                             {'dst': 'default', 'gateway': '192.0.2.2', 'dev': 'b'}])
        self.assertIsNone(parse_route('linux', routes))
        neighbors = json.dumps([{'dst': GATEWAY, 'dev': 'a', 'lladdr': MAC},
                                {'dst': GATEWAY, 'dev': 'a', 'lladdr': '02:11:22:33:44:66'}])
        self.assertIsNone(parse_neighbor('linux', neighbors, GATEWAY, 'a'))

    def test_missing_or_wrong_interface_does_not_match(self):
        for platform, outputs in FIXTURES.items():
            with self.subTest(platform=platform):
                route = parse_route(platform, outputs[0])
                self.assertIsNone(parse_neighbor(platform, outputs[1], route[0], 'different'))

    def test_incomplete_neighbors_and_placeholder_mac_are_unknown(self):
        neighbors = json.dumps([{'dst': GATEWAY, 'dev': 'wlan0', 'lladdr': MAC, 'state': ['FAILED']}])
        self.assertIsNone(parse_neighbor('linux', neighbors, GATEWAY, 'wlan0'))
        self.assertIsNone(normalize_mac('00-00-00-00-00-00'))

    def test_invalid_inputs_and_mac_normalization(self):
        self.assertEqual(normalize_mac('2-11-22-33-44-55'), MAC)
        for gateway, mac in [('::1', MAC), (GATEWAY, 'not-a-mac')]:
            with self.assertRaises(ValueError):
                detect(gateway, mac, 'linux', lambda _: self.fail('must not run'))
        self.assertIsNone(detect(GATEWAY, MAC, 'unsupported', lambda _: self.fail('must not run')))


if __name__ == '__main__':
    unittest.main()
