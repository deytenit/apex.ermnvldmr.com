"""Hand SSH access over to fresh, verified adam sessions on port 2222."""
import ipaddress
import json
import os
import platform
import pwd
import re
import time
import uuid
from pathlib import Path
from engine.descriptor import Arg, Meta, Opt

METADATA = Meta(summary="Manage the protected SSH handover to port 2222.", args=[
    Arg("operation", "begin (default), finalize, commit, rollback, or status", required=False),
    Opt("--original-port", "Current SSH port when beginning from a console"),
    Opt("--client-address", "SSH client IP when beginning from a console"),
])
STATE = "/var/lib/apex-init/access/manual-state.json"
BUNDLES = Path("/usr/local/lib/apex-provisioner")


def _ancestors():
    ancestors = {}
    pid = os.getppid()
    ticks = os.sysconf("SC_CLK_TCK")
    # proc starttime includes suspend time, while journald monotonic timestamps do not.
    suspend_us = (time.clock_gettime_ns(time.CLOCK_BOOTTIME) - time.monotonic_ns()) // 1000
    while pid > 1:
        # comm can contain spaces and parentheses; the remaining fields start at state.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ancestors[pid] = int(fields[19]) * 1_000_000 // ticks - max(0, suspend_us)
        pid = int(fields[1])
    return ancestors


def _socket(ctx, ancestors):
    sockets = ctx.sys.sudo(["ss", "-Hntp", "state", "established"], capture=True).stdout
    connections = set()
    for line in sockets.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and any(int(pid) in ancestors for pid in
                                    re.findall(r'"sshd(?:-session)?",pid=(\d+),', fields[4])):
            connections.add((fields[2], fields[3]))
    if not connections:
        return None
    if len(connections) != 1:
        raise RuntimeError("Cannot identify a unique established SSH socket for this process.")
    local, peer = connections.pop()
    peer, peer_port = peer.rsplit(":", 1)
    local, local_port = local.rsplit(":", 1)
    def address(value):
        ip = ipaddress.ip_address(value.strip("[]"))
        return str(getattr(ip, "ipv4_mapped", None) or ip)
    peer, local = address(peer), address(local)
    return local, int(local_port), peer, int(peer_port)


def _connection(ctx, *, after=None):
    if pwd.getpwuid(os.getuid()).pw_name != "adam":
        raise RuntimeError("Run this step inside a fresh SSH public-key session as adam.")
    ancestors = _ancestors()
    endpoint = _socket(ctx, ancestors)
    if not endpoint:
        raise RuntimeError("Cannot identify the SSH connection for this process.")
    local, local_port, peer, peer_port = endpoint
    if after is not None and local_port != 2222:
        raise RuntimeError("Open a fresh adam SSH connection on port 2222.")
    record = rf"^Accepted publickey for adam from {re.escape(peer)} port {int(peer_port)} ssh2:"
    result = ctx.sys.sudo(["journalctl", "-b", "_UID=0", "_EXE=/usr/sbin/sshd",
                           "_EXE=/usr/lib/openssh/sshd-session", "_EXE=/usr/lib/openssh/sshd-auth",
                           "--no-pager", "-o", "json", "--grep", record], capture=True, check=False)
    if not result.returncode:
        for line in result.stdout.splitlines():
            entry = json.loads(line)
            pid = int(entry["_PID"])
            if (pid in ancestors and int(entry["__MONOTONIC_TIMESTAMP"]) >= ancestors[pid]
                    and (after is None or ancestors[pid] > after)
                    and re.match(record, entry["MESSAGE"])):
                ctx.sys.run(["sudo", "-n", "true"])
                return f"user=adam,host={peer},addr={peer},laddr={local},lport={int(local_port)}"
    raise RuntimeError("Cannot verify a fresh public-key login for this SSH process in the root sshd journal.")


def _operator_access(ctx):
    account = pwd.getpwnam("adam")
    if account.pw_uid < 1000 or account.pw_dir != "/home/adam":
        raise RuntimeError("Configure a regular adam operator account first.")
    if pwd.getpwuid(os.getuid()).pw_name == "adam":
        return _connection(ctx)
    loginuid = int(Path("/proc/self/loginuid").read_text())
    if loginuid == account.pw_uid or (loginuid == 4294967295 and os.getuid() != 0) or loginuid < 0:
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


def _state(system):
    if system.sudo(["test", "-f", STATE], check=False).returncode:
        return {}
    return json.loads(system.sudo(["cat", STATE], capture=True).stdout)


def _original_endpoint(ctx, args):
    endpoint = _socket(ctx, _ancestors())
    port = getattr(args, "original_port", None)
    address = getattr(args, "client_address", None)
    if endpoint:
        if port or address:
            raise ValueError("SSH sessions detect the peer address and port automatically.")
        return endpoint[1], endpoint[2]
    if not port or not address or not 1 <= int(port) <= 65535:
        raise ValueError("From a console, supply --original-port and --client-address for the SSH client.")
    return int(port), str(ipaddress.ip_address(address))


def run(ctx, args):
    operation = getattr(args, "operation", None) or "begin"
    if operation not in ("begin", "finalize", "commit", "rollback", "status"):
        raise ValueError("Choose begin, finalize, commit, rollback, or status.")
    if operation == "begin":
        _operator_access(ctx)
    state = _state(ctx.sys)
    phase = state.get("status", "absent")
    if operation == "status" and phase == "absent":
        print("absent")
        return
    if operation == "begin" and phase in ("transition", "final", "recovery_pending"):
        raise RuntimeError(f"Handover is {phase}; inspect status, continue from a fresh SSH login, or roll back.")
    if operation in ("finalize", "commit"):
        required = "transition" if operation == "finalize" else "final"
        if phase != required:
            raise RuntimeError(f"{operation} requires phase {required}; current phase is {phase}.")
        _connection(ctx, after=state["authenticated_after_us"])
    profile = state.get("profile") if operation != "begin" else None
    if not profile:
        if Path("/usr/bin/pveversion").is_file():
            profile = "proxmox"
        elif platform.freedesktop_os_release().get("ID") == "debian":
            profile = "debian"
        else:
            raise RuntimeError("SSH hardening supports Debian and Proxmox VE.")
    options = ["--profile", profile]
    if operation == "begin":
        port, address = _original_endpoint(ctx, args)
        options.extend(["--original-port", str(port), "--client-address", address, "--token", uuid.uuid4().hex])
        installer = Path(__file__).resolve().parents[2] / "ci/scripts/init-node/provisioning/manual_bundle.py"
        bundle = ctx.sys.sudo(["/usr/bin/python3", "-I", "-B", str(installer)], capture=True).stdout.strip()
    else:
        if not state:
            raise RuntimeError("No SSH handover has started.")
        bundle = state["bundle"]
        options.extend(["--token", state["token"]])
    if not re.fullmatch(re.escape(str(BUNDLES)) + "/[0-9a-f]{64}", bundle):
        raise RuntimeError("Invalid installed rollback bundle location.")
    command = ["/usr/bin/python3", "-I", "-c",
               "import sys; sys.path.insert(0, sys.argv.pop(1)); from provisioning.access import main; sys.exit(main())",
               bundle, "tick" if operation == "status" else operation, *options]
    ctx.sys.sudo(command)
