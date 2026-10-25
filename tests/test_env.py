# tests/test_env.py
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

from actions import env


class TestEnv(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.comp_dir = os.path.join(self.tmpdir.name, "compositions")
        os.makedirs(os.path.join(self.comp_dir, "apex"))

        self.ctx = MagicMock()
        self.ctx.paths.compositions = self.comp_dir
        self.ctx.vars.return_value = {
            "APEX_SUBNET": "198.18.16.0/24",
            "APEX_NODE_HOST": "test-host",
            "OTHER_VAR": "ignore_me",
        }

    def test_export_format_stdout(self):
        args = MagicMock(sync=False, write=None, dotenv=False)
        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            env.run(self.ctx, args)
        finally:
            sys.stdout = old_stdout

        out = captured.getvalue()
        self.assertIn("export APEX_NODE_HOST=test-host", out)
        self.assertIn("export APEX_SUBNET=198.18.16.0/24", out)
        self.assertNotIn("OTHER_VAR", out)

    def test_dotenv_format_stdout(self):
        args = MagicMock(sync=False, write=None, dotenv=True)
        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            env.run(self.ctx, args)
        finally:
            sys.stdout = old_stdout

        out = captured.getvalue()
        self.assertIn("APEX_NODE_HOST=test-host", out)
        self.assertNotIn("export", out)

    def test_sync_to_file(self):
        target = os.path.join(self.comp_dir, "apex", ".env")
        args = MagicMock(sync=True, write=None, dotenv=False)
        env.run(self.ctx, args)

        self.ctx.host.write_file.assert_called_once()
        call_args = self.ctx.host.write_file.call_args
        self.assertEqual(call_args[0][0], target)
        self.assertIn("APEX_SUBNET=198.18.16.0/24", call_args[0][1])


if __name__ == "__main__":
    unittest.main()
