# actions/tiers/chown.py
"""Port of tiers/chown — ownership: {tier}/{proj} -> uid:gid; {tier}/shared/{proj} ->
uid:shared_gid (+sticky); base/node-infra -> root:root. Reads APEX_UID/GID from each .env."""
import os
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Apply tier ownership/permissions from each project's APEX_UID/GID/.env.",
                args=[Arg("tier1_path", "Path to tier 1", required=False),
                      Arg("tier2_path", "Path to tier 2", required=False),
                      Arg("tier3_path", "Path to tier 3", required=False)])
SHARED_GROUP = "noroot-shared"

def _env_val(path, key):
    if not os.path.exists(path):
        return ""
    for line in open(os.path.realpath(path)):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

def run(ctx, args):
    log, s = ctx.log, ctx.sys
    comp = ctx.paths.compositions
    # Defaults are the TIER ROOTS (compositions/@tierN, bash $ROOT_TIERn) — NOT
    # ctx.paths.tierN, which is the apex core project's data dir one level deeper.
    tiers = [os.path.realpath(args.tier1_path or ctx.paths.root_tier1),
             os.path.realpath(args.tier2_path or ctx.paths.root_tier2),
             os.path.realpath(args.tier3_path or ctx.paths.root_tier3)]

    cp = s.run(["getent", "group", SHARED_GROUP], check=False, capture=True)
    if cp.returncode != 0:
        log.error(f"Shared group '{SHARED_GROUP}' not found. Run tiers/useradd first."); raise SystemExit(1)

    log.info("Ensuring base structures owned by root...")
    for tp in tiers:
        s.sudo(["chown", "root:root", tp])
        shared = os.path.join(tp, "shared")
        if os.path.isdir(shared):       # dir inode only — recursing would clobber
            s.sudo(["chown", "root:root", shared])  # skipped projects' shared subtrees
        infra = os.path.join(tp, "node-infra")
        if os.path.isdir(infra):
            s.sudo(["chown", "-R", "root:root", infra])

    for proj in sorted(os.listdir(comp)):
        full = os.path.join(comp, proj)
        if not os.path.isdir(full) or proj.startswith(".") or proj.startswith("@"):
            continue
        env_path = os.path.join(full, ".env")
        uid = _env_val(env_path, "APEX_UID")
        gid = _env_val(env_path, "APEX_GID")
        sgid = _env_val(env_path, "APEX_SHARED_GID")
        if not (uid and gid and sgid):
            log.warn(f"Incomplete APEX_UID/GID/SHARED_GID for {proj}. Run tiers/useradd first. Skipping.")
            continue
        log.info(f"Applying permissions for {proj} (UID={uid} GID={gid} SHARED_GID={sgid})")
        for tp in tiers:
            pdir = os.path.join(tp, proj)
            sdir = os.path.join(tp, "shared", proj)
            if os.path.isdir(pdir):
                s.sudo(["chown", "-R", f"{uid}:{gid}", pdir]); log.success(f"  Owned {pdir}")
            if os.path.isdir(sdir):
                s.sudo(["chown", "-R", f"{uid}:{sgid}", sdir])
                s.sudo(["chmod", "+t", sdir]); log.success(f"  Owned {sdir} (sticky)")
    log.success("Permissions applied.")
