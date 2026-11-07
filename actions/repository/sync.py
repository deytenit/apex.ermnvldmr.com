"""Commit node configuration and synchronize main directly with its remote."""
import os
from datetime import datetime
from engine.descriptor import Meta, Arg
from engine.identity import read_env

METADATA = Meta(summary="Commit node changes, rebase onto origin/main, and push main.",
                args=[Arg("telegram_bot_url", "Optional notification URL", required=False)])
TITLE = "Repository Sync"


def run(ctx, args):
    log, node = ctx.log, ctx.node.name
    url = args.telegram_bot_url
    repo, commons = ctx.paths.repo_root, ctx.paths.commons
    ne = read_env(os.path.join(repo, "node.env"))
    author = ne.get("APEX_GIT_AUTHOR_NAME", "apex [bot]")
    email = ne.get("APEX_GIT_AUTHOR_EMAIL", "apex@localhost")
    env = dict(os.environ, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=email)

    def git(*a, check=True, capture=False):
        return ctx.sys.run(["git", "-C", repo, *a], check=check, capture=capture, env=env)

    try:
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD", capture=True).stdout.strip()
        if branch != "main":
            raise RuntimeError("Repository sync requires main; switch branches explicitly first.")
        cp = ctx.sys.run(["git", "-C", commons, "status", "--porcelain"], capture=True)
        if (cp.stdout or "").strip():
            raise RuntimeError(f"Commons submodule at {commons} has uncommitted changes.")
        if not os.path.isdir(ctx.paths.compositions):
            raise RuntimeError(f"Directory '{ctx.paths.compositions}' does not exist.")
        expected = git("rev-parse", "HEAD:commons", capture=True).stdout.strip()
        actual = ctx.sys.run(["git", "-C", commons, "rev-parse", "HEAD"], capture=True).stdout.strip()
        if actual != expected:
            raise RuntimeError("Commons checkout differs from the committed pin; update the submodule before retrying.")
        git("add", "-A")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            git("commit", "-m", f"[{node}] Auto-commit: {ts}")
        else:
            log.info("No changes to commit.")
        git("fetch", "origin", "main")
        if git("rebase", "origin/main", check=False).returncode != 0:
            git("rebase", "--abort", check=False)
            raise RuntimeError("Rebase conflict; local commits retained for manual resolution.")
        git("submodule", "update", "--init", "--recursive")
        git("push", "origin", "HEAD:refs/heads/main")
        log.success("Synchronized main.")
        if url and not ctx.notify.success(TITLE, url, "Sync to main completed."):
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"{type(e).__name__}: {e}")
        if url:
            ctx.notify.error(TITLE, url, f"Sync failed: {e}")
        raise SystemExit(1) from e
