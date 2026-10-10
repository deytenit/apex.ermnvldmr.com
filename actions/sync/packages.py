# actions/sync/packages.py
"""Check pending APT and running-image updates without pulling images."""
import json
import os
import re
import shutil
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Check pending apt updates + docker image updates; Telegram notify.",
                args=[Arg("telegram_bot_url", "Telegram Bot URL for notifications")])
TITLE = "Node Security Updates"

MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}


def _output(s, command):
    result = s.run(command, check=False, capture=True)
    if result.returncode:
        # Registry errors can contain authentication details; don't relay stderr.
        raise ValueError("inspection command failed")
    return result.stdout or ""


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("missing or unsupported image digest")
    return value


def _platform(os_name, architecture, variant=""):
    if not isinstance(os_name, str) or not os_name or not isinstance(architecture, str) or not architecture:
        raise ValueError("missing image platform")
    variant = variant or ""
    if architecture == "arm64" and variant == "v8":
        variant = ""
    return os_name, architecture, variant


def _repository(reference):
    name = reference.split("@", 1)[0]
    # Only a colon in the final path component is a tag separator.
    return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name


def _remote_digests(s, reference, local, cache, running_manifest=None):
    def inspect(ref, raw=False, field="Manifest"):
        command = ["docker", "buildx", "imagetools", "inspect"]
        command += ["--raw"] if raw else ["--format", "{{json ." + field + "}}"]
        key = tuple(command + [ref])
        if key not in cache:
            value = json.loads(_output(s, ["timeout", "--signal=TERM", "--kill-after=5s",
                                          "30s", *key]))
            if not isinstance(value, dict):
                raise ValueError("invalid registry JSON object")
            cache[key] = value
        return cache[key]

    platform = _platform(local.get("Os"), local.get("Architecture"), local.get("Variant"))
    descriptor = inspect(reference)
    media_type = descriptor.get("mediaType")
    selected = _digest(descriptor.get("digest"))
    if media_type in INDEX_TYPES:
        manifests = descriptor.get("manifests")
        if not isinstance(manifests, list):
            raise ValueError("invalid image index")
        matches = []
        for item in manifests:
            p = item.get("platform", {})
            if _platform(p.get("os"), p.get("architecture"), p.get("variant")) == platform:
                if item.get("mediaType") not in MANIFEST_TYPES:
                    raise ValueError("unsupported nested image index")
                matches.append(_digest(item.get("digest")))
        if len(matches) != 1:
            raise ValueError("missing or ambiguous matching image platform")
        selected = matches[0]
    elif media_type not in MANIFEST_TYPES:
        raise ValueError("unsupported registry manifest")

    immutable = _repository(reference) + "@" + selected
    manifest = inspect(immutable, raw=True)
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") not in MANIFEST_TYPES:
        raise ValueError("invalid platform manifest")
    remote_id = _digest(manifest.get("config", {}).get("digest"))
    if media_type in MANIFEST_TYPES and selected != running_manifest and remote_id != local["Id"]:
        config = inspect(immutable, field="Image")
        if _platform(config.get("os"), config.get("architecture"), config.get("variant")) != platform:
            raise ValueError("registry image platform changed")
    return selected, remote_id


def _compose_builds(ctx):
    builds = {}
    env = dict(os.environ)
    env.update({k: v for k, v in ctx.vars().items() if k.startswith("APEX_")})
    for name in sorted(os.listdir(ctx.paths.compositions)):
        directory = os.path.join(ctx.paths.compositions, name)
        if name.startswith((".", "@")) or not os.path.isfile(os.path.join(directory, "docker-compose.yml")):
            continue
        command = ["docker", "compose"]
        for filename in (".env", "apex.env"):
            if os.path.isfile(os.path.join(directory, filename)):
                command += ["--env-file", filename]
        # Resolved Compose includes secrets: capture privately and never log it.
        result = ctx.sys.run(["timeout", "--signal=TERM", "--kill-after=5s", "30s",
                              *command, "config", "--format", "json"],
                             cwd=directory, env=env, check=False, capture=True)
        if result.returncode:
            raise ValueError("Compose inspection failed")
        config = json.loads(result.stdout)
        project, services = config["name"], config["services"]
        if not isinstance(project, str) or not project or not isinstance(services, dict):
            raise ValueError("invalid Compose model")
        for service, settings in services.items():
            if settings.get("build") is not None:
                reference = settings.get("image")
                if reference is not None and not isinstance(reference, str):
                    raise ValueError("invalid Compose image")
                builds[(project, service)] = reference
    return builds


def _images(s, log, ctx=None):
    if not shutil.which("docker"):
        return "Images: unchecked (Docker unavailable)"
    if not shutil.which("timeout"):
        return "Images: unchecked (coreutils timeout unavailable)"
    try:
        ids = _output(s, ["docker", "ps", "--quiet", "--no-trunc"]).split()
        if not ids:
            return "Images: no running containers"
        output = _output(s, ["docker", "container", "inspect", "--format",
                            '{"Image":{{json .Image}},"Reference":{{json .Config.Image}},'
                            '"ImageManifestDescriptor":{{json (index . "ImageManifestDescriptor")}},'
                            '"Labels":{{json .Config.Labels}}}', *ids])
        containers = [json.loads(line) for line in output.splitlines()]
        if len(containers) != len(ids):
            raise ValueError("incomplete running-container inspection")
        if any(not isinstance(c["Reference"], str) for c in containers):
            raise ValueError("invalid running-image reference")
    except (ValueError, KeyError, TypeError, OSError) as error:
        log.warn(f"Images unchecked: {type(error).__name__}")
        return "Images: unchecked (container inspection failed)"

    try:
        builds = _compose_builds(ctx) if ctx is not None else {}
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        log.warn("Images unchecked: Compose build provenance could not be resolved")
        return "Images: unchecked (Compose inspection failed)"

    buildx = s.run(["docker", "buildx", "version"], check=False, capture=True).returncode == 0
    counts = dict.fromkeys(("updates", "current", "pinned", "local/untracked", "unchecked"), 0)
    cache, images, seen = {}, {}, set()
    for container in containers:
        reference = container["Reference"]
        reason = ""
        try:
            image_id = _digest(container["Image"])
            descriptor = container.get("ImageManifestDescriptor")
            labels = container.get("Labels") or {}
            build_key = (labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service"))
            declared_build = build_key in builds and builds[build_key] in (None, reference)
            key = (reference, image_id, json.dumps(descriptor, sort_keys=True), declared_build)
            if key in seen:
                continue
            seen.add(key)
            if not isinstance(reference, str) or not reference or reference.startswith("-"):
                raise ValueError("invalid image reference")
            if "@" in reference or re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", reference):
                status = "pinned"
            elif declared_build:
                status = "local/untracked"
            else:
                if image_id not in images:
                    data = json.loads(_output(s, ["docker", "image", "inspect", image_id]))
                    if not isinstance(data, list) or len(data) != 1 or data[0].get("Id") != image_id:
                        raise ValueError("invalid running-image inspection")
                    images[image_id] = data[0]
                local = images[image_id]
                repo_digests = local["RepoDigests"]
                if repo_digests is None or repo_digests == []:
                    status = "local/untracked"
                elif not isinstance(repo_digests, list):
                    raise ValueError("invalid repository digests")
                elif not buildx:
                    raise ValueError("Buildx unavailable")
                else:
                    running_manifest = None
                    if descriptor is not None:
                        if descriptor.get("mediaType") not in MANIFEST_TYPES:
                            raise ValueError("invalid running platform manifest")
                        running_manifest = _digest(descriptor.get("digest"))
                        platform = descriptor.get("platform", {})
                        os_name, architecture, variant = _platform(
                            platform.get("os"), platform.get("architecture"), platform.get("variant"))
                        local = dict(local, Os=os_name, Architecture=architecture, Variant=variant)
                    elif local.get("Descriptor") is not None:
                        raise ValueError("containerd running platform manifest unavailable")
                    remote_manifest, remote_config = _remote_digests(
                        s, reference, local, cache, running_manifest)
                    # Classic Docker IDs are configs; containerd IDs may be indices.
                    current = (remote_manifest == running_manifest if running_manifest
                               else remote_config == image_id)
                    status = "current" if current else "updates"
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            status, reason = "unchecked", str(error) if isinstance(error, ValueError) else type(error).__name__
        counts[status] += 1
        log.info(f"Image {reference}: {status}" + (f" ({reason})" if reason else ""))
    return "Images: " + ", ".join(f"{count} {status}" for status, count in counts.items())


def run(ctx, args):
    log, node, url, s = ctx.log, ctx.node.name, args.telegram_bot_url, ctx.sys
    try:
        _check(ctx, args, log, node, url, s)
    except SystemExit:
        raise
    except Exception as e:   # bash ERR trap parity: unhandled failures page via Telegram
        log.error(f"{type(e).__name__}: {e}")
        ctx.notify.error(TITLE, url, f"Critical error: {e}")
        raise SystemExit(1)

def _check(ctx, args, log, node, url, s):
    if not os.path.isfile("/etc/debian_version"):
        log.error("Requires a Debian-based system.")
        ctx.notify.error(TITLE, url, "Requires a Debian-based system.")
        raise SystemExit(1)

    log.info("Updating package lists...")
    if s.ok(["sudo", "apt-get", "update"]):
        cp = s.run(["bash", "-c",
                    "sudo apt-get --just-print upgrade 2>/dev/null | grep '^Inst' | cut -d' ' -f2 | sort"],
                   check=False, capture=True)
        upd = [l for l in (cp.stdout or "").splitlines() if l.strip()]
        packages = (f"*Packages ({len(upd)}):* " + ", ".join(f"`{u}`" for u in upd)) if upd else "*Packages:* _none_"
        apt_summary = f"APT: {len(upd)} pending"
    else:
        packages = "*Packages:* _Failed to check_"
        apt_summary = "APT: failed to check"

    images = _images(s, log, ctx)
    log.info(packages)

    # A failed send exits 1 (bash: telegram_info's return 1 tripped set -e).
    if not ctx.notify.info(TITLE, url, f"{apt_summary}\n{images}\n\n{packages}"):
        raise SystemExit(1)
    log.success("Security update check completed.")
