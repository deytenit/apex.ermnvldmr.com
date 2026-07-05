# actions/utils/lint-docker-compose.py
"""Port of utils/lint-docker-compose (dclint via docker) + stdlib anchor-IP validation:
every ipv4_address in compositions/*/docker-compose.yml must be unique and inside the
node's /24 (SP3 §6.2). No new deps — ipaddress + re are stdlib."""
import ipaddress, os, re, shutil
from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Lint docker-compose files (dclint via docker); validate anchor IPs in the node /24.",
                args=[Flag("--hook", "Only process staged compose files (pre-commit).")])

def run(ctx, args):
    log, s = ctx.log, ctx.sys
    repo = ctx.paths.repo_root
    rc_ip = _validate_anchor_ips(ctx)
    if not shutil.which("docker"):
        log.error("docker is not installed or not in PATH."); return rc_ip or 1

    files_string, targets = "", ["."]
    if args.hook:
        cp = s.run(["git", "-C", repo, "diff", "--cached", "--name-only", "--diff-filter=ACM",
                    "--", "*docker-compose.yml", "*docker-compose.yaml",
                    "docker-compose*.yml", "docker-compose*.yaml"], check=False, capture=True)
        files_string = (cp.stdout or "").strip()
        if not files_string:
            log.info("No staged docker-compose files to lint."); return rc_ip
        targets = [f"/workdir/{f}" for f in files_string.splitlines()]

    log.info("Running dclint --fix (via docker)...")
    r = s.run(["docker", "run", "--rm", "-v", f"{repo}:/workdir", "-w", "/workdir", "node:lts-alpine",
               "npx", "--yes", "dclint", "-e", "@tier1", "-e", "@tier2", "-e", "@tier3", "-e", ".env",
               "-r", "--fix", *targets], check=False)
    if r.returncode != 0:
        log.error(f"dclint issues (exit {r.returncode})."); return r.returncode or rc_ip
    if args.hook and files_string:
        for f in files_string.splitlines():
            s.run(["git", "-C", repo, "add", f], check=False)
    log.success("Lint completed.")
    return rc_ip

def _validate_anchor_ips(ctx) -> int:
    comp = ctx.paths.compositions
    if not os.path.isdir(comp):
        return 0
    try:
        subnet = ipaddress.ip_network(ctx.node.subnet) if ctx.node else None
    except Exception:
        subnet = None
    seen, rc = {}, 0
    for proj in sorted(os.listdir(comp)):
        f = os.path.join(comp, proj, "docker-compose.yml")
        if not os.path.isfile(f):
            continue
        try:
            text = open(f).read()
        except OSError as e:
            ctx.log.error(f"Cannot read {f}: {e}"); rc = 1
            continue
        for m in re.finditer(r"ipv4_address:\s*([0-9.]+)", text):
            ip = m.group(1)
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:   # e.g. "10.0.0." — a lint error, not a crash
                ctx.log.error(f"Malformed anchor IP {ip!r} in {proj}"); rc = 1
                continue
            if ip in seen:
                ctx.log.error(f"Duplicate anchor IP {ip} in {proj} and {seen[ip]}"); rc = 1
            seen[ip] = proj
            if subnet is not None and addr not in subnet:
                ctx.log.error(f"Anchor IP {ip} ({proj}) not in node subnet {subnet}"); rc = 1
    if rc == 0:
        ctx.log.info("Anchor IP validation passed.")
    return rc
