#!/usr/bin/env bash
set -euo pipefail

echo "=== Baking /usr/local/bin/apex-bootstrap Runner ==="

cat << 'BOOTSTRAP_EOF' > /usr/local/bin/apex-bootstrap
#!/usr/bin/env bash
set -euo pipefail

MARKER="/var/lib/apex-bootstrapped"
if [ -f "$MARKER" ]; then
  echo "[apex-bootstrap] Node has already been bootstrapped ($MARKER exists). Exiting."
  exit 0
fi

echo "============================================================"
echo "          Starting apex.ermnvldmr.com Node Bootstrap         "
echo "============================================================"

CONFIG_ENV="/etc/apex/bootstrap.env"
if [ ! -f "$CONFIG_ENV" ]; then
  echo "[apex-bootstrap] ERROR: $CONFIG_ENV not found!" >&2
  echo "[apex-bootstrap] Provide APEX_REPO_NAME, APEX_REPO_URL via cloud-init write_files." >&2
  exit 1
fi

source "$CONFIG_ENV"

if [ -z "${APEX_REPO_NAME:-}" ] || [ -z "${APEX_REPO_URL:-}" ]; then
  echo "[apex-bootstrap] ERROR: APEX_REPO_NAME or APEX_REPO_URL is empty in $CONFIG_ENV" >&2
  exit 1
fi

echo "[apex-bootstrap] Target repository: $APEX_REPO_NAME ($APEX_REPO_URL)"

# 1. Dynamic Swap Allocation
if ! swapon --show | grep -q "/swapfile"; then
  echo "[apex-bootstrap] Configuring dynamic swapfile..."
  TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
  ROOT_AVAIL_KB=$(df -k / | awk 'NR==2 {print $4}')
  SWAP_SIZE_MB=2048
  if [ "$TOTAL_MEM_KB" -gt 4194304 ]; then
    SWAP_SIZE_MB=4096
  fi
  if [ "$ROOT_AVAIL_KB" -gt $((SWAP_SIZE_MB * 1024 * 2)) ]; then
    echo "[apex-bootstrap] Allocating ${SWAP_SIZE_MB}MB swap..."
    fallocate -l "${SWAP_SIZE_MB}M" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_SIZE_MB"
    chmod 0600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    if ! grep -q "/swapfile" /etc/fstab; then
      echo "/swapfile none swap sw 0 0" >> /etc/fstab
    fi
  else
    echo "[apex-bootstrap] WARNING: Insufficient free space on rootfs for swapfile. Skipping swap."
  fi
fi

# 2. Storage Tiers & ZFS Configuration
mkdir -p /srv
for TIER_NUM in 1 2 3; do
  TIER_PATH="/srv/tier-${TIER_NUM}.${APEX_REPO_NAME}"
  DEV_VAR="APEX_TIER${TIER_NUM}_DEVICE"
  DEV="${!DEV_VAR:-}"

  if [ -n "$DEV" ] && [ -b "$DEV" ]; then
    echo "[apex-bootstrap] Checking mapped tier $TIER_NUM on device: $DEV"
    POOL_NAME="tier-${TIER_NUM}"
    
    # Check if pool already exists on the device or in zfs
    if zpool list -H -o name 2>/dev/null | grep -qw "$POOL_NAME"; then
      echo "[apex-bootstrap] ZFS pool $POOL_NAME is already imported."
    elif zpool import 2>/dev/null | grep -qw "$POOL_NAME"; then
      echo "[apex-bootstrap] Importing existing ZFS pool $POOL_NAME..."
      zpool import -f "$POOL_NAME"
    else
      echo "[apex-bootstrap] Creating new ZFS pool $POOL_NAME on $DEV..."
      zpool create -f -o ashift=12 \
        -O compression=lz4 \
        -O atime=off \
        -O mountpoint="$TIER_PATH" \
        "$POOL_NAME" "$DEV"
    fi
    zfs set mountpoint="$TIER_PATH" "$POOL_NAME" || true
  else
    echo "[apex-bootstrap] Tier $TIER_NUM device unmapped. Using rootfs directory: $TIER_PATH"
    mkdir -p "$TIER_PATH"
  fi
done

# 3. User Directories & Permissions
mkdir -p /srv/docker
mkdir -p /home/adam/.local/bin /home/adam/.ssh
ln -sf /usr/local/bin/ufw-docker /home/adam/.local/bin/ufw-docker

echo "[apex-bootstrap] Setting permissions on /srv and home directory..."
chown -R adam:adam /srv /home/adam/.local /home/adam/.ssh
chmod 0700 /home/adam/.ssh
if [ -f /home/adam/.ssh/id_ed25519 ]; then
  chmod 0600 /home/adam/.ssh/id_ed25519
fi

# 4. SSH Host Key Verification for GitHub
echo "[apex-bootstrap] Registering GitHub SSH host keys..."
ssh-keyscan -t ed25519,ecdsa,rsa github.com >> /home/adam/.ssh/known_hosts 2>/dev/null || true
chown adam:adam /home/adam/.ssh/known_hosts
chmod 0644 /home/adam/.ssh/known_hosts

# 5. Clone Repository
REPO_DIR="/srv/${APEX_REPO_NAME}"
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[apex-bootstrap] Cloning repository $APEX_REPO_URL as user adam..."
  sudo -u adam git clone "$APEX_REPO_URL" "$REPO_DIR"
else
  echo "[apex-bootstrap] Repository already cloned at $REPO_DIR."
fi

# 6. Execute ./init.sh
INIT_SCRIPT="$REPO_DIR/init.sh"
if [ -f "$INIT_SCRIPT" ]; then
  echo "[apex-bootstrap] Running ./init.sh from $REPO_DIR as user adam..."
  sudo -u adam bash -c "cd '$REPO_DIR' && bash ./init.sh"
  echo "[apex-bootstrap] ./init.sh completed successfully."
else
  echo "[apex-bootstrap] No ./init.sh found in $REPO_DIR. Skipping."
fi

# 7. Complete & Mark Bootstrapped
touch "$MARKER"
echo "============================================================"
echo " [apex-bootstrap] Node bootstrap finished successfully!     "
echo " Next steps: SSH as adam on port 2222, fill secrets & run:  "
echo "   apex tiers/link <tier1> <tier2> <tier3>                  "
echo "   apex tiers/useradd && apex tiers/chown                   "
echo "   apex configure && apex compose up                        "
echo "============================================================"
BOOTSTRAP_EOF

chmod 0755 /usr/local/bin/apex-bootstrap
echo "=== /usr/local/bin/apex-bootstrap baked successfully ==="
