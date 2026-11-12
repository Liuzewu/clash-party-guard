"""Run the Party override on synthetic profiles, without contacting a core."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

NODE = shutil.which('node')
SCRIPT = Path(__file__).resolve().parents[1] / 'examples' / 'party-override.js'


@unittest.skipUnless(NODE, 'Node.js is optional')
class PartyOverrideTests(unittest.TestCase):
    def run_policy(self, config, parents=None):
        source = '''
const fs = require('fs');
const vm = require('vm');
const sandbox = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const value = JSON.parse(process.argv[2]);
const parents = JSON.parse(process.argv[3]);
sandbox.configureSelector(value, {groupName:'Example Selector', followParentGroups:parents, exclude:/测试|測試/i});
process.stdout.write(JSON.stringify(value));
'''
        result = subprocess.run([NODE, '-e', source, str(SCRIPT), json.dumps(config),
                                 json.dumps(parents if parents is not None else ['Parent Selector'])],
                                capture_output=True, text=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def base(self):
        return {'proxies': [{'name': 'Node A'}, {'name': '测试 Node'}], 'proxy-groups': [
            {'name': 'Example Selector', 'type': 'select', 'proxies': ['Node A']},
            {'name': 'Parent Selector', 'type': 'select', 'proxies': ['Node A']}]}

    def test_parent_is_offered_without_forcing_current_selection(self):
        result = self.run_policy(self.base())
        group = result['proxy-groups'][0]
        self.assertEqual(group['proxies'], ['Node A', 'Parent Selector'])
        self.assertNotIn('now', group)
        self.assertNotIn('测试 Node', group['proxies'])
        self.assertEqual(result['proxy-groups'][1]['proxies'], ['Node A'])

    def test_new_subscription_nodes_are_added_without_duplicates(self):
        config = self.base()
        config['proxies'].append({'name': 'Node B'})
        result = self.run_policy(config)
        repeated = self.run_policy(result)
        self.assertEqual(repeated['proxy-groups'][0]['proxies'], ['Node A', 'Node B', 'Parent Selector'])

    def test_parent_with_direct_or_indirect_cycle_is_not_added(self):
        config = self.base()
        config['proxy-groups'][1]['proxies'] = ['Intermediate']
        config['proxy-groups'].append({'name': 'Intermediate', 'type': 'select', 'proxies': ['Example Selector']})
        result = self.run_policy(config)
        self.assertNotIn('Parent Selector', result['proxy-groups'][0]['proxies'])
        result = self.run_policy(self.base(), ['Example Selector'])
        self.assertNotIn('Example Selector', result['proxy-groups'][0]['proxies'])

    def test_parent_with_existing_cycle_or_wrong_type_is_not_added(self):
        config = self.base()
        config['proxy-groups'][1]['proxies'] = ['Intermediate']
        config['proxy-groups'].append({'name': 'Intermediate', 'type': 'select', 'proxies': ['Parent Selector']})
        self.assertNotIn('Parent Selector', self.run_policy(config)['proxy-groups'][0]['proxies'])
        config = self.base(); config['proxy-groups'][1]['type'] = 'url-test'
        self.assertNotIn('Parent Selector', self.run_policy(config)['proxy-groups'][0]['proxies'])

    def test_missing_selector_is_left_alone(self):
        config = self.base()
        self.assertEqual(self.run_policy(config, ['Missing Parent'])['proxy-groups'][0]['proxies'], ['Node A'])


if __name__ == '__main__':
    unittest.main()
