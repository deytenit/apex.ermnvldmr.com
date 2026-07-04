# tests/test_descriptor.py
import unittest
from engine.descriptor import Meta, Arg, Flag, Opt, Rest, build_parser

class TestDescriptor(unittest.TestCase):
    def test_required_positional(self):
        m = Meta("s", args=[Arg("src", "source")])
        ns = build_parser(m, "apex x").parse_args(["a"])
        self.assertEqual(ns.src, "a")

    def test_optional_positional_defaults_none(self):
        m = Meta("s", args=[Arg("src", "s"), Arg("bot", "b", required=False)])
        ns = build_parser(m, "apex x").parse_args(["a"])
        self.assertEqual(ns.src, "a")
        self.assertIsNone(ns.bot)

    def test_flag(self):
        m = Meta("s", args=[Flag("--dry-run", "d")])
        self.assertFalse(build_parser(m, "x").parse_args([]).dry_run)
        self.assertTrue(build_parser(m, "x").parse_args(["--dry-run"]).dry_run)

    def test_opt_with_default(self):
        m = Meta("s", args=[Opt("--tier", "t", default="/d")])
        self.assertEqual(build_parser(m, "x").parse_args([]).tier, "/d")

    def test_rest_collects_remainder(self):
        m = Meta("s", args=[Arg("action", "a"), Rest("extra", "e")])
        ns = build_parser(m, "x").parse_args(["up", "--force-recreate", "-x"])
        self.assertEqual(ns.action, "up")
        self.assertEqual(ns.extra, ["--force-recreate", "-x"])

    def test_usage_error_exits_64(self):
        m = Meta("s", args=[Arg("src", "s")])
        with self.assertRaises(SystemExit) as cm:
            build_parser(m, "x").parse_args([])   # missing required
        self.assertEqual(cm.exception.code, 64)

if __name__ == "__main__":
    unittest.main()
