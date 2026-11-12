import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from clash_guard.__main__ import main
from clash_guard.config import load


class CliIntegrationTests(unittest.TestCase):
    def _settings(self, directory):
        quality = Path(directory) / 'manual.json'
        quality.write_text(json.dumps({'nodes': {'A': {'score': 99}}}), encoding='utf-8')
        return {'group': 'group', 'controller': 'http://127.0.0.1:9090',
                'normal_interval': 15, 'quality_file': str(quality),
                'ip_quality': {'enabled': True},
                'coordination': {'enabled': True}}

    def test_preview_skips_coordination_and_once_does_not_start_sampler(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            fake_guard = MagicMock()
            fake_guard.step.return_value = {'event': 'healthy', 'wait': 15}
            fake_guard._refresh_quality.side_effect = lambda: setattr(fake_guard, 'quality', fake_guard.quality_provider())
            fake_sampler = MagicMock()
            fake_sampler.scores.return_value = {}
            with patch('clash_guard.__main__.load', return_value=settings), \
                    patch('clash_guard.__main__.Controller'), \
                    patch('clash_guard.__main__.Guard', return_value=fake_guard) as guard, \
                    patch('clash_guard.ip_quality.QualitySampler', return_value=fake_sampler), \
                    patch('clash_guard.coordination.Coordination') as coordination, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--config', 'ignored', '--state-dir', directory, '--once']), 0)
            coordination.assert_not_called()
            self.assertIsNone(guard.call_args.kwargs['coordination'])
            self.assertEqual(fake_guard.quality, {})
            fake_sampler.sample_async.assert_not_called()
            fake_sampler.close.assert_called_once()

    def test_switch_mode_constructs_configured_coordination(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            fake_guard = MagicMock()
            fake_guard.step.return_value = {'event': 'healthy', 'wait': 15}
            fake_sampler, fake_coordinator = MagicMock(), MagicMock()
            fake_sampler.scores.return_value = {}
            with patch('clash_guard.__main__.load', return_value=settings), \
                    patch('clash_guard.__main__.Controller'), \
                    patch('clash_guard.__main__.Guard', return_value=fake_guard) as guard, \
                    patch('clash_guard.ip_quality.QualitySampler', return_value=fake_sampler), \
                    patch('clash_guard.coordination.Coordination', return_value=fake_coordinator) as coordination, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--config', 'ignored', '--state-dir', directory, '--once', '--enable-switch']), 0)
            coordination.assert_called_once_with(settings['coordination'])
            self.assertIs(guard.call_args.kwargs['coordination'], fake_coordinator)
            fake_sampler.close.assert_called_once()

    def test_automatic_sampling_receives_only_eligible_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            settings['excluded_name_patterns'] = ['excluded']
            fake_guard, fake_sampler = MagicMock(), MagicMock()
            fake_guard.step.return_value = {'event': 'healthy', 'wait': 15}
            fake_sampler.scores.return_value = {}
            fake_api = MagicMock()
            fake_api.proxies.return_value = {'group': {'all': ['allowed', 'excluded']}}
            def permitted(name, proxies, now, health_url, patterns, max_age):
                return name == 'allowed' and patterns == ['excluded']
            with patch('clash_guard.__main__.load', return_value=settings), \
                    patch('clash_guard.__main__.Controller', return_value=fake_api), \
                    patch('clash_guard.__main__.Guard', return_value=fake_guard), \
                    patch('clash_guard.__main__.eligible', side_effect=permitted), \
                    patch('clash_guard.ip_quality.QualitySampler', return_value=fake_sampler), \
                    patch('clash_guard.__main__.time.sleep', side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    main(['--config', 'ignored', '--state-dir', directory])
            fake_sampler.sample_async.assert_called_once_with(['allowed'])
            fake_sampler.close.assert_called_once()

    def test_parent_following_does_not_start_independent_quality_sampling(self):
        for event in ('following_parent', 'follow_parent_unavailable'):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                settings = self._settings(directory)
                fake_guard, fake_sampler = MagicMock(), MagicMock()
                fake_guard.step.return_value = {'event': event, 'wait': 15}
                fake_sampler.scores.return_value = {}
                with patch('clash_guard.__main__.load', return_value=settings), \
                        patch('clash_guard.__main__.Controller'), \
                        patch('clash_guard.__main__.Guard', return_value=fake_guard), \
                        patch('clash_guard.ip_quality.QualitySampler', return_value=fake_sampler), \
                        patch('clash_guard.__main__.time.sleep', side_effect=KeyboardInterrupt), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        main(['--config', 'ignored', '--state-dir', directory])
                fake_sampler.sample_async.assert_not_called()
                fake_sampler.pause.assert_called_once()
                fake_sampler.close.assert_called_once()

    def test_parent_configuration_rejects_self_reference_and_duplicates(self):
        example = json.loads(Path('examples/config.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for parents in ([example['group']], ['AI', 'AI'], ['', 'AI']):
                with self.subTest(parents=parents):
                    path.write_text(json.dumps(dict(example, follow_parent_groups=parents)), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        load(path)
            path.write_text(json.dumps(dict(example, follow_parent_groups=['AI'])), encoding='utf-8')
            self.assertEqual(load(path)['follow_parent_groups'], ['AI'])
