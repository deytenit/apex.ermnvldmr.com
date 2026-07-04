# engine/context.py
"""Context (ctx) — the single seam between an action and the world (SP2 §2.1)."""
from __future__ import annotations
import os

from engine.lib.log import Log
from engine.lib.sys import Sys
from engine.lib.tpl import Tpl
from engine.lib.host import Host
from engine.lib.ufw import Ufw
from engine.lib.crowdsec import Crowdsec
from engine.lib.systemd import Systemd
from engine.lib.notify import Notify


class Paths:
    def __init__(self, repo_root: str, commons_dir: str):
        self.repo_root = repo_root
        self.commons = commons_dir
        self.proprietaries = os.path.join(repo_root, "proprietaries")
        self.configs = os.path.join(repo_root, "configs")
        self.compositions = os.path.join(repo_root, "compositions")
        self.core = os.path.join(self.compositions, "apex")

    def _tier(self, n: int) -> str:
        return os.path.realpath(os.path.join(self.core, f"@tier{n}"))

    def _stier(self, n: int) -> str:
        return os.path.realpath(os.path.join(self.core, f"@tier{n}", "shared"))

    @property
    def tier1(self): return self._tier(1)
    @property
    def tier2(self): return self._tier(2)
    @property
    def tier3(self): return self._tier(3)
    @property
    def stier1(self): return self._stier(1)
    @property
    def stier2(self): return self._stier(2)
    @property
    def stier3(self): return self._stier(3)


class _CommonsWrap:
    """ctx.commons — lazily loads and runs the shadowed commons action (opt-in wrap)."""
    def __init__(self, ctx, shadowed_path):
        self._ctx = ctx
        self._path = shadowed_path
        self._mod = None

    def run(self, args):
        if not self._path:
            raise RuntimeError("ctx.commons is only available when a commons action is shadowed")
        from engine import overlay
        if self._mod is None:
            self._mod = overlay.load_module(self._path, "apex_commons_shadowed")
        return self._mod.run(self._ctx, args)


class Context:
    def __init__(self, node, paths: Paths, action_name: str, shadowed=None):
        self.node = node
        self.paths = paths
        self.action = action_name
        name = node.name if node else "global"
        self.log = Log(name, action_name)
        self.sys = Sys(self.log)
        self.tpl = Tpl(paths.configs, self.log)
        self.host = Host(self.log, self.sys)
        self.ufw = Ufw(self.log, self.sys, self.host)
        self.crowdsec = Crowdsec(self.log, self.sys, self.tpl, self.host)
        self.systemd = Systemd(self.log, self.sys, self.tpl, self.host)
        self.notify = Notify(self.log, name)
        self._shadowed = shadowed
        self._commons = None

    @property
    def commons(self) -> _CommonsWrap:
        if self._commons is None:
            self._commons = _CommonsWrap(self, self._shadowed)
        return self._commons

    def vars(self) -> dict:
        """Substitution variable set for templating: os.environ + apex identity/paths."""
        v = dict(os.environ)
        v["APEX_COMMONS"] = self.paths.commons        # absolute launcher dir (for crontab/systemd)
        v["APEX_REPO_ROOT"] = self.paths.repo_root
        if self.node:
            v.update({
                "APEX_NODE": self.node.name,
                "APEX_CLUSTER": self.node.cluster,
                "APEX_SUBNET": self.node.subnet,
                "APEX_NODE_FQDN": self.node.fqdn,
                "APEX_TIER1": self.paths.tier1, "APEX_TIER2": self.paths.tier2,
                "APEX_TIER3": self.paths.tier3,
                "APEX_TIER1_SHARED": self.paths.stier1,
                "APEX_TIER2_SHARED": self.paths.stier2,
                "APEX_TIER3_SHARED": self.paths.stier3,
            })
        return v
