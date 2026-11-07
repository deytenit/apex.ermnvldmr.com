"""Apply the common APT archive and journald retention policy."""
from pathlib import Path
from engine.descriptor import Meta

METADATA = Meta(summary="Configure APT archive cleanup and journald retention.")


def _configure(ctx, path, legacy, content, restart=False):
    for target in (path, legacy):
        if target.is_symlink():
            raise RuntimeError(f"Refusing redirected configuration: {target}")
    previous = path.read_text() if path.exists() else None
    backup = ctx.host.backup(str(legacy), sudo=True) if legacy.exists() else None
    if previous == content and not backup:
        return
    if previous != content:
        ctx.host.write_file(str(path), content, sudo=True)
    if backup:
        ctx.sys.sudo(["rm", "--", str(legacy)])
    if restart:
        try:
            ctx.sys.sudo(["systemctl", "restart", "systemd-journald"])
        except Exception:
            if backup:
                ctx.sys.sudo(["cp", "--preserve=all", "--", backup, str(legacy)])
            if previous is None:
                ctx.sys.sudo(["rm", "--", str(path)])
            elif previous != content:
                ctx.host.write_file(str(path), previous, backup=False, sudo=True)
            raise


def run(ctx, args):
    if not Path("/etc/debian_version").is_file():
        raise RuntimeError("Debian required.")
    apt = Path("/etc/apt/apt.conf.d")
    journal = Path("/etc/systemd/journald.conf.d")
    _configure(ctx, apt / "99-apex", apt / "01clean", 'APT::Keep-Downloaded-Packages "0";\n')
    _configure(ctx, journal / "99-apex.conf", journal / "maxuse.conf",
               "[Journal]\nSystemMaxUse=200M\n", restart=True)
