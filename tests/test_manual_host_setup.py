import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine import overlay


def action(name):
    return overlay.load_module(str(Path(__file__).resolve().parents[1] / f"actions/{name}.py"),
                               "test_manual_" + name.replace("/", "_"))


class HostSetup(unittest.TestCase):
    def test_packages_preserve_pve_firewall(self):
        packages = action("config/packages")
        ctx = SimpleNamespace(sys=Mock())
        with (patch.object(Path, "is_file", return_value=True),
              patch.object(Path, "exists", return_value=True),
              patch.object(packages, "_install_missing") as install):
            packages.run(ctx, None)
        requested = install.call_args.args[1]
        self.assertIn("sudo", requested)
        self.assertNotIn("ufw", requested)

    def test_fresh_key_proof_gates_each_destructive_phase(self):
        hardening = action("config/hardening")
        for phase, operation in (("transition", "finalize"), ("final", "commit")):
            state = {"status": phase, "authenticated_after_us": 123,
                     "bundle": "/usr/local/lib/apex-provisioner/" + "a" * 64, "token": "a" * 32}
            system = Mock()
            with (patch.object(hardening, "_state", return_value=state),
                  patch.object(hardening, "_connection", side_effect=RuntimeError("fresh login required")) as proof):
                with self.assertRaisesRegex(RuntimeError, "fresh login required"):
                    hardening.run(SimpleNamespace(sys=system), SimpleNamespace(operation=operation))
                proof.assert_called_once_with(unittest.mock.ANY, after=123)
            self.assertFalse(system.mock_calls)

    def test_begin_installs_bundle_before_dispatch_with_real_endpoint_and_profile(self):
        hardening = action("config/hardening")
        ctx = SimpleNamespace(sys=Mock())
        bundle = "/usr/local/lib/apex-provisioner/" + "a" * 64
        ctx.sys.sudo.return_value.stdout = bundle
        with (patch.object(hardening, "_operator_access"),
              patch.object(hardening, "_state", return_value={}),
              patch.object(Path, "is_file", return_value=True),
              patch.object(hardening, "_original_endpoint", return_value=(2022, "192.0.2.8"))):
            hardening.run(ctx, None)
        install, begin = [call.args[0] for call in ctx.sys.sudo.call_args_list]
        self.assertTrue(install[-1].endswith("/manual_bundle.py"))
        self.assertIn(bundle, begin)
        self.assertEqual(begin[begin.index("begin"): -2],
                         ["begin", "--profile", "proxmox", "--original-port", "2022", "--client-address", "192.0.2.8"])

    def test_installed_watchdog_survives_checkout_and_dispatches_cli_arguments(self):
        hardening = action("config/hardening")
        root = Path(__file__).resolve().parents[1]
        installer = overlay.load_module(str(root / "scripts/provisioning/manual_bundle.py"),
                                        "test_manual_bundle")
        with tempfile.TemporaryDirectory() as temp, patch.object(sys, "path", list(sys.path)):
            destination = installer.install(Path(temp).resolve())
            self.assertEqual(installer.install(Path(temp).resolve()), destination)
            state = {"status": "final", "authenticated_after_us": 120, "profile": "debian",
                     "bundle": "/usr/local/lib/apex-provisioner/" + destination.name, "token": "a" * 32}
            system = Mock()
            with (patch.object(hardening, "_state", return_value=state),
                  patch.object(hardening, "_connection")):
                hardening.run(SimpleNamespace(sys=system), SimpleNamespace(operation="commit"))
            # Run the generated CLI in isolation. Stop at its root check after argument parsing.
            command = system.sudo.call_args.args[0]
            command[0] = sys.executable
            command[3] = "import os; os.geteuid = lambda: 1000; " + command[3]
            command[4] = str(destination)
            result = subprocess.run(command, cwd=temp, capture_output=True, text=True, timeout=30)
            self.assertIn("Access handover requires root", result.stderr)
            source = destination / "provisioning/access.py"
            source.chmod(0o666)
            with self.assertRaisesRegex(RuntimeError, "Untrusted installed"):
                installer.install(Path(temp).resolve())
            source.chmod(0o644)
            source.write_text("changed")
            with self.assertRaisesRegex(RuntimeError, "differs from the checkout"):
                installer.install(Path(temp).resolve())

    def test_other_linux_distributions_are_rejected_before_installing_the_watchdog(self):
        hardening = action("config/hardening")
        ctx = SimpleNamespace(sys=Mock())
        with (patch.object(hardening, "_operator_access"),
              patch.object(hardening, "_state", return_value={}),
              patch.object(Path, "is_file", return_value=False),
              patch.object(hardening.platform, "freedesktop_os_release", return_value={"ID": "ubuntu"})):
            with self.assertRaisesRegex(RuntimeError, "supports Debian"):
                hardening.run(ctx, None)
        self.assertFalse(ctx.sys.mock_calls)

    def test_current_endpoint_must_have_authenticated_with_a_key(self):
        hardening = action("config/hardening")
        ctx = SimpleNamespace(sys=Mock())
        sockets = SimpleNamespace(stdout='0 0 192.0.2.9:22 192.0.2.8:3002 users:(("sshd",pid=60,fd=4))')
        accepted = {"_PID": "60", "__MONOTONIC_TIMESTAMP": "100",
                    "MESSAGE": "Accepted publickey for adam from 192.0.2.8 port 3002 ssh2: ED25519 SHA256:example"}
        with (patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.100 4321 192.0.2.9 1234",
                                      "SSH_USER_AUTH": "/arbitrary/file"}),
              patch.object(hardening, "_ancestors", return_value={60: 50}),
              patch.object(hardening.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="adam"))):
            for changes in ({"_PID": "42"}, {"__MONOTONIC_TIMESTAMP": "49"},
                            {"MESSAGE": accepted["MESSAGE"].replace("3002", "4002")}):
                with self.subTest(changes=changes):
                    ctx.sys.sudo.side_effect = [sockets, SimpleNamespace(
                        returncode=0, stdout=json.dumps({**accepted, **changes}))]
                    with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                        hardening._connection(ctx)
            ctx.sys.sudo.side_effect = [sockets, SimpleNamespace(returncode=0, stdout=json.dumps(accepted))]
            self.assertEqual(hardening._connection(ctx),
                             "user=adam,host=192.0.2.8,addr=192.0.2.8,laddr=192.0.2.9,lport=22")

    def test_reused_transport_and_wrong_port_cannot_finalize(self):
        hardening = action("config/hardening")
        ctx = SimpleNamespace(sys=Mock())
        entry = {"_PID": "60", "__MONOTONIC_TIMESTAMP": "100",
                 "MESSAGE": "Accepted publickey for adam from 192.0.2.8 port 3002 ssh2: ED25519"}
        ctx.sys.sudo.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(entry))
        with (patch.object(hardening.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="adam")),
              patch.object(hardening, "_ancestors", return_value={60: 50})):
            for port, after in ((22, 49), (2222, 50), (2222, 99), (2222, 101)):
                with (self.subTest(port=port, after=after),
                      patch.object(hardening, "_socket", return_value=("192.0.2.9", port, "192.0.2.8", 3002)),
                      self.assertRaises(RuntimeError)):
                    hardening._connection(ctx, after=after)
            with patch.object(hardening, "_socket", return_value=("192.0.2.9", 2222, "192.0.2.8", 3002)):
                self.assertIn("lport=2222", hardening._connection(ctx, after=49))

    def test_journald_restart_failure_restores_previous_config(self):
        base = action("config/base")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "99-apex.conf"
            legacy = root / "maxuse.conf"
            target.write_text("previous\n")
            legacy.write_text("legacy\n")
            backup = root / "legacy-backup"
            host = Mock()
            host.backup.side_effect = lambda path, **kwargs: (backup.write_bytes(Path(path).read_bytes())
                                                             and str(backup))
            host.write_file.side_effect = lambda path, content, **kwargs: Path(path).write_text(content)
            def sudo(command):
                if command[0] == "systemctl":
                    raise RuntimeError("restart failed")
                if command[0] == "cp":
                    Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
                else:
                    Path(command[-1]).unlink()
            ctx = SimpleNamespace(sys=SimpleNamespace(sudo=sudo), host=host)
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                base._configure(ctx, target, legacy, "updated\n", restart=True)
            self.assertEqual(target.read_text(), "previous\n")
            self.assertEqual(legacy.read_text(), "legacy\n")

    def test_unverified_session_makes_no_host_changes(self):
        hardening = action("config/hardening")
        ctx = SimpleNamespace(sys=Mock(), host=Mock())
        with (patch.object(hardening.pwd, "getpwnam", return_value=SimpleNamespace(
                  pw_uid=1001, pw_dir="/home/adam")),
              patch.object(hardening.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="root")),
              patch.object(Path, "read_text", return_value="1001")):
            with self.assertRaisesRegex(RuntimeError, "independent administrator"):
                hardening.run(ctx, None)
        self.assertFalse(ctx.host.mock_calls)
        self.assertFalse(ctx.sys.mock_calls)

    def test_independent_administrator_can_harden_only_with_an_operator_key(self):
        hardening = action("config/hardening")
        account = SimpleNamespace(pw_uid=1001, pw_dir="/home/adam")
        system = Mock()
        def privileged(command, **kwargs):
            output = ""
            if "stat" in command:
                output = "1001 600 regular file" if command[-1].endswith("authorized_keys") else "1001 700 directory"
            elif "cat" in command:
                output = "ssh-ed25519 fixture"
            elif "-T" in command:
                output = "authorizedkeysfile .ssh/authorized_keys"
            return SimpleNamespace(stdout=output, returncode=0)
        system.sudo.side_effect = privileged
        ctx = SimpleNamespace(sys=system)
        with (patch.object(hardening.pwd, "getpwnam", return_value=account),
              patch.object(hardening.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="root")),
              patch.object(hardening.os, "getuid", return_value=0),
              patch.object(Path, "read_text", return_value="0")):
            system.run.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, "valid unrestricted operator public key"):
                hardening._operator_access(ctx)
            system.run.return_value.returncode = 0
            self.assertEqual(hardening._operator_access(ctx), "user=adam,host=localhost,addr=127.0.0.1")

    def test_chown_limits_changes_to_checkout_and_skips_mounts_and_hardlinks(self):
        chown = action("repository/chown")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in (".git", "configs", "volumes"):
                (root / name).mkdir()
            for name in ("configs/node", ".git/HEAD", "private", "volumes/data"):
                (root / name).write_text("fixture")
            os.link(root / "private", root / "configs/key")
            (root / "configs/other").symlink_to(root / "volumes", target_is_directory=True)
            with patch("os.chown") as ownership, patch("os.fchown"):
                chown._repair(str(root), {"configs/node", "configs/key", "configs/other", "volumes/data"},
                              {str(root / "volumes")}, os.getuid(), os.getgid())
            self.assertEqual({call.args[0] for call in ownership.call_args_list},
                             {"HEAD", "node", "other"})
            self.assertTrue(all(not call.kwargs["follow_symlinks"] for call in ownership.call_args_list))
