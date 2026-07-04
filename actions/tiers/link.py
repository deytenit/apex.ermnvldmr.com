# actions/tiers/link.py
"""Port of tiers/link — @tier symlink structure for the node + each composition project."""
import os
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Create @tier symlink structure for the node + each composition project.",
                args=[Arg("tier1_path", "Path to tier 1 storage root"),
                      Arg("tier2_path", "Path to tier 2 storage root"),
                      Arg("tier3_path", "Path to tier 3 storage root")])

def run(ctx, args):
    log, s = ctx.log, ctx.sys
    comp = ctx.paths.compositions
    tiers = [os.path.realpath(args.tier1_path),
             os.path.realpath(args.tier2_path),
             os.path.realpath(args.tier3_path)]

    def confirm(msg):
        return input(f"{msg} [y/N]: ").strip().lower() in ("y", "yes")

    def safe_remove(link, desc):
        if os.path.islink(link):
            s.sudo(["rm", link]); return True
        if os.path.exists(link):
            log.warn(f"Path exists but is not a symlink: {link}")
            if confirm(f"Remove existing {desc} (NOT a symlink)?"):
                s.sudo(["rm", "-rf", link]); return True
            return False
        return True

    def mklink(target, link, desc):
        s.sudo(["ln", "-snf", target, link])
        log.success(f"Created {desc} symlink: {link} -> {target}")

    log.info("Starting @tier setup...")
    for t in tiers:
        s.sudo(["mkdir", "-p", t])
    # node-level @tierN -> tier roots
    for i, n in enumerate((1, 2, 3)):
        link = os.path.join(comp, f"@tier{n}")
        if safe_remove(link, f"node-@tier{n}"):
            mklink(tiers[i], link, f"node-@tier{n}")
    # per-project (every composition subdir except dot/@)
    for proj in sorted(os.listdir(comp)):
        full = os.path.join(comp, proj)
        if not os.path.isdir(full) or proj.startswith(".") or proj.startswith("@"):
            continue
        log.info(f"Processing project: {proj}")
        for i, n in enumerate((1, 2, 3)):
            base = tiers[i]
            s.sudo(["mkdir", "-p", os.path.join(base, "shared")])
            s.sudo(["mkdir", "-p", os.path.join(base, "shared", proj)])
            s.sudo(["mkdir", "-p", os.path.join(base, proj)])
            shared_link = os.path.join(base, proj, "shared")
            if safe_remove(shared_link, "shared-internal"):
                s.sudo(["ln", "-snf", "../shared", shared_link])
            link = os.path.join(full, f"@tier{n}")
            if safe_remove(link, f"@tier{n}"):
                mklink(os.path.join(base, proj), link, f"@tier{n}")
        env_link = os.path.join(full, ".env")
        env_target = os.path.join(tiers[0], proj, ".env")
        if safe_remove(env_link, ".env"):
            if not os.path.isfile(env_target):
                s.sudo(["touch", env_target])
            mklink(env_target, env_link, ".env")
    log.success("@tier setup completed.")
