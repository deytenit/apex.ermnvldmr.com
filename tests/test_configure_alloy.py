"""Native Alloy opt-in and validation boundaries."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from engine import overlay

ROOT = Path(__file__).resolve().parents[1]
alloy = overlay.load_module(str(ROOT / 'actions/configure/alloy.py'), 'test_alloy_action')


class NativeAlloy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.configs = Path(self.temp.name)
        self.ctx = SimpleNamespace(paths=SimpleNamespace(configs=str(self.configs), commons=str(ROOT)),
                                   sys=Mock(), log=Mock())

    def configure(self, **settings):
        (self.configs / 'alloy').mkdir(exist_ok=True)
        (self.configs / 'alloy/config.json').write_text(json.dumps(settings))

    def test_unconfigured_node_is_untouched(self):
        alloy.run(self.ctx, SimpleNamespace(dry_run=False))
        self.ctx.sys.sudo.assert_not_called()

    def test_invalid_role_or_environment_path_cannot_change_service(self):
        for role, path in [('unknown', '/etc/alloy/secrets'), ('host', '/tmp/file\nUser=root')]:
            self.configure(role=role, environment_file=path)
            with self.assertRaises(ValueError):
                alloy.run(self.ctx, SimpleNamespace(dry_run=False))
            self.ctx.sys.sudo.assert_not_called()

    def test_dry_run_never_installs_or_starts(self):
        self.configure(role='guest', environment_file='/etc/alloy/secrets')
        alloy.run(self.ctx, SimpleNamespace(dry_run=True))
        self.ctx.sys.sudo.assert_not_called()

    def test_failed_config_validation_leaves_existing_service_untouched(self):
        self.configure(role='host', environment_file='/etc/alloy/secrets')
        def execute(command):
            if command[0] == 'python3':
                raise RuntimeError('invalid Alloy configuration')
            self.assertEqual(command, ['test', '-f', '/etc/alloy/secrets'])
        self.ctx.sys.sudo.side_effect = execute
        with self.assertRaisesRegex(RuntimeError, 'invalid Alloy'):
            alloy.run(self.ctx, SimpleNamespace(dry_run=False))
        self.ctx.sys.daemon_reload.assert_not_called()
