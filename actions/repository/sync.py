"""Commit node configuration and synchronize main directly with its remote."""
import os
from datetime import datetime
from pathlib import Path
import shutil
from engine.descriptor import Meta, Arg
from engine.identity import read_env
from engine.lib import tier0

METADATA = Meta(summary="Commit node changes, rebase onto origin/main, and push main.",
                args=[Arg("telegram_bot_url", "Optional notification URL", required=False)])
TITLE = "Repository Sync"


def _encrypt_sops_file(ctx, repo: str, plain: Path, enc: Path, in_type: str, out_type: str):
    if shutil.which("sops") is None:
        raise RuntimeError("sops is required to encrypt modified composition secrets.")
    rel_enc = str(enc.relative_to(repo))
    result = ctx.sys.run(
        ["sops", "-e", "--filename-override", rel_enc,
         "--input-type", in_type, "--output-type", out_type, str(plain)],
        cwd=repo, capture=True,
    )
    tmp = enc.with_name(f".{enc.name}.tmp")
    tmp.write_text(result.stdout, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, enc)


def _encrypt_secrets(ctx, repo, compositions):
    sops_config = os.path.join(repo, ".sops.yaml")
    if not os.path.isfile(sops_config):
        return
    comp_root = Path(compositions)
    if not comp_root.is_dir():
        return
    for plain in sorted(comp_root.glob("*/.env")):
        enc = plain.with_name(".enc.env")
        if enc.exists() and plain.stat().st_mtime <= enc.stat().st_mtime:
            continue
        _encrypt_sops_file(ctx, repo, plain, enc, "dotenv", "dotenv")

    # Sync @tier0/.gitignore and encrypt modified runtime files paired with .enc* in @tier0
    for proj_dir in sorted(comp_root.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith(".") or proj_dir.name.startswith("@"):
            continue
        t0 = proj_dir / "@tier0"
        if not t0.is_dir():
            continue
        tier0.sync_tier0_gitignore(t0)
        for root, dirs, files in os.walk(t0):
            dirs.sort()
            for fname in sorted(files):
                fpath = Path(root) / fname
                if not tier0.is_enc_path(fpath):
                    continue
                plain = tier0.enc_to_plain(fpath)
                if not plain.is_file():
                    continue
                if plain.stat().st_mtime <= fpath.stat().st_mtime:
                    continue
                if plain.stat().st_size > tier0.MAX_TIER0_BYTES:
                    raise RuntimeError(
                        f"{plain.relative_to(repo)} exceeds 256 KB @tier0 limit ({plain.stat().st_size} B)."
                    )
                in_t, out_t = tier0.sops_io_types(plain, fpath, decrypt=False)
                _encrypt_sops_file(ctx, repo, plain, fpath, in_t, out_t)

    # Validate @tier0 guardrails before staging
    errors = tier0.validate_tier0_compositions(comp_root)
    if errors:
        raise RuntimeError("Tier 0 guardrail violation(s):\n" + "\n".join(errors))


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
        _encrypt_secrets(ctx, repo, ctx.paths.compositions)
        git("add", "-A")
        head_before = git("rev-parse", "HEAD", capture=True).stdout.strip()
        committed = False
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            git("commit", "-m", f"[{node}] Auto-commit: {ts}")
            committed = True
        else:
            log.info("No changes to commit.")
        git("fetch", "origin", "main")
        if git("rebase", "origin/main", check=False).returncode != 0:
            git("rebase", "--abort", check=False)
            raise RuntimeError("Rebase conflict; local commits retained for manual resolution.")
        git("submodule", "update", "--init", "--recursive")
        head_after = git("rev-parse", "HEAD", capture=True).stdout.strip()
        advanced = (head_before != head_after)
        git("push", "origin", "HEAD:refs/heads/main")
        log.success("Synchronized main.")
        if url and advanced:
            if not ctx.notify.success(TITLE, url, "Sync to main completed."):
                raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"{type(e).__name__}: {e}")
        if url:
            ctx.notify.error(TITLE, url, f"Sync failed: {e}")
        raise SystemExit(1) from e
