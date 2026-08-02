# tests/test_notify.py
import os
import tempfile
import unittest
from engine.lib.notify import build_text, resolve_bot_url

class TestNotify(unittest.TestCase):
    def test_instance_is_the_host(self):
        t = build_text("Title", "msg", "node1.example.com", "INFO", "2026-07-04 00:00:00 UTC")
        self.assertIn("node1.example.com", t)
        self.assertIn("#instance_node1_example_com", t)   # dots -> underscores in the hashtag

    def test_html_escaped_and_truncated(self):
        t = build_text("A & B", "<script>" + "x" * 300, "d", "ERROR", "T")
        self.assertIn("A &amp; B", t)
        self.assertIn("&lt;script&gt;", t)
        self.assertIn("...", t)
        self.assertIn("escalation:</b> critical", t.lower())

    def test_success_is_resolved(self):
        t = build_text("T", "m", "d", "SUCCESS", "T")
        self.assertIn("Resolved", t)
        self.assertIn("✅", t)

    def test_literal_bot_url_is_unchanged(self):
        self.assertEqual(resolve_bot_url("https://example.test/bot"),
                         "https://example.test/bot")

    def test_bot_url_file_reference(self):
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "w") as f:
                f.write("https://example.test/secret\n")
            self.assertEqual(resolve_bot_url("@" + path),
                             "https://example.test/secret")
        finally:
            os.unlink(path)

if __name__ == "__main__":
    unittest.main()
