#!/usr/bin/env bash
# Run CLI, bootstrap, and host provisioning checks with the project modules on the import path.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s tests/provisioning -p 'test_*.py' -v
