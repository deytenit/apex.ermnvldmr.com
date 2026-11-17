"""Tests for operator sudo and docker group provisioning in apex config actions."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_action(name):
    path = ROOT / "actions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"action_{name.replace('/', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OperatorConfigTests(unittest.TestCase):
    def setUp(self):
        self.base = load_action("config/base")
        self.docker = load_action("config/docker")

    def test_base_skips_when_adam_absent(self):
        ctx = SimpleNamespace(sys=Mock(), host=Mock())
        with patch.object(self.base.pwd, "getpwnam", side_effect=KeyError):
            self.base._configure_operator(ctx)
        ctx.sys.sudo.assert_not_called()
        ctx.host.write_file.assert_not_called()

    def test_base_rejects_system_adam_account(self):
        ctx = SimpleNamespace(sys=Mock(), host=Mock())
        account = SimpleNamespace(pw_uid=998, pw_gid=998)
        with patch.object(self.base.pwd, "getpwnam", return_value=account):
            with self.assertRaisesRegex(RuntimeError, "inspection"):
                self.base._configure_operator(ctx)
        ctx.sys.sudo.assert_not_called()

    def test_base_configures_sudo_group_and_sudoers(self):
        ctx = SimpleNamespace(sys=Mock(), host=Mock())
        account = SimpleNamespace(pw_uid=1001, pw_gid=1001)
        with (patch.object(self.base.pwd, "getpwnam", return_value=account),
              patch.object(self.base.grp, "getgrnam", return_value=SimpleNamespace())):
            self.base._configure_operator(ctx)
        ctx.sys.sudo.assert_any_call(
            ["usermod", "--shell", "/bin/bash", "--append", "--groups", "sudo", "adam"]
        )
        ctx.sys.sudo.assert_any_call(["visudo", "-cf", "-"], input=self.base.SUDOERS_POLICY)
        ctx.host.write_file.assert_called_once_with(
            str(self.base.SUDOERS_PATH), self.base.SUDOERS_POLICY, mode="0440", sudo=True
        )
        ctx.sys.sudo.assert_any_call(["visudo", "-c"])

    def test_docker_adds_adam_to_docker_group(self):
        ctx = SimpleNamespace(sys=Mock(), paths=SimpleNamespace(commons=str(ROOT)))
        account = SimpleNamespace(pw_uid=1001, pw_gid=1001)
        with (patch.object(self.docker.pwd, "getpwnam", return_value=account),
              patch.object(self.docker.grp, "getgrnam", return_value=SimpleNamespace())):
            self.docker.run(ctx, [])
        ctx.sys.sudo.assert_any_call(["usermod", "--append", "--groups", "docker", "adam"])

    def test_docker_skips_group_when_adam_absent(self):
        ctx = SimpleNamespace(sys=Mock(), paths=SimpleNamespace(commons=str(ROOT)))
        with patch.object(self.docker.pwd, "getpwnam", side_effect=KeyError):
            self.docker.run(ctx, [])
        self.assertEqual(ctx.sys.sudo.call_count, 1)
