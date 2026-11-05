"""Deploy the opt-in native Alloy service using shared host/guest configs."""
import json
from pathlib import Path
import re
import tempfile

from engine.descriptor import Flag, Meta

METADATA = Meta(summary="Configure native Alloy from configs/alloy/config.json.",
                args=[Flag("--dry-run", "Validate the deployment plan without changes.")])


def deployment(ctx):
    settings = Path(ctx.paths.configs) / "alloy/config.json"
    if not settings.exists():
        return None
    values = json.loads(settings.read_text())
    role = values.get("role")
    if role not in ("host", "guest"):
        raise ValueError("Alloy role must be host or guest")
    env_file = values.get("environment_file", "")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", env_file):
        raise ValueError("Alloy environment_file must be an absolute path without whitespace")
    source = Path(ctx.paths.commons) / "etc/alloy" / (
        "native-host.alloy" if role == "host" else "native.alloy")
    config = source.read_text()
    extra = Path(ctx.paths.configs) / "alloy/extra.alloy"
    if extra.is_file():
        config += "\n" + extra.read_text()
    unit = ("[Service]\nEnvironmentFile=" + env_file + "\n"
            'ExecStart=\nExecStart=/usr/bin/alloy run --storage.path=/var/lib/alloy --server.http.listen-addr=127.0.0.1:12346 /etc/alloy/config.alloy\n'
            + ("User=root\nGroup=root\n" if role == "guest" else "") +
            "SupplementaryGroups=systemd-journal" + (" docker" if role == "guest" else "") + "\n")
    return config, unit, env_file


def run(ctx, args):
    plan = deployment(ctx)
    if plan is None:
        ctx.log.info("Native Alloy is not configured for this node; skipping.")
        return
    config, unit, env_file = plan
    if args.dry_run:
        ctx.log.info("Native Alloy deployment plan validated; no changes made.")
        return
    ctx.sys.sudo(["test", "-f", env_file])
    # Validate with the actual service environment without printing credentials.
    validator = '''import os, stat, subprocess, sys
from pathlib import Path
# Environment files deliberately use plain KEY=value, with no shell evaluation.
secret = Path(sys.argv[1])
info = secret.stat()
if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
    raise RuntimeError('Alloy environment file must be root-owned with mode 0600 or stricter')
for line in secret.read_text().splitlines():
    if not line.strip() or line.lstrip().startswith('#'): continue
    key, value = line.split('=', 1)
    os.environ[key] = value
subprocess.run(['alloy', 'validate', sys.argv[2]], check=True)
'''
    with tempfile.TemporaryDirectory(prefix="apex-alloy-") as directory:
        config_path = Path(directory) / "config.alloy"
        unit_path = Path(directory) / "apex.conf"
        config_path.write_text(config)
        unit_path.write_text(unit)
        ctx.sys.sudo(["python3", "-c", validator, env_file, str(config_path)])
        ctx.sys.sudo(["install", "-d", "-m", "0755", "/etc/alloy",
                      "/etc/systemd/system/alloy.service.d"])
        ctx.sys.sudo(["install", "-m", "0644", str(config_path), "/etc/alloy/config.alloy"])
        ctx.sys.sudo(["install", "-m", "0644", str(unit_path),
                      "/etc/systemd/system/alloy.service.d/apex.conf"])
    ctx.sys.sudo(["systemctl", "unmask", "alloy.service"])
    ctx.sys.daemon_reload()
    ctx.sys.sudo(["systemctl", "enable", "alloy.service"])
    ctx.sys.sudo(["systemctl", "restart", "alloy.service"])
    ctx.sys.sudo(["systemctl", "is-active", "--quiet", "alloy.service"])
    ctx.log.success("Configured native Alloy.")
