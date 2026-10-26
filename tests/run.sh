#!/usr/bin/env bash
# Run the core and standalone provisioning test suites (stdlib unittest — no pytest).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=ci/scripts/init-node${PYTHONPATH:+:$PYTHONPATH} python3 -m unittest discover -s ci/scripts/init-node/tests -p 'test_*.py' -v
