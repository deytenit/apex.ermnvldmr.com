# engine/identity.py
"""Node identity from host FQDN + node.env (replaces bash root_require_node).

Decision B: the FQDN <node>.a<x>.apex.ermnvldmr.com keeps the cluster; we parse
node+cluster from it. node.env owns APEX_SUBNET and mirrors APEX_CLUSTER; hostname
wins on drift. Falls back to the repo directory name when the FQDN is not yet
apex-shaped (mid-cutover).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple

_FQDN = re.compile(r"^(?P<node>[^.]+)\.(?P<cluster>a\d+)\.apex\.ermnvldmr\.com$")
_DIRNAME = re.compile(r"^(?P<node>[^.]+)\.apex\.ermnvldmr\.com")


@dataclass
class Node:
    name: str
    cluster: str
    subnet: str
    fqdn: str


def read_env(path: str) -> Dict[str, str]:
    """Parse a KEY=value file (shell-sourceable). Ignores comments/blank lines."""
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def resolve(fqdn: str, repo_root: str) -> Tuple[Node, List[str]]:
    """Return (Node, warnings). Exits 66 (EX_NOINPUT) if APEX_SUBNET is missing."""
    warns: List[str] = []
    env = read_env(os.path.join(repo_root, "node.env"))

    m = _FQDN.match(fqdn or "")
    if m:
        name, cluster = m.group("node"), m.group("cluster")
        env_cluster = env.get("APEX_CLUSTER")
        if env_cluster and env_cluster != cluster:
            warns.append(
                f"cluster drift: hostname says {cluster!r}, node.env says "
                f"{env_cluster!r}; hostname wins."
            )
    else:
        # Fallback: derive node from repo dir name; cluster from node.env.
        warns.append(
            f"FQDN {fqdn!r} is not apex-shaped; falling back to repo dir + node.env."
        )
        base = os.path.basename(os.path.realpath(repo_root))
        dm = _DIRNAME.match(base)
        name = dm.group("node") if dm else base
        cluster = env.get("APEX_CLUSTER", "")
        if not cluster:
            sys.stderr.write("identity: no cluster in FQDN or node.env\n")
            raise SystemExit(66)

    subnet = env.get("APEX_SUBNET", "")
    if not subnet:
        sys.stderr.write("identity: APEX_SUBNET missing from node.env\n")
        raise SystemExit(66)

    return Node(name=name, cluster=cluster, subnet=subnet, fqdn=fqdn), warns
