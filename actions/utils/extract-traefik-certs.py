# actions/utils/extract-traefik-certs.py
"""Port of utils/extract-traefik-certs — dump traefik v3 acme.json to pem via docker."""
import os
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Extract Traefik v3 acme.json certs into fullchain.pem/privkey.pem (via docker).",
                args=[Arg("source_acme", "path to acme.json"),
                      Arg("dest_dir", "destination directory"),
                      Arg("telegram_bot_url", "(optional) Telegram Bot URL", required=False)])
TITLE = "Traefik Cert Extraction"

def run(ctx, args):
    log, node, url = ctx.log, ctx.node.name, args.telegram_bot_url
    if not os.path.isfile(args.source_acme):
        log.error(f"Source file not found: {args.source_acme}"); raise SystemExit(1)
    abs_src = os.path.realpath(args.source_acme)
    src_dir, src_file = os.path.dirname(abs_src), os.path.basename(abs_src)
    ctx.sys.sudo(["mkdir", "-p", args.dest_dir])
    abs_dest = os.path.realpath(args.dest_dir)
    log.info(f"Extracting certs: {abs_src} -> {abs_dest}")
    try:
        ctx.sys.run(["docker", "run", "--rm", "-v", f"{src_dir}:/data", "-v", f"{abs_dest}:/output",
                     "ldez/traefik-certs-dumper:v2.9.3", "file", "--version", "v3",
                     "--source", f"/data/{src_file}", "--dest", "/output", "--domain-subdir=true",
                     "--crt-name=fullchain", "--crt-ext=.pem", "--key-name=privkey", "--key-ext=.pem"],
                    check=True)
        log.info("Setting secure permissions (600) on extracted keys...")
        ctx.sys.sudo(["find", abs_dest, "-type", "f", "-name", "*.pem", "-exec", "chmod", "600", "{}", "+"])
        ctx.sys.sudo(["chown", "-R", f"{os.getuid()}:{os.getgid()}", abs_dest])
        log.success("Certificates extracted.")
        ctx.notify.success(TITLE, url, f"Certificates extracted for {node}", node)
    except Exception as e:
        log.error(str(e)); ctx.notify.error(TITLE, url, f"cert extraction failed: {e}", node)
        raise SystemExit(1)
