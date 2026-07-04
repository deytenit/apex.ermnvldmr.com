#!/usr/bin/env bash
# Run the full engine test suite (stdlib unittest — no pytest).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
