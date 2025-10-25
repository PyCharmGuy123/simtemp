# Test plan

This document contains concrete commands to execute the suggested test plan (T1–T6) for
the `nxp_simtemp` project. 

Prerequisites
-------------
- Build tools and kernel headers installed. On Debian/Ubuntu:

Notes
-----
- The commands below are for running tests locally on the host (no QEMU or
  VM required). They assume you have sudo privileges to insert/remove the
  kernel module and write sysfs attributes.

T1 — Load / Unload (basic)
---------------------------
Build and test load/unload on the host kernel.

```bash
# Build the module (using distro kernel build dir)
./scripts/qemu_build_module.sh --kdir /lib/modules/$(uname -r)/build

# Insert the module (as root)
sudo insmod kernel/nxp_simtemp.ko

# Check kernel logs for probe
dmesg | tail -n 20

# Confirm device and sysfs exist
ls -l /dev | grep simtemp || true
ls -l /sys/class/simtemp || true
cat /sys/class/simtemp/simtemp/stats || true

# Remove module cleanly
sudo rmmod nxp_simtemp
dmesg | tail -n 20
```

T2 — Periodic Read (sampling rate)
-----------------------------------
Set sampling_ms to 100 ms and verify ~10 samples/sec using the Python CLI or
using `dd` + `xxd` to inspect binary records.

```bash
# Load module
sudo insmod kernel/nxp_simtemp.ko

# Set sampling to 100 ms
echo 100 | sudo tee /sys/class/simtemp/simtemp/sampling_ms

# Use the provided CLI (preferred) to print readable samples:
python3 user/cli/main.py          # runs a continuous reader (if implemented)

# Or sample with dd+xxd to collect 10 records and inspect timestamps
# (struct is 16 bytes: u64 + s32 + u32)
dd if=/dev/simtemp bs=16 count=10 2>/dev/null | xxd

# Use timestamps to compute rate: (do in Python or manual inspection of ns differences)

# cleanup
sudo rmmod nxp_simtemp
```

T3 — Threshold Event (POLLPRI)
-------------------------------
Lower threshold to a value that will be crossed and verify poll/alert.

```bash
# Load module
sudo insmod kernel/nxp_simtemp.ko

# Set sampling faster to make the test quick
echo 100 | sudo tee /sys/class/simtemp/simtemp/sampling_ms

# Set threshold low to force alert quickly
echo 20000 | sudo tee /sys/class/simtemp/simtemp/threshold_mC

# cleanup
sudo rmmod nxp_simtemp
```

T4 — Error Paths
-----------------
Test invalid sysfs writes and behavior at fast sampling.

```bash
sudo insmod kernel/nxp_simtemp.ko

# invalid sampling_ms -> expect error / no change
echo 0 | sudo tee /sys/class/simtemp/simtemp/sampling_ms || true

# very fast sampling (1 ms) — check for stability (do short run)
echo 1 | sudo tee /sys/class/simtemp/simtemp/sampling_ms
timeout 5s dd if=/dev/simtemp bs=16 count=50 2>/dev/null | wc -c

# stats should increment
cat /sys/class/simtemp/simtemp/stats

sudo rmmod nxp_simtemp
```

T5 — Concurrency
-----------------
Run a reader in background while changing sysfs attributes concurrently and
then unload the module.

```bash
sudo insmod kernel/nxp_simtemp.ko

# background reader (prints hex) — runs for 10s
bash -c "timeout 10s dd if=/dev/simtemp bs=16 count=100 2>/dev/null | xxd" &
READER_PID=$!

# concurrently toggle mode and threshold
for i in 1 2 3 4 5; do
  echo normal  | sudo tee /sys/class/simtemp/simtemp/mode
  sleep 0.5
  echo ramp    | sudo tee /sys/class/simtemp/simtemp/mode
  sleep 0.5
  echo 35000   | sudo tee /sys/class/simtemp/simtemp/threshold_mC
done

wait $READER_PID || true

# attempt safe unload while readers were active
sudo rmmod nxp_simtemp
dmesg | tail -n 30
```

T6 — API Contract (struct size / endianness)
-------------------------------------------
Verify the size of the packed record and confirm byte order (host little-endian).

```bash
python3 - <<'PY'
import struct
print('struct sizes: u64+ s32 + u32 =', struct.calcsize('<Q i I'))
print('verify little-endian:', struct.pack('<I', 0x01020304))
PY

# grab one sample and parse on host to confirm values
sudo insmod kernel/nxp_simtemp.ko
dd if=/dev/simtemp bs=16 count=1 2>/dev/null | python3 - <<'PY'
import sys, struct
data = sys.stdin.buffer.read(16)
ts, temp, flags = struct.unpack('<Q i I', data)
print('ts', ts, 'temp_mC', temp, 'flags', flags)
PY
sudo rmmod nxp_simtemp
```


Extra debugging & diagnostics
-----------------------------
- If a test fails or you see unexpected behavior, collect the following
  information locally and paste it into an issue or send it to me:

```bash
# kernel logs (last 80 lines)
dmesg | tail -n 80

# class and driver listings
ls -l /sys/class/simtemp || true
ls -l /sys/bus/platform/drivers/nxp_simtemp || true

# device node and stats
ls -l /dev | grep simtemp || true
cat /sys/class/simtemp/simtemp/stats || true
```
End of file.
