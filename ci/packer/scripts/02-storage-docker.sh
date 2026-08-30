#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== [1/5] Configuring Debian Repositories for OpenZFS DKMS ==="
# Enable contrib and non-free-firmware for zfs-dkms
sed -i 's/Components: main/Components: main contrib non-free-firmware/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
sed -i 's/main$/main contrib non-free-firmware/g' /etc/apt/sources.list 2>/dev/null || true

apt-get update
apt-get install -y --no-install-recommends linux-headers-amd64 zfsutils-linux zfs-dkms

# Pre-compile DKMS modules during bake time so first boot has zero compilation delay
systemctl enable zfs-import-cache zfs-import.target zfs.target || true

echo "=== [2/5] Installing Docker CE from Official Debian Repository ==="
for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
  apt-get remove -y "$pkg" 2>/dev/null || true
done

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

mkdir -p /srv/docker
cat << 'EOF_DOCKER' > /etc/docker/daemon.json
{
  "data-root": "/srv/docker",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF_DOCKER
systemctl enable docker.service containerd.service

echo "=== [3/5] Installing and Configuring UFW + ufw-docker ==="
apt-get install -y --no-install-recommends ufw iptables

curl -fsSL https://github.com/chaifeng/ufw-docker/raw/master/ufw-docker -o /usr/local/bin/ufw-docker
chmod +x /usr/local/bin/ufw-docker

# Pre-configure firewall baseline (keep port 22 open during build for Packer SSH session)
ufw default deny incoming
ufw default allow outgoing
ufw default allow routed
ufw allow 22/tcp comment 'SSH default for builder'
ufw allow 2222/tcp comment 'SSH for Apex'
ufw --force enable

# Set up ufw-docker routing in after.rules (now that UFW is active)
/usr/local/bin/ufw-docker install || true

# Symlink into adam ~/.local/bin for engine compatibility
mkdir -p /etc/skel/.local/bin
ln -sf /usr/local/bin/ufw-docker /etc/skel/.local/bin/ufw-docker

echo "=== [4/5] Hardening SSH Configuration ==="
mkdir -p /etc/ssh/sshd_config.d
cat << 'EOF_SSH' > /etc/ssh/sshd_config.d/99-apex.conf
Port 2222
PasswordAuthentication no
PermitEmptyPasswords no
KbdInteractiveAuthentication no
X11Forwarding no
AllowUsers adam
EOF_SSH

echo "=== [5/5] Installing CrowdSec ==="
# PackageCloud does not publish a dedicated 'trixie' suite yet; bookworm suite works seamlessly on Debian 13
export os=debian
export dist=bookworm
if curl -fsSL https://packagecloud.io/install/repositories/crowdsec/crowdsec/script.deb.sh | bash; then
  apt-get update
  apt-get install -y --no-install-recommends crowdsec crowdsec-firewall-bouncer-iptables
else
  echo "PackageCloud script failed, falling back to Debian native repository..."
  rm -f /etc/apt/sources.list.d/crowdsec*.list
  apt-get update
  apt-get install -y --no-install-recommends crowdsec crowdsec-firewall-bouncer-iptables || \
  apt-get install -y --no-install-recommends crowdsec
fi

echo "=== Docker, ZFS & Security Stack Complete ==="
