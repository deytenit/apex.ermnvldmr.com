"""Tier 0 SOPS naming, format dispatch, .gitignore management, and guardrail validation."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

MAX_TIER0_BYTES = 256 * 1024  # 256 KB strict cap

FORBIDDEN_TIER0_PATTERNS = (
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite-shm",
    "*.sqlite-wal",
    "*.sqlite3",
    "*.mv.db",
    "acme.json",
    "querylog*.json*",
    "*.log",
    "*.pid",
    "*.lock",
    "*.sock",
    "*.bak",
    "*.bak.*",
    "*.torrent",
)

GITIGNORE_HEADER = "# Managed by APEX: decrypted @tier0 secret targets\n"


def is_enc_path(path: Path | str) -> bool:
    """Return True if path follows the @tier0 SOPS naming convention (<stem>.enc.<ext> or <name>.enc)."""
    name = Path(path).name
    if name in (".enc.env", ".env"):
        return name == ".enc.env"
    if name.endswith(".enc"):
        return len(name) > 4
    return ".enc." in name and not name.startswith(".enc.")


def plain_to_enc(path: Path | str) -> Path:
    """Convert a runtime @tier0 path (<stem>.<ext> or <name>) to its SOPS sibling path."""
    p = Path(path)
    if is_enc_path(p):
        return p
    name = p.name
    dot = name.rfind(".")
    if dot > 0:
        enc_name = f"{name[:dot]}.enc{name[dot:]}"
    else:
        enc_name = f"{name}.enc"
    return p.with_name(enc_name)


def enc_to_plain(path: Path | str) -> Path:
    """Convert a SOPS @tier0 path (<stem>.enc.<ext> or <name>.enc) to its runtime path."""
    p = Path(path)
    name = p.name
    if name.endswith(".enc") and len(name) > 4:
        return p.with_name(name[:-4])
    idx = name.rfind(".enc.")
    if idx > 0:
        return p.with_name(name[:idx] + name[idx + 4:])
    raise ValueError(f"Not an encrypted @tier0 path: {path}")


def _sops_stored_as_raw(path: Path) -> bool:
    """Detect whether an existing SOPS-encrypted file was encrypted in raw/binary envelope mode."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and set(data.keys()) == {"data", "sops"}
    except Exception:
        return False


def sops_io_types(plain_path: Path | str, enc_path: Path | str | None = None, *, decrypt: bool = False) -> tuple[str, str]:
    """Determine SOPS (--input-type, --output-type) for a @tier0 file.

    Structured formats (yaml, json, dotenv, ini) are preferred.
    For .jsonc, strict JSON without comments uses ('json', 'json'); commented JSONC falls back to ('raw', 'raw').
    All other unstructured text or static key files use ('raw', 'raw').
    """
    p = Path(plain_path)
    ext = p.suffix.lower()
    if p.name in (".env", ".enc.env") or ext == ".env":
        return ("dotenv", "dotenv")
    if ext in (".yaml", ".yml"):
        return ("yaml", "yaml")
    if ext == ".json":
        if decrypt and enc_path and _sops_stored_as_raw(Path(enc_path)):
            return ("raw", "raw")
        return ("json", "json")
    if ext == ".ini":
        return ("ini", "ini")
    if ext == ".jsonc":
        if decrypt and enc_path is not None:
            return ("raw", "raw") if _sops_stored_as_raw(Path(enc_path)) else ("json", "json")
        source = p if p.is_file() else (Path(enc_path) if enc_path else p)
        try:
            json.loads(source.read_text(encoding="utf-8"))
            return ("json", "json")
        except Exception:
            return ("raw", "raw")
    return ("raw", "raw")


def sync_tier0_gitignore(tier0_dir: Path | str) -> list[str]:
    """Ensure <project>/@tier0/.gitignore ignores every decrypted file paired with a .enc* file."""
    t0 = Path(tier0_dir)
    if not t0.is_dir():
        return []
    entries: list[str] = []
    for root, dirs, files in os.walk(t0):
        dirs.sort()
        for f in sorted(files):
            full = Path(root) / f
            if is_enc_path(full):
                plain = enc_to_plain(full)
                rel = plain.relative_to(t0).as_posix()
                entries.append(f"/{rel}")
    entries = sorted(set(entries))
    gi_path = t0 / ".gitignore"
    if entries:
        content = GITIGNORE_HEADER + "".join(f"{e}\n" for e in entries)
        if not gi_path.is_file() or gi_path.read_text(encoding="utf-8") != content:
            gi_path.write_text(content, encoding="utf-8")
    elif gi_path.is_file() and gi_path.read_text(encoding="utf-8").startswith(GITIGNORE_HEADER):
        gi_path.unlink()
    return entries


def _read_gitignore_entries(tier0_dir: Path) -> set[str]:
    gi = tier0_dir / ".gitignore"
    if not gi.is_file():
        return set()
    return {
        line.strip()
        for line in gi.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def validate_tier0_compositions(compositions_dir: Path | str) -> list[str]:
    """Validate all compositions/*/@tier0 trees against size, high-churn, and secret-ignore rules."""
    comp = Path(compositions_dir)
    errors: list[str] = []
    if not comp.is_dir():
        return errors
    for proj_dir in sorted(comp.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith(".") or proj_dir.name.startswith("@"):
            continue
        t0 = proj_dir / "@tier0"
        if not t0.is_dir():
            continue
        ignored = _read_gitignore_entries(t0)
        for root, dirs, files in os.walk(t0):
            dirs.sort()
            for fname in sorted(files):
                if fname == ".gitignore" or fname == ".gitkeep":
                    continue
                fpath = Path(root) / fname
                rel = fpath.relative_to(t0).as_posix()
                proj_rel = f"compositions/{proj_dir.name}/@tier0/{rel}"
                # 1. Check forbidden high-churn / DB / cache patterns
                for pat in FORBIDDEN_TIER0_PATTERNS:
                    if fnmatch.fnmatch(fname.lower(), pat.lower()):
                        errors.append(
                            f"{proj_rel}: forbidden high-churn/state file matching '{pat}' in @tier0"
                        )
                        break
                # 2. Check strict 256 KB size cap
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                if size > MAX_TIER0_BYTES:
                    errors.append(
                        f"{proj_rel}: file size {size} B exceeds 256 KB ({MAX_TIER0_BYTES} B) @tier0 limit"
                    )
                # 3. If a .enc file exists, its plaintext counterpart MUST be listed in @tier0/.gitignore
                if is_enc_path(fpath):
                    if size == 0:
                        errors.append(f"{proj_rel}: empty ciphertext")
                    plain = enc_to_plain(fpath)
                    plain_rel = "/" + plain.relative_to(t0).as_posix()
                    if plain.exists() and plain_rel not in ignored:
                        errors.append(
                            f"{proj_rel}: decrypted counterpart '{plain_rel}' is not listed in "
                            f"compositions/{proj_dir.name}/@tier0/.gitignore"
                        )
    return errors
