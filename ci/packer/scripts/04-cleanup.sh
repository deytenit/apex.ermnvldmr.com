#!/usr/bin/env bash
set -euo pipefail
/usr/local/bin/apex-provision-image sanitize
apt-get clean
cloud-init clean --logs --seed
sync
