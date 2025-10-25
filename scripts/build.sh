#!/usr/bin/env bash
#set -e for quick failure
set -euo pipefail

KDIR=${KDIR:-/lib/modules/$(uname -r)/build}
ROOT_DIR=$(dirname "$0")/..
KERNEL_DIR="$ROOT_DIR/kernel"

if [ ! -d "$KDIR" ]; then
  echo "Kernel build dir $KDIR not found. Install kernel headers or set KDIR." >&2
  exit 2
fi

echo "Building kernel module..."
pushd "$KERNEL_DIR" >/dev/null
make KDIR="$KDIR"
popd >/dev/null

echo "Build complete. Module: $KERNEL_DIR/nxp_simtemp.ko"
