import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('timezone_action', ROOT / 'actions/configure/timezone.py')
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)


class TestTimezone(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'timezone'
        self.zone = 'UTC'
        self.cron_refreshes = []
        def read(*args, **kwargs):
            return SimpleNamespace(stdout=self.zone+'\n')
        def apply(args):
            self.zone = args[-1]
        self.system = Mock()
        self.system.run.side_effect = read
        self.system.sudo.side_effect = apply
        self.system.service_is_active.return_value = True
        def restart(name):
            self.cron_refreshes.append(name)
            return True
        self.system.restart.side_effect = restart
        self.ctx = SimpleNamespace(paths=SimpleNamespace(configs=self.tmp.name), sys=self.system, log=Mock())

    def test_timezone_transition_refreshes_cron_once_and_repeat_is_noop(self):
        self.source.write_text('Europe/Moscow\n')
        action.run(self.ctx, SimpleNamespace(dry_run=False))
        self.assertEqual(self.zone, 'Europe/Moscow')
        self.assertEqual(self.cron_refreshes, ['cron'])
        action.run(self.ctx, SimpleNamespace(dry_run=False))
        self.assertEqual(self.cron_refreshes, ['cron'])

    def test_preview_and_absent_config_preserve_host(self):
        action.run(self.ctx, SimpleNamespace(dry_run=False))
        self.source.write_text('Europe/Moscow\n')
        action.run(self.ctx, SimpleNamespace(dry_run=True))
        self.assertEqual(self.zone, 'UTC')
        self.assertEqual(self.cron_refreshes, [])
        self.system.sudo.assert_not_called()

    def test_failed_cron_restart_is_reported_as_failure(self):
        self.source.write_text('Europe/Moscow\n')
        self.system.restart.side_effect = None
        self.system.restart.return_value = False
        with self.assertRaises(SystemExit):
            action.run(self.ctx, SimpleNamespace(dry_run=False))
        self.ctx.log.success.assert_not_called()
        self.assertEqual(self.zone, 'Europe/Moscow')

    def test_inactive_cron_is_not_started(self):
        self.source.write_text('Europe/Moscow\n')
        self.system.service_is_active.return_value = False
        action.run(self.ctx, SimpleNamespace(dry_run=False))
        self.assertEqual(self.zone, 'Europe/Moscow')
        self.assertEqual(self.cron_refreshes, [])

    def test_invalid_zone_does_not_mutate_host(self):
        self.source.write_text('../not-a-zone\n')
        with self.assertRaises(SystemExit):
            action.run(self.ctx, SimpleNamespace(dry_run=False))
        self.system.sudo.assert_not_called()
        self.assertEqual(self.cron_refreshes, [])
