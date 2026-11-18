# actions/tier/chown.py
"""Port of tier/chown — ownership: {tier}/{proj} -> uid:gid; {tier}/shared/{proj} ->
uid:shared_gid (+sticky); base/node-infra -> root:root. Reads APEX_UID/GID from each .env."""
import os
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Apply tier ownership/permissions from each project's APEX_UID/GID/.env.",
                args=[Arg("tier1_path", "Path to tier 1", required=False),
                      Arg("tier2_path", "Path to tier 2", required=False),
                      Arg("tier3_path", "Path to tier 3", required=False)])
SHARED_GROUP = "noroot-shared"

def _env_val(ctx, path, key):
    if not os.path.exists(path):
        return ""
    try:
        with open(os.path.realpath(path), encoding="utf-8") as f:
            lines = f.readlines()
    except PermissionError:
        cp = ctx.sys.sudo(["cat", os.path.realpath(path)], capture=True, check=False)
        if cp.returncode == 0:
            lines = (cp.stdout or "").splitlines()
        else:
            return ""
    except Exception:
        return ""

    for line in lines:
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
        log.error(f"Shared group '{SHARED_GROUP}' not found. Run tier/useradd first."); raise SystemExit(1)

    log.info("Ensuring base structures owned by root...")
    tier0_root = ctx.paths.root_tier0
    if os.path.isdir(tier0_root):
        s.sudo(["chown", "adam:adam", tier0_root])
        s.sudo(["chmod", "0770", tier0_root])
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
        # opt-out: .apex-root-owned forces this project's tier data (dir + children,
        # recursively) to root:root — for services that run as root and reject
        # non-root-owned data (e.g. seafile-mc). Blocks noroot ownership entirely.
        proj_tier0 = os.path.join(full, "@tier0")
        if os.path.isfile(os.path.join(full, ".apex-root-owned")):
            log.info(f"Applying root ownership for {proj} (.apex-root-owned)")
            if os.path.isdir(proj_tier0):
                s.sudo(["chown", "-R", "adam:root", proj_tier0])
                s.sudo(["chmod", "-R", "u+rwX,g+rwX,o-rwx", proj_tier0])
                log.success(f"  Owned {proj_tier0} (adam:root)")
            for tp in tiers:
                for d in (os.path.join(tp, proj), os.path.join(tp, "shared", proj)):
                    if os.path.isdir(d):
                        s.sudo(["chown", "-R", "root:root", d]); log.success(f"  Owned {d} (root)")
            continue
        env_path = os.path.join(full, ".env")
        uid = _env_val(ctx, env_path, "APEX_UID")
        gid = _env_val(ctx, env_path, "APEX_GID")
        sgid = _env_val(ctx, env_path, "APEX_SHARED_GID")
        if not (uid and gid and sgid):
            log.warn(f"Incomplete APEX_UID/GID/SHARED_GID for {proj}. Run tier/useradd first. Skipping.")
            continue
        log.info(f"Applying permissions for {proj} (UID={uid} GID={gid} SHARED_GID={sgid})")
        if os.path.isdir(proj_tier0):
            s.sudo(["chown", "-R", f"adam:{gid}", proj_tier0])
            s.sudo(["chmod", "-R", "u+rwX,g+rwX,o-rwx", proj_tier0])
            log.success(f"  Owned {proj_tier0} (adam:{gid})")
        for tp in tiers:
            pdir = os.path.join(tp, proj)
            sdir = os.path.join(tp, "shared", proj)
            if os.path.isdir(pdir):
                s.sudo(["chown", "-R", f"{uid}:{gid}", pdir]); log.success(f"  Owned {pdir}")
                env_file = os.path.join(pdir, ".env")
                if os.path.isfile(env_file):
                    s.sudo(["chown", f"adam:{gid}", env_file])
                    s.sudo(["chmod", "0640", env_file])
            if os.path.isdir(sdir):
                s.sudo(["chown", "-R", f"{uid}:{sgid}", sdir])
                s.sudo(["chmod", "+t", sdir]); log.success(f"  Owned {sdir} (sticky)")
    log.success("Permissions applied.")
