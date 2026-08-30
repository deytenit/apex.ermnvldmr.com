#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== [1/6] Configuring Base Packages ==="
apt-get update
apt-get install -y --no-install-recommends \
  curl wget tcpdump socat rsync bind9-dnsutils iproute2 net-tools traceroute ca-certificates \
  jq sed gawk tar gzip bzip2 xz-utils unzip htop iotop strace lsof sysstat \
  parted fdisk pciutils usbutils smartmontools tmux vim less tree bash-completion \
  git python3 python3-minimal sudo rsyslog cron qemu-guest-agent

echo "=== [2/6] Configuring Sudo & Passwordless Escalation ==="
cat << 'EOF_SUDO' > /etc/sudoers.d/99-apex
%sudo ALL=(ALL:ALL) NOPASSWD: ALL
EOF_SUDO
chmod 0440 /etc/sudoers.d/99-apex
visudo -cf /etc/sudoers.d/99-apex

echo "=== [3/6] Configuring VirtIO Kernel Modules ==="
cat << 'EOF_VIRTIO' > /etc/modules-load.d/virtio.conf
virtio
virtio_net
virtio_blk
virtio_pci
virtio_scsi
virtiofs
EOF_VIRTIO

echo "=== [4/6] Configuring GRUB Serial Console & Predictable Interfaces ==="
sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8 net.ifnames=0 biosdevname=0"/' /etc/default/grub
sed -i 's/^#GRUB_TERMINAL=.*/GRUB_TERMINAL="console serial"/' /etc/default/grub
if ! grep -q "GRUB_SERIAL_COMMAND" /etc/default/grub; then
  echo 'GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"' >> /etc/default/grub
fi
update-grub

mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d
cat << 'EOF_GETTY' > /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,38400,9600 %I $TERM
EOF_GETTY
systemctl enable serial-getty@ttyS0.service || true
systemctl enable qemu-guest-agent 2>/dev/null || true

echo "=== [5/6] Configuring Cloud-Init Datasources & Service Enablement ==="
mkdir -p /etc/cloud /etc/cloud/cloud.cfg.d
cat << 'EOF_DS' > /etc/cloud/ds-identify.cfg
policy: enabled
EOF_DS

cat << 'EOF_CLOUD_CFG' > /etc/cloud/cloud.cfg.d/99-cloud-init.cfg
datasource_list: [ NoCloud, ConfigDrive, OpenStack, Ec2, None ]
disable_vm_template: false
EOF_CLOUD_CFG

# Cloud-init services are static and enabled by generator via ds-identify
systemctl enable cloud-init-local.service cloud-init.service cloud-config.service cloud-final.service 2>/dev/null || true

echo "=== [6/6] Configuring Predictable Network DHCP ==="
mkdir -p /etc/systemd/network
cat << 'EOF_NET' > /etc/systemd/network/10-eth.network
[Match]
Name=eth* en*

[Network]
DHCP=yes
EOF_NET
systemctl enable systemd-networkd systemd-resolved || true

mkdir -p /etc/network/interfaces.d
cat << 'EOF_IF' > /etc/network/interfaces.d/eth0
auto eth0
iface eth0 inet dhcp
EOF_IF

echo "=== Base System Configuration Complete ==="
