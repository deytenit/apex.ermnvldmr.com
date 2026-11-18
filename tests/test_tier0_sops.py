import os
import tempfile
import unittest
from pathlib import Path

from engine.lib import tier0


class TestTier0SopsAndGuardrails(unittest.TestCase):
    def test_plain_to_enc_and_enc_to_plain_roundtrip(self):
        cases = [
            (Path("AdGuardHome.yaml"), Path("AdGuardHome.enc.yaml")),
            (Path("sub/config.jsonc"), Path("sub/config.enc.jsonc")),
            (Path("gitea/app.ini"), Path("gitea/app.enc.ini")),
            (Path("dms/adam@ermnvldmr.com.dovecot.sieve"), Path("dms/adam@ermnvldmr.com.dovecot.enc.sieve")),
            (Path("radicale/users"), Path("radicale/users.enc")),
            (Path("telegram-send-message-url"), Path("telegram-send-message-url.enc")),
            (Path("opendkim/mail.private"), Path("opendkim/mail.enc.private")),
        ]
        for plain, enc in cases:
            with self.subTest(plain=plain):
                self.assertTrue(tier0.is_enc_path(enc))
                self.assertFalse(tier0.is_enc_path(plain))
                self.assertEqual(tier0.plain_to_enc(plain), enc)
                self.assertEqual(tier0.enc_to_plain(enc), plain)

    def test_sops_io_types_prefers_structured_formats_and_falls_back_to_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yaml_f = root / "cfg.yaml"
            yaml_f.write_text("key: val\n")
            self.assertEqual(tier0.sops_io_types(yaml_f), ("yaml", "yaml"))

            ini_f = root / "app.ini"
            ini_f.write_text("[server]\nPORT = 3000\n")
            self.assertEqual(tier0.sops_io_types(ini_f), ("ini", "ini"))

            jsonc_valid = root / "config.jsonc"
            jsonc_valid.write_text('{"inbounds": []}\n')
            self.assertEqual(tier0.sops_io_types(jsonc_valid), ("json", "json"))

            jsonc_comments = root / "commented.jsonc"
            jsonc_comments.write_text('// comment\n{"inbounds": []}\n')
            self.assertEqual(tier0.sops_io_types(jsonc_comments), ("raw", "raw"))

            raw_f = root / "users"
            raw_f.write_text("adam:$2y$05$hash\n")
            self.assertEqual(tier0.sops_io_types(raw_f), ("raw", "raw"))

    def test_sync_tier0_gitignore_lists_decrypted_counterparts_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = Path(tmp) / "@tier0"
            (t0 / "xray" / "data").mkdir(parents=True)
            (t0 / "xray" / "data" / "config.enc.jsonc").write_text("{}\n")
            (t0 / "telegram-send-message-url.enc").write_text("ENC\n")
            (t0 / "traefik" / "dynamic").mkdir(parents=True)
            (t0 / "traefik" / "dynamic" / "git.yml").write_text("http: {}\n")

            tier0.sync_tier0_gitignore(t0)
            gi = (t0 / ".gitignore").read_text()
            self.assertEqual(
                gi,
                "# Managed by APEX: decrypted @tier0 secret targets\n"
                "/telegram-send-message-url\n"
                "/xray/data/config.jsonc\n",
            )

    def test_validate_tier0_rejects_oversized_and_high_churn_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp) / "compositions"
            t0 = comp / "apex" / "@tier0"
            t0.mkdir(parents=True)
            # 1. Oversized file (>256 KB)
            big = t0 / "huge.yaml"
            big.write_bytes(b"x" * (tier0.MAX_TIER0_BYTES + 1))
            # 2. High-churn SQLite DB and acme.json
            (t0 / "stats.db").write_bytes(b"sqlite")
            (t0 / "acme.json").write_text("{}")
            # 3. Unignored plaintext counterpart next to .enc file
            (t0 / "secret.enc.yaml").write_text("k: ENC[...]\n")
            (t0 / "secret.yaml").write_text("k: plain\n")

            errors = tier0.validate_tier0_compositions(comp)
            self.assertEqual(len(errors), 4)
            self.assertTrue(any("exceeds 256 KB" in e for e in errors))
            self.assertTrue(any("stats.db" in e for e in errors))
            self.assertTrue(any("acme.json" in e for e in errors))
            self.assertTrue(any("secret.yaml" in e and ".gitignore" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
