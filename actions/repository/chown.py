"""Give adam ownership of the active node's tracked checkout and Git metadata."""
import inspect
import json
from pathlib import Path
import re
from engine.descriptor import Meta

METADATA = Meta(summary="Set ownership of tracked files and Git metadata under /srv to adam.")


def _repair(root, tracked, mounts, uid, gid):
    import os
    import stat

    directories = {""}
    for item in tracked:
        directory = os.path.dirname(item)
        while directory and directory not in directories:
            directories.add(directory)
            directory = os.path.dirname(directory)
    metadata = os.path.join(root, ".git")

    def fail(error):
        raise error

    for parent, dirs, files, fd in os.fwalk(root, onerror=fail, follow_symlinks=False):
        os.fchown(fd, uid, gid)
        for name in dirs.copy() + files:
            path = os.path.join(parent, name)
            relative = os.path.relpath(path, root)
            if path in mounts or not (relative in tracked or relative in directories or
                                      path == metadata or path.startswith(metadata + "/")):
                if name in dirs:
                    dirs.remove(name)
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                continue
            if (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)) and info.st_nlink == 1:
                os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)


def run(ctx, args):
    root = Path(ctx.paths.repo_root)
    if (root.parent != Path("/srv") or root != root.resolve(strict=True) or
            not (root / "node.env").is_file() or (root / ".git").is_symlink() or
            not (root / ".git").is_dir()):
        raise RuntimeError("Expected the current node checkout directly under /srv.")
    git = ["git", "-c", f"safe.directory={root}", "-c", f"safe.directory={root}/commons",
               "-C", str(root)]
    entries = ctx.sys.run([*git, "ls-files", "--cached", "--recurse-submodules", "-z"],
                          capture=True).stdout
    gitfiles = ctx.sys.run([*git, "submodule", "foreach", "--quiet", "--recursive",
                            'printf "%s\\0" "$displaypath/.git"'], capture=True).stdout
    mounts = [re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4])
              for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    script = (inspect.getsource(_repair) + "\nimport json, pwd, sys\n"
              "root, entries, mounts = json.load(sys.stdin)\n"
              "adam = pwd.getpwnam('adam')\n"
              "_repair(root, set(entries), set(mounts), adam.pw_uid, adam.pw_gid)")
    ctx.sys.sudo(["python3", "-I", "-c", script],
                 input=json.dumps([str(root), (entries + gitfiles).split("\0"), mounts]))
