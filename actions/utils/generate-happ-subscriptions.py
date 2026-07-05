# actions/utils/generate-happ-subscriptions.py
"""Port of utils/generate-happ-subscriptions — build + run the happ generator image."""
import os
import shutil
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Generate .psub files from a JSON config via the happ generator (docker).",
                args=[Arg("input_json", "input JSON config"), Arg("output_dir", "output directory")])
IMAGE = "happ-subscription-generator"

def run(ctx, args):
    log = ctx.log
    src_dir = os.path.join(ctx.paths.commons, "source", "happ-subscription-generator")
    inp, out = os.path.realpath(args.input_json), os.path.realpath(args.output_dir)
    # GNU realpath (bash) fails unless all but the last component exist — without this
    # guard a mistyped path reaches `docker run -v`, which creates it root-owned.
    for label, p in (("input_json", inp), ("output_dir", out)):
        parent = os.path.dirname(p)
        if not os.path.isdir(parent):
            log.error(f"Cannot resolve {label}: no such directory: {parent}")
            raise SystemExit(1)
    if not shutil.which("docker"):
        log.error("docker is not installed or not in PATH.")
        raise SystemExit(1)
    log.info(f"Building {IMAGE}...")
    ctx.sys.run(["docker", "build", "-t", IMAGE, src_dir], check=True)
    log.info(f"Running generator: {inp} -> {out}")
    ctx.sys.run(["docker", "run", "--rm", "-v", f"{inp}:/app/input.json:ro",
                 "-v", f"{out}:/app/output:rw", IMAGE, "/app/input.json", "/app/output"], check=True)
    log.success("Subscription generation completed.")
