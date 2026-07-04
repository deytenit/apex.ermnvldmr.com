# actions/configure/cron.py
"""Port of configure/cron — install crontab from configs/cron/crontab."""
import os
from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Install the node crontab from configs/cron/crontab.",
                args=[Flag("--dry-run", "Render + preview; do not install.")])

def run(ctx, args):
    crontab = os.path.join(ctx.paths.configs, "cron", "crontab")
    if not os.path.isfile(crontab):
        ctx.log.error(f"Crontab file not found: {crontab}"); raise SystemExit(1)
    text = ctx.tpl.render(crontab, ctx.vars())
    ctx.host.install_crontab(text, dry_run=args.dry_run)
