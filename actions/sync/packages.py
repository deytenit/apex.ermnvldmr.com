# actions/sync/packages.py
"""Port of sync/packages — apt upgradable + docker image (skopeo) update check; Telegram."""
import os, re, shutil
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Check pending apt updates + docker image updates; Telegram notify.",
                args=[Arg("telegram_bot_url", "Telegram Bot URL for notifications")])
TITLE = "Node Security Updates"

def run(ctx, args):
    log, node, url, s = ctx.log, ctx.node.name, args.telegram_bot_url, ctx.sys
    if not os.path.isfile("/etc/debian_version"):
        log.error("Requires a Debian-based system."); raise SystemExit(1)

    log.info("Updating package lists...")
    if s.ok(["sudo", "apt-get", "update"]):
        cp = s.run(["bash", "-c",
                    "sudo apt-get --just-print upgrade 2>/dev/null | grep '^Inst' | cut -d' ' -f2 | sort"],
                   check=False, capture=True)
        upd = [l for l in (cp.stdout or "").splitlines() if l.strip()]
        packages = (f"*Packages ({len(upd)}):* " + ", ".join(f"`{u}`" for u in upd)) if upd else "*Packages:* _none_"
    else:
        packages = "*Packages:* _Failed to check_"

    if not shutil.which("docker"):
        images = "*Images:* _Docker not available_"
    elif not shutil.which("skopeo"):
        images = "*Images:* _skopeo not available_"
    else:
        cp = s.run(["bash", "-c", "docker ps --format '{{.Image}}' | sort -u"], check=False, capture=True)
        outdated = []
        for image in [l for l in (cp.stdout or "").splitlines() if l.strip()]:
            name, sep, tag = image.partition(":")
            tag = tag or "latest"
            ld = s.run(["bash", "-c",
                        f"docker image inspect '{image}' --format '{{{{index .RepoDigests 0}}}}' 2>/dev/null | cut -d'@' -f2"],
                       check=False, capture=True).stdout.strip()
            if not ld:
                continue
            if "/" not in name:
                reg = f"docker.io/library/{name}"
            elif not re.match(r".*\..*/", name):
                reg = f"docker.io/{name}"
            else:
                reg = name
            rd = s.run(["bash", "-c",
                        f"skopeo inspect 'docker://{reg}:{tag}' 2>/dev/null | "
                        f"grep -o '\"Digest\"[[:space:]]*:[[:space:]]*\"[^\"]*\"' | cut -d'\"' -f4"],
                       check=False, capture=True).stdout.strip()
            if rd and ld != rd:
                disp = name[len("docker.io/"):] if name.startswith("docker.io/") else name
                disp = disp[len("library/"):] if disp.startswith("library/") else disp
                outdated.append(disp)
        images = (f"*Images ({len(outdated)}):* " + ", ".join(f"`{i}`" for i in outdated)) if outdated else "*Images:* _none_"

    ctx.notify.info(TITLE, url, f"*APT:*\n{packages}\n\n*Skopeo:*\n{images}", node)
    log.success("Security update check completed.")
