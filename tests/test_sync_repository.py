import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from actions.repository import sync as repository
from engine.lib.sys import Sys


class RepositorySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "node"
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
                        GIT_ALLOW_PROTOCOL="file")
        self.git("init", "--bare", str(self.remote))
        self.git("clone", str(self.remote), str(self.repo))
        self.git("checkout", "-b", "main", cwd=self.repo)
        (self.repo / "compositions").mkdir()
        (self.repo / "compositions/service").write_text("base\n")
        self.commons_source = self.root / "shared"
        self.git("init", str(self.commons_source))
        (self.commons_source / "file").write_text("shared\n")
        self.git("add", ".", cwd=self.commons_source)
        self.git("commit", "-m", "shared", cwd=self.commons_source)
        self.git("submodule", "add", str(self.commons_source), "commons", cwd=self.repo)
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-m", "base", cwd=self.repo)
        self.git("push", "origin", "main", cwd=self.repo)
        self.log = Mock()
        class Commands(Sys):
            def run(inner, cmd, **kw):
                kw["env"] = dict(self.env, **kw.get("env", {}))
                kw["env"]["GIT_ALLOW_PROTOCOL"] = "file"
                return super().run(cmd, **kw)
        self.ctx = SimpleNamespace(log=self.log, sys=Commands(self.log), notify=Mock(),
                                   node=SimpleNamespace(name="node"),
                                   paths=SimpleNamespace(repo_root=str(self.repo), commons=str(self.repo / "commons"),
                                                         compositions=str(self.repo / "compositions")))
        self.args = SimpleNamespace(telegram_bot_url=None)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, env=self.env, stderr=subprocess.PIPE, text=True).strip()

    def sync(self):
        repository.run(self.ctx, self.args)

    def test_local_change_publishes_main_without_sync_branch_or_notification(self):
        (self.repo / "compositions/service").write_text("changed\n")
        self.sync()
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show", "main:compositions/service"), "changed")
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")
        self.ctx.notify.success.assert_not_called()
        self.ctx.notify.error.assert_not_called()

    def test_noop_keeps_head(self):
        before = self.git("rev-parse", "HEAD", cwd=self.repo)
        self.sync()
        self.assertEqual(before, self.git("rev-parse", "HEAD", cwd=self.repo))

    def test_other_branch_does_not_stage_changes(self):
        self.git("checkout", "-b", "work", cwd=self.repo)
        (self.repo / "compositions/service").write_text("changed\n")
        with self.assertRaises(SystemExit):
            self.sync()
        self.assertEqual(self.git("diff", "--cached", cwd=self.repo), "")

    def test_dirty_commons_blocks_parent_commit(self):
        before = self.git("rev-parse", "HEAD", cwd=self.repo)
        (self.repo / "commons/file").write_text("dirty\n")
        with self.assertRaises(SystemExit):
            self.sync()
        self.assertEqual(before, self.git("rev-parse", "HEAD", cwd=self.repo))

    def test_stale_submodule_after_failed_update_cannot_rollback_pin(self):
        old = self.git("rev-parse", "HEAD", cwd=self.repo / "commons")
        (self.commons_source / "file").write_text("new shared\n")
        self.git("add", ".", cwd=self.commons_source)
        self.git("commit", "-m", "new shared", cwd=self.commons_source)
        new = self.git("rev-parse", "HEAD", cwd=self.commons_source)
        self.git("-C", str(self.repo / "commons"), "fetch", "origin")
        self.git("update-index", "--cacheinfo", "160000," + new + ",commons", cwd=self.repo)
        self.git("commit", "-m", "advance pin", cwd=self.repo)
        before = self.git("rev-parse", "HEAD", cwd=self.repo)
        with self.assertRaises(SystemExit):
            self.sync()
        self.assertEqual(before, self.git("rev-parse", "HEAD", cwd=self.repo))
        self.assertEqual(old, self.git("rev-parse", "HEAD", cwd=self.repo / "commons"))
        self.git("submodule", "update", "--init", cwd=self.repo)
        self.sync()
        self.assertEqual(new, self.git("--git-dir=" + str(self.remote), "rev-parse", "main:commons"))

    def test_concurrent_main_advance_rejects_push_without_overwriting(self):
        original = self.ctx.sys.run
        other = self.root / "other"
        self.git("clone", "--branch", "main", str(self.remote), str(other))
        def race(cmd, **kw):
            if "push" in cmd:
                (other / "compositions/service").write_text("concurrent\n")
                self.git("add", ".", cwd=other)
                self.git("commit", "-m", "concurrent", cwd=other)
                self.git("push", "origin", "main", cwd=other)
            return original(cmd, **kw)
        self.ctx.sys.run = race
        (self.repo / "compositions/service").write_text("local\n")
        with self.assertRaises(SystemExit):
            self.sync()
        self.assertEqual("concurrent", self.git("--git-dir=" + str(self.remote), "show", "main:compositions/service"))
        self.assertEqual("local", self.git("show", "HEAD:compositions/service", cwd=self.repo))

    def test_conflict_preserves_local_commit_and_remote(self):
        other = self.root / "other"
        self.git("clone", "--branch", "main", str(self.remote), str(other))
        (other / "compositions/service").write_text("remote\n")
        self.git("add", ".", cwd=other)
        self.git("commit", "-m", "remote", cwd=other)
        self.git("push", "origin", "main", cwd=other)
        (self.repo / "compositions/service").write_text("local\n")
        with self.assertRaises(SystemExit):
            self.sync()
        self.assertEqual(self.git("show", "HEAD:compositions/service", cwd=self.repo), "local")
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show", "main:compositions/service"), "remote")
        self.assertFalse((self.repo / ".git/rebase-merge").exists())

    def test_encrypts_modified_secrets_when_sops_configured(self):
        (self.repo / ".sops.yaml").write_text("creation_rules:\n  - age: age1test\n")
        app = self.repo / "compositions/app"
        app.mkdir()
        (app / ".env").write_text("SECRET=hello\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        sops = bin_dir / "sops"
        sops.write_text('#!/bin/sh\necho "SECRET=ENC[AES256_GCM,data:abc]"\n')
        sops.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        with unittest.mock.patch.dict(os.environ, PATH=self.env["PATH"]):
            self.sync()
        committed = self.git("--git-dir=" + str(self.remote), "show", "main:compositions/app/.enc.env")
        self.assertEqual(committed, "SECRET=ENC[AES256_GCM,data:abc]")

    def test_encrypts_modified_tier0_secrets_and_maintains_gitignore(self):
        (self.repo / ".sops.yaml").write_text("creation_rules:\n  - age: age1test\n")
        app = self.repo / "compositions/app"
        t0 = app / "@tier0"
        t0.mkdir(parents=True)
        (t0 / "cfg.enc.yaml").write_text("k: old\n")
        (t0 / "cfg.yaml").write_text("k: updated\n")
        timestamp = (t0 / "cfg.enc.yaml").stat().st_mtime_ns
        os.utime(t0 / "cfg.yaml", ns=(timestamp, timestamp))
        (t0 / "empty.enc.yaml").touch()
        (t0 / "empty.yaml").write_text("k: recovered\n")
        os.utime(t0 / "empty.yaml", ns=(timestamp - 1000000000, timestamp - 1000000000))
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        sops = bin_dir / "sops"
        sops.write_text('#!/bin/sh\necho "k: ENC[AES256_GCM,data:new]"\n')
        sops.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        with unittest.mock.patch.dict(os.environ, PATH=self.env["PATH"]):
            self.sync()
        committed_enc = self.git("--git-dir=" + str(self.remote), "show", "main:compositions/app/@tier0/cfg.enc.yaml")
        self.assertEqual(committed_enc, "k: ENC[AES256_GCM,data:new]")
        committed_gi = self.git("--git-dir=" + str(self.remote), "show", "main:compositions/app/@tier0/.gitignore")
        self.assertIn("/cfg.yaml", committed_gi)
        self.assertEqual(self.git("show", "HEAD:compositions/app/@tier0/empty.enc.yaml", cwd=self.repo),
                         "k: ENC[AES256_GCM,data:new]")

    def test_empty_sops_output_preserves_ciphertext_and_blocks_push(self):
        (self.repo / ".sops.yaml").write_text("creation_rules: []\n")
        t0 = self.repo / "compositions/app/@tier0"
        t0.mkdir(parents=True)
        enc = t0 / "cfg.enc.yaml"
        enc.write_text("old ciphertext\n")
        plain = t0 / "cfg.yaml"
        plain.write_text("new plaintext\n")
        timestamp = enc.stat().st_mtime_ns + 1000000000
        os.utime(plain, ns=(timestamp, timestamp))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        sops = bin_dir / "sops"
        sops.write_text("#!/bin/sh\nexit 0\n")
        sops.chmod(0o755)
        before = self.git("rev-parse", "main", cwd=self.repo)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        with (unittest.mock.patch.dict(os.environ, PATH=self.env["PATH"]),
              self.assertRaises(SystemExit)):
            self.sync()
        self.assertIn("empty ciphertext", self.ctx.log.error.call_args.args[0])
        self.assertEqual(enc.read_text(), "old ciphertext\n")
        self.assertEqual(before, self.git("--git-dir=" + str(self.remote), "rev-parse", "main"))

    def test_tier0_oversized_file_blocks_sync(self):
        (self.repo / ".sops.yaml").write_text("creation_rules:\n  - age: age1test\n")
        app = self.repo / "compositions/app"
        t0 = app / "@tier0"
        t0.mkdir(parents=True)
        (t0 / "huge.yaml").write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(SystemExit):
            self.sync()


if __name__ == "__main__":
    unittest.main()
