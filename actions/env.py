"""Export synthesized APEX values or bridge a composition's environment for Compose."""
import os
import shlex

from engine.descriptor import Flag, Meta, Opt

METADATA = Meta(
    summary="Export or sync synthesized APEX_* variables for shell or docker compose.",
    args=[
        Flag("--export", "Format as shell export statements (default)."),
        Flag("--dotenv", "Print quoted generated APEX_* dotenv values only."),
        Opt("--write", "Write a standalone generated dotenv file (0600).", default=None),
        Flag("--sync", "Preserve user .env text and refresh the core Compose managed block."),
    ],
)

_START = "# BEGIN APEX MANAGED ENV"
_END = "# END APEX MANAGED ENV"


def _dotenv_value(value):
    """Quote a literal using Compose dotenv escapes, including interpolation dollars."""
    value = str(value)
    for old, new in (("\\", "\\\\"), ('"', '\\"'), ("$", "$$"),
                     ("\r", "\\r"), ("\n", "\\n"), ("\t", "\\t")):
        value = value.replace(old, new)
    return f'"{value}"'


def _read_optional(path):
    try:
        with open(path, encoding="utf-8", newline="") as source:
            return source.read()
    except FileNotFoundError:
        return ""


def _sync_body(existing, public, generated):
    if any(marker in public or marker in generated for marker in (_START, _END)):
        raise ValueError("Managed environment marker collides with source content.")
    lines = existing.splitlines(keepends=True)
    markers = [(index, line.rstrip("\r\n")) for index, line in enumerate(lines)
               if _START in line or _END in line]
    if markers:
        if (len(markers) != 2 or markers[0][1] != _START or markers[1][1] != _END):
            raise ValueError("Malformed or duplicate managed environment markers; .env unchanged.")
        start, end = markers[0][0], markers[1][0]
        existing = "".join(lines[:start] + lines[end + 1:])
    # Keep user bytes in order, but place managed values last to preserve precedence.
    separator = "" if not existing or existing.endswith("\n") else "\n"
    public_separator = "" if not public or public.endswith("\n") else "\n"
    return (existing + separator + _START + "\n" + public + public_separator
            + generated + _END + "\n")


def run(ctx, args):
    modes = (args.export, args.dotenv, args.write is not None, args.sync)
    if sum(bool(mode) for mode in modes) > 1:
        ctx.log.error("Choose only one of --export, --dotenv, --write, or --sync.")
        raise SystemExit(64)
    variables = {k: v for k, v in ctx.vars().items() if k.startswith("APEX_")}
    generated = "".join(f"{key}={_dotenv_value(value)}\n"
                        for key, value in sorted(variables.items()))
    target = args.write
    if args.sync:
        core = os.path.join(ctx.paths.compositions, "apex")
        target = os.path.join(core, ".env")
        try:
            generated = _sync_body(_read_optional(target),
                                   _read_optional(os.path.join(core, "apex.env")), generated)
        except ValueError as error:
            ctx.log.error(str(error))
            raise SystemExit(65) from error
    if target is not None:
        ctx.host.write_file(target, generated, mode="0600")
        ctx.log.success(f"Wrote {len(variables)} synthesized variables to {target}.")
    elif args.dotenv:
        print(generated, end="")
    else:
        for key, value in sorted(variables.items()):
            print(f"export {key}={shlex.quote(str(value))}")
    return 0
