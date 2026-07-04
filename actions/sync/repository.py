# actions/sync/repository.py
"""Port of sync/repository — capture-up commit + rebase onto origin/trunk + force-push."""
import os
from datetime import datetime
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Capture-up: commit node changes, rebase onto origin/trunk, force-push sync/<node>.",
                args=[Arg("telegram_bot_url", "Telegram Bot URL for notifications")])
TITLE = "Annual Remote Sync"

def run(ctx, args):
    log, node, url = ctx.log, ctx.node.name, args.telegram_bot_url
    repo, commons = ctx.paths.repo_root, ctx.paths.commons
    env = dict(os.environ,
               GIT_AUTHOR_NAME="Adam Jensen [bot]", GIT_AUTHOR_EMAIL="adam@ermnvldmr.com",
               GIT_COMMITTER_NAME="Adam Jensen [bot]", GIT_COMMITTER_EMAIL="adam@ermnvldmr.com")

    def git(*a, check=True):
        return ctx.sys.run(["git", "-C", repo, *a], check=check, env=env)

    try:
        cp = ctx.sys.run(["git", "-C", commons, "status", "--porcelain"], capture=True)
        if (cp.stdout or "").strip():
            log.error(f"Commons submodule at {commons} has uncommitted changes. Aborting.")
            raise SystemExit(1)
        branch = f"sync/{node}"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if ctx.sys.ok(["git", "-C", repo, "rev-parse", "--verify", branch]):
            git("checkout", branch)
        else:
            git("checkout", "-b", branch)
        git("add", "-A")
        git("submodule", "update", "--remote", "--recursive")
        git("add", "commons")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            git("commit", "-m", f"[{node}] Auto-commit: {ts}")
        else:
            log.info("No changes to commit.")
        git("fetch", "origin", "trunk")
        if git("rebase", "origin/trunk", check=False).returncode != 0:
            log.error("Rebase conflict — aborting rebase; manual intervention required.")
            git("rebase", "--abort", check=False)
            ctx.notify.error(TITLE, url, "Rebase conflict. Manual intervention required.", node)
            raise SystemExit(1)
        git("submodule", "update", "--init", "--recursive")
        git("push", "--force-with-lease", "origin", branch)
        log.success(f"Pushed {branch}.")
        ctx.notify.success(TITLE, url, f"Sync to '{branch}' completed.", node)
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"{type(e).__name__}: {e}")
        ctx.notify.error(TITLE, url, f"Sync failed: {e}", node)
        raise SystemExit(1)
