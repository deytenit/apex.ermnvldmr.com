# tests/test_overlay.py
import os, tempfile, unittest
from engine import overlay

def _write(root, rel, body):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(body)
    return p

ACT = 'from engine.descriptor import Meta\nMETADATA = Meta(summary="{s}")\ndef run(ctx, args):\n    return "{r}"\n'

class TestOverlay(unittest.TestCase):
    def setUp(self):
        self.commons = tempfile.mkdtemp()
        self.prop = tempfile.mkdtemp()

    def test_commons_only(self):
        _write(self.commons, "config/ufw.py", ACT.format(s="commons ufw", r="C"))
        r = overlay.resolve("config/ufw", self.commons, self.prop)
        self.assertIsNotNone(r); self.assertIsNone(r.shadowed)
        self.assertTrue(r.path.startswith(self.commons))

    def test_override_exposes_shadowed(self):
        _write(self.commons, "config/ufw.py", ACT.format(s="commons", r="C"))
        _write(self.prop, "config/ufw.py", ACT.format(s="local", r="L"))
        r = overlay.resolve("config/ufw", self.commons, self.prop)
        self.assertTrue(r.path.startswith(self.prop))     # local wins
        self.assertTrue(r.shadowed.startswith(self.commons))

    def test_unknown_returns_none(self):
        self.assertIsNone(overlay.resolve("no/such", self.commons, self.prop))

    def test_discover_marks_sources_and_disabled(self):
        _write(self.commons, "config/ufw.py", ACT.format(s="commons ufw", r="C"))
        _write(self.commons, "repository/sync.py", ACT.format(s="sync repo", r="C"))
        _write(self.prop, "config/ufw.py", ACT.format(s="local ufw", r="L"))
        _write(self.prop, "sync/minecraft.py",
               'from engine.descriptor import Meta\nMETADATA = Meta(summary="mc")\nDISABLED = "wip"\ndef run(ctx, args): pass\n')
        infos = {i.name: i for i in overlay.discover(self.commons, self.prop)}
        self.assertEqual(infos["config/ufw"].source, "override")
        self.assertEqual(infos["repository/sync"].source, "commons")
        self.assertEqual(infos["sync/minecraft"].source, "local")
        self.assertEqual(infos["sync/minecraft"].disabled, "wip")
        self.assertEqual(infos["config/ufw"].summary, "local ufw")

    def test_only_entity_action_paths_can_be_listed_and_dispatched(self):
        for name in ("configure", "config/nested/tool", "config/tool"):
            _write(self.prop, name + ".py", ACT.format(s="test", r="ok"))
        names = [item.name for item in overlay.discover(self.commons, self.prop)]
        self.assertEqual(names, ["config/tool"])
        for name in ("configure", "config/nested/tool", "config/../configure"):
            self.assertIsNone(overlay.resolve(name, self.commons, self.prop))

    def test_load_module_runs(self):
        p = _write(self.commons, "x/y.py", ACT.format(s="s", r="Z"))
        mod = overlay.load_module(p, "apex_action_x_y")
        self.assertEqual(mod.run(None, None), "Z")

if __name__ == "__main__":
    unittest.main()
