# tests/test_notify.py
import unittest
from engine.lib.notify import build_text

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

if __name__ == "__main__":
    unittest.main()
