"""Provision the golden-image operator while preserving authorized keys."""
import pwd
import grp
from pathlib import Path
from engine.descriptor import Meta, Opt

METADATA = Meta(summary="Configure the adam operator, sudo access and optional SSH public key.",
                args=[Opt("--public-key-file", "Append one OpenSSH public key from this file.")])


HOME = Path("/home/adam")


def run(ctx, args):
    system = ctx.sys
    key = None
    if args.public_key_file:
        key = Path(args.public_key_file).read_text().strip()
        if len(key.splitlines()) != 1 or not key.startswith(("ssh-", "ecdsa-", "sk-")):
            raise ValueError("Supply one OpenSSH public key.")
        system.run(["ssh-keygen", "-lf", "-"], input=key + "\n", capture=True)
    groups = ["sudo"]
    try:
        grp.getgrnam("docker")
    except KeyError:
        pass
    else:
        groups.append("docker")
    try:
        account = pwd.getpwnam("adam")
    except KeyError:
        if HOME.exists() or HOME.is_symlink():
            raise RuntimeError("Resolve existing /home/adam before creating the account.") from None
        system.sudo(["useradd", "--create-home", "--shell", "/bin/bash", "--user-group", "adam"])
        account = pwd.getpwnam("adam")
    if account.pw_uid < 1000 or Path(account.pw_dir) != HOME or HOME.resolve() != HOME:
        raise RuntimeError("Existing adam account has incompatible UID or home.")
    if Path(account.pw_dir).stat().st_uid != account.pw_uid:
        raise RuntimeError("The operator must own /home/adam.")
    system.sudo(["usermod", "--shell", "/bin/bash", "--append", "--groups", ",".join(groups), "adam"])
    if key:
        system.sudo(["runuser", "-u", "adam", "--", "sh", "-ec",
                     'umask 077; cd "$1"; mkdir -p .ssh; chmod 700 .ssh; '
                     'touch .ssh/authorized_keys; chmod 600 .ssh/authorized_keys; '
                     'IFS= read -r key; grep -Fxq -- "$key" .ssh/authorized_keys || '
                     'printf "\\n%s\\n" "$key" >> .ssh/authorized_keys', "sh", str(HOME)], input=key + "\n")
    content = "adam ALL=(ALL:ALL) NOPASSWD: ALL\n"
    system.sudo(["visudo", "-cf", "-"], input=content)
    path = Path("/etc/sudoers.d/99-apex-adam")
    if path.is_symlink():
        raise RuntimeError(f"Refusing redirected sudo policy: {path}")
    ctx.host.write_file(str(path), content, mode="0440", backup=False, sudo=True)
    system.sudo(["visudo", "-c"])
    system.sudo(["runuser", "-u", "adam", "--", "sudo", "-n", "true"])
