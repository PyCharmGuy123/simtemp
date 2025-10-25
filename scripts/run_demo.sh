#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(dirname "$0")/..
KERNEL_DIR="$ROOT_DIR/kernel"
MODULE="$KERNEL_DIR/nxp_simtemp.ko"

#!/usr/bin/env bash
# Lightweight demo script for simtemp
# Runs: build -> insmod -> run CLI test -> show stats -> rmmod -> dmesg tail

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KDIR="$REPO_ROOT/kernel"
CLI="$REPO_ROOT/user/cli/main.py"
KO="$KDIR/nxp_simtemp.ko"

echo "[simtemp] repo: $REPO_ROOT"

# Build kernel module
echo "[simtemp] Building kernel module..."
make -C /lib/modules/$(uname -r)/build M="$KDIR" modules

# Insert module (try to remove any earlier instance first)
echo "[simtemp] Ensuring module is not loaded..."
sudo rmmod nxp_simtemp >/dev/null 2>&1 || true

echo "[simtemp] Inserting module $KO"
sudo insmod "$KO"

# Give the driver a moment to schedule its first sample
sleep 0.2

# Run CLI in test mode (requires sudo for sysfs writes/reads where applicable)
if [ ! -x "$CLI" ]; then
  echo "[simtemp] Running CLI via python3 $CLI --test"
else
  echo "[simtemp] Running CLI (executable) $CLI --test"
fi
sudo python3 "$CLI" --test || true

# Show stats
echo "[simtemp] Stats from sysfs:"
sudo cat /sys/devices/platform/nxp_simtemp/stats || true

# Remove module
echo "[simtemp] Removing module"
sudo rmmod nxp_simtemp || true

# Tail kernel log
echo "[simtemp] dmesg (last 80 lines):"
sudo dmesg | tail -n 80

echo "[simtemp] Demo finished"
