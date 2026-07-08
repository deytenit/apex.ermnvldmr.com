# actions/sync/repository.py
"""Port of sync/repository — capture-up commit + rebase onto origin/main + force-push."""
import os
from datetime import datetime
from engine.descriptor import Meta, Arg
from engine.identity import read_env

METADATA = Meta(summary="Capture-up: commit node changes, rebase onto origin/main, force-push sync/<node>.",
                args=[Arg("telegram_bot_url", "Telegram Bot URL for notifications")])
TITLE = "Annual Remote Sync"

def run(ctx, args):
    log, node, url = ctx.log, ctx.node.name, args.telegram_bot_url
    repo, commons = ctx.paths.repo_root, ctx.paths.commons
    ne = read_env(os.path.join(repo, "node.env"))       # per-node bot identity (generic defaults)
    author = ne.get("APEX_GIT_AUTHOR_NAME", "apex [bot]")
    email = ne.get("APEX_GIT_AUTHOR_EMAIL", "apex@localhost")
    env = dict(os.environ,
               GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=email)

    def git(*a, check=True):
        return ctx.sys.run(["git", "-C", repo, *a], check=check, env=env)

    try:
        cp = ctx.sys.run(["git", "-C", commons, "status", "--porcelain"], capture=True)
        if (cp.stdout or "").strip():
            log.error(f"Commons submodule at {commons} has uncommitted changes. Aborting.")
            raise SystemExit(1)
        if not os.path.isdir(ctx.paths.compositions):
            # Without this, `git add -A` would stage a deleted node tree and an
            # unattended cron run would commit + force-push the deletion.
            log.error(f"Directory '{ctx.paths.compositions}' does not exist. Cannot sync.")
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
        git("fetch", "origin", "main")
        if git("rebase", "origin/main", check=False).returncode != 0:
            log.error("Rebase conflict — aborting rebase; manual intervention required.")
            git("rebase", "--abort", check=False)
            ctx.notify.error(TITLE, url, "Rebase conflict. Manual intervention required.")
            raise SystemExit(1)
        git("submodule", "update", "--init", "--recursive")
        git("push", "--force-with-lease", "origin", branch)
        log.success(f"Pushed {branch}.")
        if not ctx.notify.success(TITLE, url, f"Sync to '{branch}' completed."):
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"{type(e).__name__}: {e}")
        ctx.notify.error(TITLE, url, f"Sync failed: {e}")
        raise SystemExit(1)
