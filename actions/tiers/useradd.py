# actions/tiers/useradd.py
"""Port of tiers/useradd — one noroot system user per composition project; inject the
APEX_UID/APEX_GID/APEX_SHARED_GID block into each project .env."""
import os, re
from engine.descriptor import Meta

METADATA = Meta(summary="Create a noroot system user per composition project; write APEX_UID/GID into .env.")
SHARED_GROUP = "noroot-shared"
START = "# START AUTO GENERATED APEX-NOROOT-USERS"
END = "# END AUTO GENERATED APEX-NOROOT-USERS"

def _sanitize(raw):
    s = re.sub(r"-+$", "", re.sub(r"^-+", "", re.sub(r"[^a-z0-9]+", "-", raw.lower())))
    s = re.sub(r"-+", "-", s) or "project"
    return re.sub(r"-+$", "", s[:24])

def run(ctx, args):
    log, sysx = ctx.log, ctx.sys
    comp = ctx.paths.compositions

    def getent_group(name):
        cp = sysx.run(["getent", "group", name], check=False, capture=True)
        return cp.stdout.split(":")[2].strip() if cp.returncode == 0 else ""

    shared_gid = getent_group(SHARED_GROUP)
    if not shared_gid:
        log.info(f"Creating shared group: {SHARED_GROUP}")
        sysx.sudo(["groupadd", "-r", SHARED_GROUP])
        shared_gid = getent_group(SHARED_GROUP)
    log.info(f"Shared group {SHARED_GROUP} (GID {shared_gid})")

    used = {}
    for proj in sorted(os.listdir(comp)):
        full = os.path.join(comp, proj)
        if not os.path.isdir(full) or proj.startswith(".") or proj.startswith("@"):
            continue
        base = f"noroot-{_sanitize(proj)}"
        cand, i = base, 1
        while cand in used:
            cand = f"{base}-{i}"; i += 1
        used[cand] = proj
        exists = sysx.run(["getent", "passwd", cand], check=False, capture=True)
        if exists.returncode != 0:
            sysx.sudo(["useradd", "-r", "-M", "-d", "/nonexistent", "-s", "/usr/sbin/nologin",
                       "-c", f"System user for project {proj}", "-U", cand])
            sysx.run(["bash", "-c", f"sudo passwd -l {cand} >/dev/null 2>&1 || true"])
            log.success(f"Created system user {cand} (project {proj})")
        else:
            log.info(f"User {cand} exists (project {proj})")
        if not sysx.ok(["bash", "-c", f"groups {cand} 2>/dev/null | grep -q '\\b{SHARED_GROUP}\\b'"]):
            sysx.sudo(["usermod", "-aG", SHARED_GROUP, cand])
        pw = sysx.run(["getent", "passwd", cand], check=False, capture=True)
        parts = pw.stdout.split(":") if pw.returncode == 0 else []
        uid, gid = (parts[2], parts[3]) if len(parts) >= 4 else ("", "")
        env_path = os.path.join(full, ".env")
        if uid and gid and shared_gid and os.path.exists(env_path):
            _inject_env(ctx, env_path, uid, gid, shared_gid)
        elif os.path.exists(env_path):
            log.warn(f"Could not determine UID/GID for {cand}; skipping .env update")
        else:
            log.info(f"No .env present for project {proj}")
    log.success("noroot user provisioning completed.")

def _inject_env(ctx, env_path, uid, gid, shared_gid):
    actual = os.path.realpath(env_path)
    with open(actual) as f:
        text = f.read()
    block = f"{START}\nAPEX_UID={uid}\nAPEX_GID={gid}\nAPEX_SHARED_GID={shared_gid}\n{END}\n"
    if START in text:
        new = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n", block, text, flags=re.S)
    else:
        new = block + "\n" + text
    # backup=False: bash never left copies; a timestamped .bak would pile up a
    # secret-bearing .env duplicate per run next to the bind-mounted project dir.
    ctx.host.write_file(actual, new, backup=False, sudo=True)
    ctx.log.success(f"Updated {actual} with APEX_UID/GID/SHARED_GID")
