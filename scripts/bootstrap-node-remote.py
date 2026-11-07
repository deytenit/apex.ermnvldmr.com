"""Root-side Debian/PVE bootstrap, staged and invoked by bootstrap-node."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


HOME = Path("/home/adam")


def checked(command, *, data=None, capture=False, **kwargs):
    # A Git hook or remote can emit credentials. Report the operation, never its output.
    result = subprocess.run(command, input=data, text=True, stderr=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed (exit {result.returncode})")
    return result.stdout


def repository_endpoint(url):
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Invalid SSH repository URL")
    if url.startswith("ssh://"):
        parsed = urlsplit(url)
        if (parsed.password is not None or not parsed.path.strip("/") or parsed.fragment
                or parsed.query or parsed.port == 0):
            raise ValueError("Invalid SSH repository URL")
        host, port = parsed.hostname, parsed.port
    else:
        match = re.fullmatch(r"(?:[\w.-]+@)?(\[[a-fA-F0-9:]+\]|[\w.-]+):(.+)", url, re.ASCII)
        host, port = (match[1].strip("[]"), None) if match else (None, None)
    if not host or not re.fullmatch(r"[a-zA-Z0-9:][a-zA-Z0-9.:-]*", host):
        raise ValueError("An SSH repository host is required")
    return host, f"[{host}]:{port}" if port not in (None, 22) else host


def existing(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return b""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise ValueError(f"Unsafe file: {path.name}")
    return path.read_bytes()


def install(path, content, mode):
    existing(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def validate_credentials(spec, endpoint):
    key = spec["operator_key"].strip()
    if len(key.splitlines()) != 1 or not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise ValueError("Expected one operator public key")
    checked(["ssh-keygen", "-lf", "-"], data=key + "\n")
    with tempfile.TemporaryDirectory() as temp:
        private = Path(temp) / "deploy"
        install(private, (spec["deploy_key"].rstrip() + "\n").encode(), 0o600)
        public = checked(["ssh-keygen", "-y", "-P", "", "-f", str(private)], capture=True)
        if public.startswith("sk-") or public.split()[:2] == key.split()[:2]:
            raise ValueError("Use a separate unattended deploy key")
        pins = Path(temp) / "known_hosts"
        pins.write_text(spec["known_hosts"])
        matches = checked(["ssh-keygen", "-F", endpoint, "-f", str(pins)], capture=True)
        trusted = [line for line in matches.splitlines() if not line.startswith(("#", "@revoked "))]
        if not trusted:
            raise ValueError("No trusted host key for the repository")
        for pin in trusted:
            checked(["ssh-keygen", "-lf", "-"], data=pin + "\n")


def configure_ssh(home, spec, host):
    directory = home / ".ssh"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Unsafe SSH directory")
    directory.chmod(0o700)
    key = spec["operator_key"].strip()
    authorized = directory / "authorized_keys"
    content = existing(authorized)
    identity = key.split()[:2]
    present = False
    for line in content.decode(errors="replace").splitlines():
        fields = line.split()
        if fields[:2] == identity:
            present = True
        elif fields and not line.lstrip().startswith("#") and identity[1] in fields:
            raise ValueError("Existing operator key has SSH authorization restrictions; inspect it")
    if not present:
        content = content + (b"\n" if content and not content.endswith(b"\n") else b"")
        content += key.encode() + b"\n"
    # Git also evaluates includeIf for the separate gitdirs of recursive submodules.
    config = home / ".gitconfig"
    old = existing(config)
    gitdir = f"/srv/{spec['node_name']}/.git"
    prefix = directory / ("apex-" + spec["node_name"])
    include = (f'[includeIf "gitdir:{gitdir}"]\n\tpath = {prefix}.gitconf\n'
               f'[includeIf "gitdir:{gitdir}/"]\n\tpath = {prefix}.gitconf\n').encode()
    for suffix in ("key", "hosts", "sshconf", "gitconf"):
        existing(Path(f"{prefix}.{suffix}"))
    write_repository_credentials(prefix, spec, host)
    install(Path(f"{prefix}.gitconf"),
            f"[core]\n\tsshCommand = ssh -F {prefix}.sshconf\n".encode(), 0o600)
    if include not in old:
        install(config, old + b"\n" + include, 0o600)
    install(authorized, content, 0o600)


def write_repository_credentials(prefix, spec, host):
    policy = (f"Host {host}\n    IdentitiesOnly yes\n"
              f"    IdentityFile {prefix}.key\n    UserKnownHostsFile {prefix}.hosts\n"
              "    GlobalKnownHostsFile /dev/null\nHost *\n    StrictHostKeyChecking yes\n"
              "    BatchMode yes\n")
    for suffix, content in (("key", spec["deploy_key"]), ("hosts", spec["known_hosts"]),
                            ("sshconf", policy)):
        install(Path(f"{prefix}.{suffix}"), (content.rstrip() + "\n").encode(), 0o600)


def verify_and_clone(spec, host, account):
    srv = Path("/srv")
    if not srv.exists():
        srv.mkdir()
        srv.chmod(0o755)
    if srv.resolve() != srv or srv.stat().st_uid != 0 or srv.stat().st_mode & 0o022:
        raise RuntimeError("/srv must be a root-controlled directory")
    with tempfile.TemporaryDirectory(prefix=".apex-credentials-", dir=srv) as temp:
        stage = Path(temp)
        prefix = stage / "git"
        write_repository_credentials(prefix, spec, host)
        for path in stage.iterdir():
            os.chown(path, account.pw_uid, account.pw_gid)
        stage.chmod(0o711)

        def git(*args):
            return checked(["runuser", "-u", "adam", "--", "env", "GIT_TERMINAL_PROMPT=0",
                            f"GIT_SSH_COMMAND=ssh -F {prefix}.sshconf", "git", *args],
                           capture=True, cwd="/")

        try:
            git("ls-remote", "--exit-code", "--", spec["repo_url"], "HEAD")
        except RuntimeError:
            raise RuntimeError("Git access check failed; check the repository URL, deploy-key "
                               "grants and pinned host key") from None
        checkout(srv, spec["node_name"], spec["repo_url"], account, git)


def prepare_packages():
    if not Path("/etc/debian_version").is_file():
        raise RuntimeError("Debian or Proxmox VE is required")
    missing = []
    for package in ("git", "sudo", "openssh-client"):
        result = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package], text=True,
                                capture_output=True)
        if result.returncode or result.stdout.split()[1:] != ["ok", "installed"]:
            missing.append(package)
    if missing:
        checked(["apt-get", "update"])
        checked(["apt-get", "install", "-y", "--no-remove", "--no-install-recommends", *missing])


def prepare_account():
    try:
        account = pwd.getpwnam("adam")
    except KeyError:
        if os.path.lexists(HOME):
            raise RuntimeError("Existing /home/adam requires inspection") from None
        checked(["useradd", "--create-home", "--user-group", "--shell", "/bin/bash", "adam"])
        account = pwd.getpwnam("adam")
    if (not 1000 <= account.pw_uid < 65534 or account.pw_gid == 0
            or account.pw_dir != str(HOME) or HOME.resolve() != HOME
            or HOME.stat().st_uid != account.pw_uid or HOME.stat().st_mode & 0o022
            or not HOME.is_dir()):
        raise RuntimeError("Existing adam account or home requires inspection")
    groups = "sudo"
    if not Path("/usr/bin/pveversion").exists():
        checked(["groupadd", "--system", "--force", "docker"])
        groups += ",docker"
    checked(["usermod", "--shell", "/bin/bash", "--append", "--groups", groups, "adam"])
    return account


def checkout(srv, node, url, account, git):
    destination = srv / node
    if os.path.lexists(destination):
        info = destination.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid:
            raise RuntimeError("Existing checkout path requires inspection")

        def query(*args):
            return git("-C", str(destination), *args).strip()

        if query("rev-parse", "--show-toplevel") != str(destination) or query(
                "config", "--get", "remote.origin.url") != url:
            raise RuntimeError("Existing checkout does not match the requested repository")
        for line in query("submodule", "status", "--recursive").splitlines():
            if line.startswith(("-", "+", "U")):
                raise RuntimeError(f"Existing submodule does not match its recorded commit: {line}")
        return
    with tempfile.TemporaryDirectory(prefix=".apex-bootstrap-", dir=srv) as staging:
        stage = Path(staging)
        stage.chmod(0o711)
        clone = stage / "checkout"
        clone.mkdir()
        os.chown(clone, account.pw_uid, account.pw_gid)
        try:
            git("clone", "--recurse-submodules", "--", url, str(clone))
        except RuntimeError:
            raise RuntimeError("Recursive clone failed; check access to pinned submodules "
                               "and available disk space") from None
        clone.rename(destination)


def bootstrap(spec):
    node = spec["node_name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", node):
        raise ValueError("Invalid node checkout name")
    host, endpoint = repository_endpoint(spec["repo_url"])
    if not all(isinstance(spec[k], str) and spec[k].strip()
               for k in ("known_hosts", "deploy_key", "operator_key")):
        raise ValueError("Missing SSH credentials")
    if os.geteuid() != 0:
        raise RuntimeError("Root login is required")
    os.umask(0o077)
    os.environ.clear()
    os.environ.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", LC_ALL="C",
                      DEBIAN_FRONTEND="noninteractive", HOME="/root")
    # Serialize account, credential and checkout changes from competing bootstraps.
    with open("/run/apex-bootstrap.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prepare_packages()
        validate_credentials(spec, endpoint)
        account = prepare_account()
        verify_and_clone(spec, host, account)
        child = os.fork()
        if child == 0:
            try:
                os.initgroups("adam", account.pw_gid)
                os.setgid(account.pw_gid)
                os.setuid(account.pw_uid)
                os.chdir(HOME)
                configure_ssh(HOME, spec, host)
            except Exception as error:
                print(f"SSH setup failed: {error}", file=sys.stderr, flush=True)
                os._exit(1)
            os._exit(0)
        if os.waitpid(child, 0)[1]:
            raise RuntimeError("Unable to install SSH credentials")
        policy = "adam ALL=(ALL:ALL) NOPASSWD: ALL\n"
        checked(["visudo", "-cf", "-"], data=policy)
        install(Path("/etc/sudoers.d/99-apex-adam"), policy.encode(), 0o440)
        checked(["visudo", "-c"])
        checked(["runuser", "-u", "adam", "--", "sudo", "-n", "true"])


if __name__ == "__main__":
    try:
        bootstrap(json.load(sys.stdin))
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        sys.exit(f"Bootstrap failed: {error}")
