# actions/configure.py
"""Umbrella: run every configure/* action in a stable order (SP2 §3.1)."""
import os
from engine.descriptor import Meta, Flag, build_parser
from engine import overlay

METADATA = Meta(summary="Run every configure/* action in stable order.",
                args=[Flag("--dry-run", "Pass --dry-run to sub-actions that support it.")])
ORDER = ["configure/base", "configure/ufw", "configure/crowdsec",
         "configure/cron", "configure/systemd", "configure/routing"]

def run(ctx, args):
    commons_actions = os.path.join(ctx.paths.commons, "actions")
    prop = os.path.join(ctx.paths.proprietaries, "actions")
    prop = prop if os.path.isdir(prop) else None
    names = [i.name for i in overlay.discover(commons_actions, prop) if i.name.startswith("configure/")]
    ordered = [n for n in ORDER if n in names] + sorted(n for n in names if n not in ORDER)
    rc = 0
    for name in ordered:
        r = overlay.resolve(name, commons_actions, prop)
        mod = overlay.load_module(r.path, "apex_umbrella_" + name.replace("/", "_"))
        if getattr(mod, "DISABLED", None):
            ctx.log.warn(f"Skipping disabled {name}"); continue
        meta = getattr(mod, "METADATA")
        has_dry = any(getattr(a, "name", None) == "--dry-run" for a in meta.args)
        argv = ["--dry-run"] if (args.dry_run and has_dry) else []
        sub_args = build_parser(meta, f"apex {name}").parse_args(argv)
        ctx.log.info(f"=== {name} ===")
        try:
            mod.run(ctx, sub_args)
        except SystemExit as e:
            if e.code:
                ctx.log.error(f"{name} exited {e.code}"); rc = rc or e.code
    return rc
