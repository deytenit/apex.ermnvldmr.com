#!/usr/bin/env python3
"""apex launcher — stdlib only. Loads the shared engine and optional checkout libraries."""
import os
import sys

_here = os.path.dirname(os.path.realpath(__file__))     # commons dir (or standalone repo root)
sys.path.insert(0, os.path.join(_here, "scripts"))
_vendor = os.path.join(_here, "vendor")
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)
if os.path.basename(_here) == "commons":                # node checkout: expose proprietaries/lib
    _prop = os.path.join(os.path.dirname(_here), "proprietaries")
    if os.path.isdir(_prop):
        sys.path.insert(0, _prop)

from engine.cli import main  # noqa: E402
raise SystemExit(main(sys.argv[1:]))
