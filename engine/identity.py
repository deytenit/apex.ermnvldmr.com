# engine/identity.py
"""Node identity — fully explicit, from node.env. The framework assumes no domain
and no clustering: a node declares its real FQDN and its public host name, and the
short `name` is just the first label. The OS hostname is only a fallback for the FQDN.

node.env keys:
  APEX_NODE_FQDN  real FQDN (traefik Host() routing).            default: OS hostname
  APEX_NODE_HOST  public host name (backup --host, obs/notify    default: APEX_NODE_FQDN
                  `instance`); often the FQDN minus a cluster
                  segment, but that's the node's choice.
  APEX_SUBNET     the node's /24 for the `direct` network.       required
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class Node:
    name: str    # short label (first segment of the host), e.g. "icarus" — logs, branch, messages
    subnet: str  # the node's direct-network /24
    fqdn: str    # real FQDN — traefik Host() routing
    host: str    # public host name — backup --host, obs `instance`, notify instance


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


def resolve(hostname: str, repo_root: str) -> Tuple[Node, List[str]]:
    """Return (Node, warnings). Exits 66 (EX_NOINPUT) if APEX_SUBNET is missing."""
    warns: List[str] = []
    env = read_env(os.path.join(repo_root, "node.env"))

    fqdn = env.get("APEX_NODE_FQDN", "") or (hostname or "").strip()
    if not fqdn:
        sys.stderr.write("identity: APEX_NODE_FQDN missing from node.env and no hostname\n")
        raise SystemExit(66)
    if not env.get("APEX_NODE_FQDN"):
        warns.append(f"APEX_NODE_FQDN not set in node.env; using hostname {fqdn!r}.")

    host = env.get("APEX_NODE_HOST", "") or fqdn      # node's public name; defaults to the FQDN
    name = host.split(".")[0]                          # short label for logs/branch/messages

    subnet = env.get("APEX_SUBNET", "")
    if not subnet:
        sys.stderr.write("identity: APEX_SUBNET missing from node.env\n")
        raise SystemExit(66)

    return Node(name=name, subnet=subnet, fqdn=fqdn, host=host), warns
