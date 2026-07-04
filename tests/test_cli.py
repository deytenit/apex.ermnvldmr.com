# tests/test_cli.py
import os, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

def run(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "apex"), *args],
                          text=True, capture_output=True)

class TestCli(unittest.TestCase):
    def test_help_lists_and_exits_zero(self):
        r = run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Available actions", r.stdout)

    def test_unknown_action_exits_one(self):
        r = run("no/such")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Unknown action", r.stderr)

if __name__ == "__main__":
    unittest.main()
