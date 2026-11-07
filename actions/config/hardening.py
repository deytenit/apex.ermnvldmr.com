"""Apply the image's public-key SSH policy to adam after a verified key login."""
import ipaddress
import json
import os
import pwd
import re
from pathlib import Path
from engine.descriptor import Meta

METADATA = Meta(summary="Require SSH public-key authentication for adam from an independent administrator or verified adam key session.")
CONFIG = "/etc/ssh/sshd_config"
POLICY = """
# APEX operator SSH policy
Match User adam
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitEmptyPasswords no
    X11Forwarding no
"""


def _ancestors():
    ancestors = {}
    pid = os.getppid()
    ticks = os.sysconf("SC_CLK_TCK")
    while pid > 1:
        # comm can contain spaces and parentheses; the remaining fields start at state.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ancestors[pid] = int(fields[19]) * 1_000_000 // ticks
        pid = int(fields[1])
    return ancestors


def _connection(ctx):
    if pwd.getpwuid(os.getuid()).pw_name != "adam":
        raise RuntimeError("Run config/hardening inside an SSH public-key session as adam.")
    ancestors = _ancestors()
    sockets = ctx.sys.sudo(["ss", "-Hntp", "state", "established"], capture=True).stdout
    connections = set()
    for line in sockets.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and any(int(pid) in ancestors for pid in
                                    re.findall(r'"sshd(?:-session)?",pid=(\d+),', fields[4])):
            connections.add((fields[2], fields[3]))
    if len(connections) != 1:
        raise RuntimeError("Cannot identify a unique established SSH socket for this process.")
    local, peer = connections.pop()
    peer, peer_port = peer.rsplit(":", 1)
    local, local_port = local.rsplit(":", 1)
    def address(value):
        ip = ipaddress.ip_address(value.strip("[]"))
        return str(getattr(ip, "ipv4_mapped", None) or ip)
    peer, local = address(peer), address(local)
    record = rf"^Accepted publickey for adam from {re.escape(peer)} port {int(peer_port)} ssh2:"
    result = ctx.sys.sudo(["journalctl", "-b", "_UID=0", "_EXE=/usr/sbin/sshd",
                           "_EXE=/usr/lib/openssh/sshd-session", "_EXE=/usr/lib/openssh/sshd-auth",
                           "--no-pager", "-o", "json", "--grep", record], capture=True, check=False)
    if not result.returncode:
        for line in result.stdout.splitlines():
            entry = json.loads(line)
            pid = int(entry["_PID"])
            if (pid in ancestors and int(entry["__MONOTONIC_TIMESTAMP"]) >= ancestors[pid]
                    and re.match(record, entry["MESSAGE"])):
                ctx.sys.run(["sudo", "-n", "true"])
                return f"user=adam,host={peer},addr={peer},laddr={local},lport={int(local_port)}"
    raise RuntimeError("Cannot verify public-key login for this SSH process in the root sshd journal.")


def _operator_access(ctx):
    account = pwd.getpwnam("adam")
    if account.pw_uid < 1000 or account.pw_dir != "/home/adam":
        raise RuntimeError("Configure a regular adam operator account first.")
    if pwd.getpwuid(os.getuid()).pw_name == "adam":
        return _connection(ctx)
    loginuid = int(Path("/proc/self/loginuid").read_text())
    if loginuid in (account.pw_uid, 4294967295) or loginuid < 0:
        raise RuntimeError("Use an independent administrator login or an SSH public-key session as adam.")
    if os.getuid() not in (0, loginuid):
        raise RuntimeError("Run from the independent administrator's own login.")
    ctx.sys.sudo(["true"])
    home = Path(account.pw_dir)
    authorized = home / ".ssh" / "authorized_keys"
    # Inspect as adam so private .ssh directories remain usable from another administrator.
    command = ["runuser", "-u", "adam", "--"]
    for path in (home, authorized.parent, authorized):
        metadata = ctx.sys.sudo([*command, "env", "LC_ALL=C", "stat", "-c", "%u %a %F", "--", str(path)],
                                capture=True).stdout.strip().split(" ", 2)
        owner, mode, kind = metadata
        expected = "regular file" if path == authorized else "directory"
        if int(owner) not in (0, account.pw_uid) or int(mode, 8) & 0o022 or kind != expected:
            raise RuntimeError(f"Unsafe operator SSH path: {path}")
    keys = ctx.sys.sudo([*command, "cat", "--", str(authorized)], capture=True).stdout
    for key in keys.splitlines():
        if key.startswith(("ssh-", "ecdsa-", "sk-")) and ctx.sys.run(
            ["ssh-keygen", "-lf", "-"], input=key + "\n", capture=True, check=False,
        ).returncode == 0:
            break
    else:
        raise RuntimeError("Install a valid unrestricted operator public key before applying SSH hardening.")
    ctx.sys.sudo([*command, "sudo", "-n", "true"])
    connection = "user=adam,host=localhost,addr=127.0.0.1"
    paths = _effective(ctx.sys, connection).get("authorizedkeysfile", "").split()
    if not {".ssh/authorized_keys", "%h/.ssh/authorized_keys", str(authorized)}.intersection(paths):
        raise RuntimeError("sshd does not use the operator's verified authorized_keys file.")
    return connection


def _effective(system, connection):
    result = system.sudo(["/usr/sbin/sshd", "-T", "-C", connection], capture=True)
    return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)


def run(ctx, args):
    connection = _operator_access(ctx)
    system = ctx.sys
    system.sudo(["/usr/sbin/sshd", "-t"])
    effective = _effective(system, connection)
    if effective.get("authenticationmethods") not in ("any", "publickey"):
        raise RuntimeError("Preserve the existing SSH authentication chain; reconcile it explicitly.")
    required = {"authenticationmethods": "publickey", "pubkeyauthentication": "yes",
                "passwordauthentication": "no", "kbdinteractiveauthentication": "no",
                "permitemptypasswords": "no", "x11forwarding": "no"}
    if all(effective.get(key) == value for key, value in required.items()):
        return
    if Path(CONFIG).is_symlink():
        raise RuntimeError(f"Refusing redirected SSH configuration: {CONFIG}")
    original = system.sudo(["cat", CONFIG], capture=True).stdout
    if "# APEX operator SSH policy" in original:
        raise RuntimeError("The managed SSH policy is ineffective; reconcile earlier directives.")
    ctx.host.write_file(CONFIG, original + "\n" + POLICY, sudo=True)
    try:
        system.sudo(["/usr/sbin/sshd", "-t"])
        effective = _effective(system, connection)
        if any(effective.get(key) != value for key, value in required.items()):
            raise RuntimeError("The effective SSH policy differs; inspect existing Match blocks.")
        system.reload("ssh")
    except BaseException:
        ctx.host.write_file(CONFIG, original, backup=False, sudo=True)
        system.sudo(["/usr/sbin/sshd", "-t"])
        system.reload("ssh")
        raise
