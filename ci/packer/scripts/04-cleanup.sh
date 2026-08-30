#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== [1/4] Purging APT Caches and Temp Files ==="
apt-get autoremove -y --purge
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

echo "=== [2/4] Resetting Machine Identity for Clones ==="
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id

mkdir -p /etc/systemd/system/ssh.service.d
cat << 'EOF_SSH_HOST' > /etc/systemd/system/ssh.service.d/10-generate-host-keys.conf
[Service]
ExecStartPre=-/usr/bin/ssh-keygen -A
EOF_SSH_HOST
ssh-keygen -A

echo "=== [3/4] Resetting Cloud-Init State & Bash History ==="
ufw delete allow 22/tcp 2>/dev/null || true
cloud-init clean --logs --seed || true
rm -rf /var/lib/cloud/*
rm -f /root/.bash_history /home/*/.bash_history

echo "=== [4/4] Syncing Filesystems ==="
sync

echo "=== Image Sanitization Complete ==="
