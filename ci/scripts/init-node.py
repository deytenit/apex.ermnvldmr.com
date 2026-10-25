#!/usr/bin/env python3
"""Initialize a plain Debian target server via SSH enrollment.

Standalone tool for hosts on providers that do not support cloud-init.
Kept separate from the core local-only apex engine.
"""
import argparse
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(os.path.dirname(_here))
_prov_dir = os.path.join(_here, "init-node")
sys.path.insert(0, _prov_dir)

from provisioning.transport import initialize


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Initialize a plain Debian server through protected SSH enrollment.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "target",
        help="Bootstrap USER@HOST (root or an existing administrator with passwordless sudo)",
    )
    parser.add_argument(
        "--user-data",
        dest="user_data",
        help="Path to APEX cloud-config file (e.g., ci/cloud-init/user-data.template.yaml)",
    )
    parser.add_argument(
        "--port",
        dest="port",
        help="Bootstrap SSH port; otherwise uses standard SSH configuration",
    )
    parser.add_argument(
        "--identity-file",
        dest="identity_file",
        help="SSH private identity file; otherwise uses SSH configuration/agent",
    )

    args = parser.parse_args(argv)

    print(f"[init-node] Starting enrollment for {args.target}...")
    try:
        initialize(
            _repo_root,
            args.target,
            args.user_data,
            args.port,
            args.identity_file,
            progress=lambda msg: print(f"[init-node] {msg}"),
        )
        print(f"[init-node] Successfully initialized {args.target}.")
        return 0
    except Exception as e:
        sys.stderr.write(f"[init-node] Error: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
