#!/usr/bin/env bash
set -euo pipefail

# Service configuration and credentials are deployed later by apex config/alloy.
alloy_version=1.21.3
alloy_sha256=dbbd705de8ab37dec0cbd6361a3fc7e754d6b2854b57ce672a83e6366199ba73
[[ "$(dpkg --print-architecture)" == amd64 ]]
alloy_deb=$(mktemp --suffix=.deb)
trap 'rm -f "$alloy_deb"' EXIT
curl --fail --location --retry 3 --output "$alloy_deb" \
  "https://github.com/grafana/alloy/releases/download/v${alloy_version}/alloy-${alloy_version}-1.amd64.deb"
printf '%s  %s\n' "$alloy_sha256" "$alloy_deb" | sha256sum --check --status
systemctl mask alloy.service
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$alloy_deb"
systemctl disable --now alloy.service
systemctl unmask alloy.service
! systemctl is-active --quiet alloy.service
! systemctl is-enabled --quiet alloy.service
