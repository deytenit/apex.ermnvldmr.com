"""Remote bootstrap checks using disposable repositories, keys, and home directories."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bootstrap-node-remote.py"
loader = importlib.util.spec_from_file_location("bootstrap_remote", SCRIPT)
remote = importlib.util.module_from_spec(loader)
loader.loader.exec_module(remote)


def git(*args):
    return subprocess.run(["git", "-c", "protocol.file.allow=always", *map(str, args)],
                          text=True, capture_output=True, check=True).stdout


class OperatorAccount(unittest.TestCase):
    def test_new_debian_account_is_ready_for_sudo_and_later_docker_install(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve() / "adam"
            account = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(home))
            commands = []

            def provision(command):
                commands.append(command)
                if command[0] == "useradd":
                    home.mkdir(mode=0o700)

            actual_stat = Path.stat

            def stat(path, *args, **kwargs):
                info = actual_stat(path, *args, **kwargs)
                if path != home:
                    return info
                fields = list(info)
                fields[4] = account.pw_uid
                return os.stat_result(fields)

            with (patch.object(remote, "HOME", home),
                  patch.object(Path, "stat", stat),
                  patch.object(Path, "exists", return_value=False),
                  patch.object(remote.pwd, "getpwnam", side_effect=[KeyError, account]),
                  patch.object(remote, "checked", side_effect=provision)):
                self.assertEqual(remote.prepare_account(), account)
            self.assertEqual(sum(command[0] == "useradd" for command in commands), 1)
            self.assertIn("sudo,docker", commands[-1])

    def test_existing_system_account_cannot_be_promoted(self):
        command = Mock()
        account = SimpleNamespace(pw_uid=998, pw_gid=998, pw_dir="/home/adam")
        with (patch.object(remote.pwd, "getpwnam", return_value=account),
              patch.object(remote, "checked", command)):
            with self.assertRaisesRegex(RuntimeError, "account or home"):
                remote.prepare_account()
        command.assert_not_called()


class Credentials(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "adam"
        self.home.mkdir()
        for name in ("operator", "deploy"):
            subprocess.run(["ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f",
                            str(self.root / name)], check=True)
        self.spec = dict(operator_key=(self.root / "operator.pub").read_text(),
                         deploy_key=(self.root / "deploy").read_text(), node_name="web",
                         known_hosts="[example.test]:2222 " + (self.root / "deploy.pub").read_text())
        self.authorized = self.home / ".ssh/authorized_keys"

    def test_credentials_validated_against_port_and_operator_identity(self):
        remote.validate_credentials(self.spec, "[example.test]:2222")
        with self.assertRaises(RuntimeError):
            remote.validate_credentials(self.spec, "example.test")
        self.spec["deploy_key"] = (self.root / "operator").read_text()
        with self.assertRaisesRegex(ValueError, "separate"):
            remote.validate_credentials(self.spec, "[example.test]:2222")

    def test_repeat_and_second_checkout_preserve_operator_keys_and_git_identities(self):
        self.authorized.parent.mkdir()
        self.authorized.write_text((self.root / "deploy.pub").read_text())
        remote.configure_ssh(self.home, self.spec, "example.test")
        first_config = (self.home / ".gitconfig").read_bytes()
        remote.configure_ssh(self.home, self.spec, "example.test")
        self.assertEqual((self.home / ".gitconfig").read_bytes(), first_config)
        self.assertEqual(self.authorized.read_text().splitlines(), [
            (self.root / "deploy.pub").read_text().strip(), self.spec["operator_key"].strip()])
        remote.configure_ssh(self.home, dict(self.spec, node_name="db", deploy_key="second"), "db.test")
        self.assertEqual((self.authorized.parent / "apex-web.key").read_text(), self.spec["deploy_key"])
        work = self.root / "checkout"
        git("init", "-q", work)
        config = self.home / ".gitconfig"
        config.write_text(config.read_text().replace("/srv/web", str(work)))
        env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")
        self.assertIn("apex-web.sshconf", subprocess.check_output(
            ["git", "-C", str(work), "config", "core.sshCommand"], text=True, env=env))
        self.assertEqual(subprocess.run(["git", "config", "--get", "core.sshCommand"],
                         cwd=self.root, env=env, capture_output=True).returncode, 1)

    def test_restricted_key_and_redirected_authorization_are_preserved(self):
        self.authorized.parent.mkdir()
        restriction = 'from="192.0.2.1" ' + self.spec["operator_key"]
        self.authorized.write_text(restriction)
        with self.assertRaisesRegex(ValueError, "restrictions"):
            remote.configure_ssh(self.home, self.spec, "example.test")
        self.assertEqual(self.authorized.read_text(), restriction)
        victim = self.root / "preserved"
        self.authorized.rename(victim)
        for link in (lambda: self.authorized.symlink_to(victim),
                     lambda: os.link(victim, self.authorized)):
            link()
            with self.assertRaisesRegex(ValueError, "Unsafe file"):
                remote.configure_ssh(self.home, self.spec, "example.test")
            self.assertEqual(victim.read_text(), restriction)
            self.authorized.unlink()


class Checkout(unittest.TestCase):
    def test_failed_clone_retries_recursively_and_preserves_local_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            srv = root / "srv"
            srv.mkdir()
            source = root / "repo"
            account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with self.assertRaises(subprocess.CalledProcessError):
                remote.checkout(srv, "node", str(source), account, git)
            self.assertEqual(list(srv.iterdir()), [])
            for repo in (source, root / "common"):
                git("init", "-q", repo)
                (repo / "sample").write_text("initial")
                git("-C", repo, "add", "sample")
                git("-C", repo, "-c", "user.name=T", "-c", "user.email=t@test.example",
                    "commit", "-qm", "initial")
            git("-C", source, "submodule", "add", str(root / "common"), "commons")
            git("-C", source, "-c", "user.name=T", "-c", "user.email=t@test.example",
                "commit", "-qm", "add submodule")
            remote.checkout(srv, "node", str(source), account, git)
            self.assertEqual((srv / "node/commons/sample").read_text(), "initial")
            dirty = srv / "node/sample"
            dirty.write_text("local edits")
            remote.checkout(srv, "node", str(source), account, git)
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                remote.checkout(srv, "node", "another-origin", account, git)
            self.assertEqual(dirty.read_text(), "local edits")
