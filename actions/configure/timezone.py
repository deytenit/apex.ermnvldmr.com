"""Apply the optional node timezone before installing scheduled jobs."""
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Apply configs/timezone and refresh cron when the timezone changes.",
                args=[Flag("--dry-run", "Validate and preview without changing the host.")])


def run(ctx, args):
    source = Path(ctx.paths.configs) / "timezone"
    if not source.is_file():
        ctx.log.info("No configs/timezone declaration; preserving host timezone.")
        return
    zone = source.read_text().strip()
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        ctx.log.error("configs/timezone must name an installed IANA timezone.")
        raise SystemExit(1)
    current = ctx.sys.run(["timedatectl", "show", "--property=Timezone", "--value"], capture=True).stdout.strip()
    if current == zone:
        ctx.log.info(f"Timezone already set to {zone}.")
        return
    if args.dry_run:
        ctx.log.info(f"[DRY-RUN] would change timezone from {current} to {zone} and refresh active cron.")
        return
    ctx.sys.sudo(["timedatectl", "set-timezone", zone])
    if ctx.sys.service_is_active("cron") and not ctx.sys.restart("cron"):
        ctx.log.error("Timezone changed, but cron did not become active after restart.")
        raise SystemExit(1)
    ctx.log.success(f"Timezone set to {zone}.")
