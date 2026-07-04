# engine/lib/host.py
"""ctx.host — idempotent host mutation: backup, write_file, inject_block, install_crontab.

Ports root_template_crontab (install + rollback) and the ufw block-injection logic
(root_template.sh _root_template_inject_rules_to_ufw_file), generalized + self-healing.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import time
from typing import List, Optional, Sequence, Tuple


def apply_block(text: str, content: str, start: str, end: str,
                anchor: Optional[str] = None,
                old_pairs: Sequence[Tuple[str, str]] = ()) -> Tuple[str, bool]:
    """Pure transform: remove existing (start,end) and old_pairs blocks, insert a fresh
    block after the first anchor line (or append if anchor is None). If an anchor is
    given but not present, return (text, False) unchanged (matches bash skip behavior)."""
    lines = text.splitlines()
    if anchor is not None and not any(anchor in ln for ln in lines):
        return text, False

    pairs = [(start, end), *old_pairs]
    stripped_starts = {s: e for s, e in pairs}
    out: List[str] = []
    skip_until: Optional[str] = None
    for ln in lines:
        if skip_until is None:
            s = ln.strip()
            if s in stripped_starts:
                skip_until = stripped_starts[s]
                continue
            out.append(ln)
        else:
            if ln.strip() == skip_until:
                skip_until = None
            continue

    block = [start, *content.splitlines(), end]
    if anchor is None:
        out.extend(block)
        return "\n".join(out) + "\n", True

    result: List[str] = []
    inserted = False
    for ln in out:
        result.append(ln)
        if not inserted and anchor in ln:
            result.extend(block)
            inserted = True
    return "\n".join(result) + "\n", True


class Host:
    def __init__(self, log, sys_):
        self.log = log
        self.sys = sys_

    # --- read/write with sudo awareness ---
    def _read(self, path: str, sudo: bool = False) -> str:
        if sudo:
            cp = self.sys.run(["sudo", "cat", path], check=True, capture=True)
            return cp.stdout or ""
        with open(path) as f:
            return f.read()

    def backup(self, path: str, sudo: bool = False) -> Optional[str]:
        exists = (self.sys.ok(["sudo", "test", "-f", path]) if sudo
                  else os.path.isfile(path))
        if not exists:
            return None
        bkp = f"{path}.{int(time.time())}.bak"
        if sudo:
            self.sys.sudo(["cp", "-a", path, bkp])
        else:
            shutil.copy2(path, bkp)
        self.log.info(f"Backed up {path} -> {bkp}")
        return bkp

    def write_file(self, path: str, content: str, backup: bool = True,
                   dry_run: bool = False, sudo: bool = False) -> None:
        if dry_run:
            self.log.info(f"[DRY-RUN] would write {len(content)} bytes to {path}")
            return
        if backup:
            self.backup(path, sudo=sudo)
        tmp = tempfile.NamedTemporaryFile("w", delete=False)
        tmp.write(content)
        tmp.close()
        if sudo:
            self.sys.sudo(["mkdir", "-p", os.path.dirname(path)])
            self.sys.sudo(["cp", tmp.name, path])
            os.unlink(tmp.name)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.move(tmp.name, path)
        self.log.success(f"Wrote {path}")

    def inject_block(self, target: str, content: str, start: str, end: str,
                     anchor: Optional[str] = None,
                     old_pairs: Sequence[Tuple[str, str]] = (),
                     dry_run: bool = False, sudo: bool = True) -> bool:
        exists = (self.sys.ok(["sudo", "test", "-f", target]) if sudo
                  else os.path.isfile(target))
        if not exists:
            self.log.error(f"Target file does not exist: {target}")
            return False
        text = self._read(target, sudo=sudo)
        new_text, injected = apply_block(text, content, start, end, anchor, old_pairs)
        if not injected:
            self.log.warn(f"Anchor {anchor!r} not found in {target}; skipping.")
            return False
        if dry_run:
            self.log.info(f"[DRY-RUN] would inject block into {target}")
            return True
        self.write_file(target, new_text, backup=True, sudo=sudo)
        self.log.success(f"Block injected into {target}")
        return True

    def install_crontab(self, text: str, dry_run: bool = False) -> None:
        """Port of root_template_crontab: backup, install via `crontab -`, rollback."""
        backup = subprocess.run(["crontab", "-l"], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        prev = backup.stdout if backup.returncode == 0 else ""
        if not text.strip():
            self.log.error("Rendered crontab is empty; aborting (previous kept).")
            raise SystemExit(65)  # EX_DATAERR
        self.log.info("Rendered crontab preview:\n" + text)
        if dry_run:
            self.log.info("[DRY-RUN] would install the crontab above.")
            return
        res = subprocess.run(["crontab", "-"], input=text, text=True)
        if res.returncode != 0:
            self.log.error("crontab install failed; restoring previous.")
            subprocess.run(["crontab", "-"], input=prev, text=True)
            raise SystemExit(1)
        self.log.success("Crontab installed.")
