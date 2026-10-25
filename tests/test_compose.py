# tests/test_compose.py
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from actions import compose


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.comp_dir = os.path.join(self.tmpdir.name, "compositions")
        os.makedirs(self.comp_dir)

        # Create compositions: apex, sso, arr
        for name in ("apex", "sso", "arr"):
            d = os.path.join(self.comp_dir, name)
            os.makedirs(d)
            with open(os.path.join(d, "docker-compose.yml"), "w") as f:
                f.write(f"name: {name}\n")

        self.stier3 = os.path.join(self.tmpdir.name, "tier3_shared")
        os.makedirs(os.path.join(self.stier3, "apex", "xray", "shm"))

        self.ctx = MagicMock()
        self.ctx.paths.compositions = self.comp_dir
        self.ctx.paths.stier3 = self.stier3
        self.ctx.vars.return_value = {
            "APEX_SUBNET": "198.18.16.0/24",
            "APEX_NODE_HOST": "test-host",
        }
        self.ctx.sys.run = MagicMock()

    def test_sequence_all_by_default(self):
        args = MagicMock(action="restart", p=None, dry_run=False, extra=[])
        compose.run(self.ctx, args)

        # Should execute on apex, arr, sso (apex core first, others sorted)
        calls = self.ctx.sys.run.call_args_list
        self.assertEqual(len(calls), 3)
        visited = [os.path.basename(c.kwargs["cwd"]) for c in calls]
        self.assertEqual(visited, ["apex", "arr", "sso"])

    def test_down_reverse_sequence(self):
        args = MagicMock(action="down", p=None, dry_run=False, extra=[])
        compose.run(self.ctx, args)

        calls = self.ctx.sys.run.call_args_list
        self.assertEqual(len(calls), 3)
        visited = [os.path.basename(c.kwargs["cwd"]) for c in calls]
        # Reverse on down: sso, arr, apex
        self.assertEqual(visited, ["sso", "arr", "apex"])

    def test_target_single_composition_positional(self):
        args = MagicMock(action="restart", p=None, dry_run=False, extra=["apex"])
        compose.run(self.ctx, args)

        calls = self.ctx.sys.run.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(os.path.basename(calls[0].kwargs["cwd"]), "apex")
        self.assertEqual(calls[0].args[0], ["docker", "compose", "restart"])

    def test_target_single_composition_flag(self):
        args = MagicMock(action="restart", p="sso", dry_run=False, extra=[])
        compose.run(self.ctx, args)

        calls = self.ctx.sys.run.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(os.path.basename(calls[0].kwargs["cwd"]), "sso")

    def test_target_with_service_passthrough(self):
        args = MagicMock(action="restart", p=None, dry_run=False, extra=["apex", "traefik"])
        compose.run(self.ctx, args)

        calls = self.ctx.sys.run.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(os.path.basename(calls[0].kwargs["cwd"]), "apex")
        self.assertEqual(calls[0].args[0], ["docker", "compose", "restart", "traefik"])

    def test_target_up_detached_order(self):
        args = MagicMock(action="up", p=None, dry_run=False, extra=["-d", "sso"])
        compose.run(self.ctx, args)

        calls = self.ctx.sys.run.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(os.path.basename(calls[0].kwargs["cwd"]), "sso")
        self.assertEqual(calls[0].args[0], ["docker", "compose", "up", "-d"])

    def test_dry_run_skips_execution(self):
        args = MagicMock(action="restart", p=None, dry_run=True, extra=["apex"])
        compose.run(self.ctx, args)

        self.ctx.sys.run.assert_not_called()
        self.ctx.log.info.assert_any_call(
            f"[DRY-RUN] (cd {os.path.join(self.comp_dir, 'apex')} && docker compose restart)"
        )

    def test_unknown_target_raises_system_exit(self):
        args = MagicMock(action="restart", p=None, dry_run=False, extra=["nonexistent"])
        with self.assertRaises(SystemExit) as cm:
            compose.run(self.ctx, args)
        self.assertEqual(cm.exception.code, 1)

    def test_xray_socket_cleanup_on_apex_restart(self):
        shm = os.path.join(self.stier3, "apex", "xray", "shm")
        sock_file = os.path.join(shm, "stale.sock")
        with open(sock_file, "w") as f:
            f.write("")

        args = MagicMock(action="restart", p=None, dry_run=False, extra=["apex"])
        compose.run(self.ctx, args)

        self.assertFalse(os.path.exists(sock_file))


if __name__ == "__main__":
    unittest.main()
