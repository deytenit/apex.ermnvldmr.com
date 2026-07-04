# tests/test_host.py
import unittest
from engine.lib.host import apply_block

class TestApplyBlock(unittest.TestCase):
    def test_insert_after_anchor(self):
        text = "line1\n# End required lines\nline2\n"
        out, injected = apply_block(text, "ruleA\nruleB",
                                    "# START APEX.ERMNVLDMR.COM RULES",
                                    "# END APEX.ERMNVLDMR.COM RULES",
                                    anchor="# End required lines")
        self.assertTrue(injected)
        self.assertIn("# End required lines\n# START APEX.ERMNVLDMR.COM RULES\nruleA\nruleB\n# END APEX.ERMNVLDMR.COM RULES", out)

    def test_self_heal_removes_old_and_new_blocks(self):
        text = ("a\n# End required lines\n"
                "# START ROOT.ERMNVLDMR.COM RULES\nold\n# END ROOT.ERMNVLDMR.COM RULES\n"
                "# START APEX.ERMNVLDMR.COM RULES\nprev\n# END APEX.ERMNVLDMR.COM RULES\n"
                "b\n")
        out, injected = apply_block(text, "new",
                                    "# START APEX.ERMNVLDMR.COM RULES",
                                    "# END APEX.ERMNVLDMR.COM RULES",
                                    anchor="# End required lines",
                                    old_pairs=[("# START ROOT.ERMNVLDMR.COM RULES",
                                                "# END ROOT.ERMNVLDMR.COM RULES")])
        self.assertTrue(injected)
        self.assertNotIn("old", out)
        self.assertNotIn("prev", out)
        self.assertEqual(out.count("# START APEX.ERMNVLDMR.COM RULES"), 1)
        self.assertIn("new", out)

    def test_anchor_missing_does_not_inject(self):
        text = "a\nb\n"
        out, injected = apply_block(text, "x", "S", "E", anchor="# End required lines")
        self.assertFalse(injected)
        self.assertEqual(out, text)

if __name__ == "__main__":
    unittest.main()
