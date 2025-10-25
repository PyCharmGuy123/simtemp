# simtemp — Virtual Sensor and driver system

This repo implements `nxp_simtemp`, a virtual temperature sensor kernel module
and a small Python CLI to read the device.

Prerequisites

- Linux system with kernel headers for the running kernel (package usually named `linux-headers-$(uname -r)` or similar).
- A working C toolchain (gcc, make) and build tools.
- sudo access for loading kernel modules and adjusting device/sysfs permissions during evaluation.

1) Build the kernel module

The repository contains the kernel module source under `kernel/`. There are two common approaches to build it:

- Demo (uses the repository helper scripts when available):

```bash
# from repo root
./scripts/build.sh   #This reduces the building process to just running a script, however, the whole process is documented
			         #In the next section

```

- Manual (standard out-of-tree module build):

```bash
# from repo root
cd kernel
make -C /lib/modules/$(uname -r)/build M=$(pwd) modules
```

After a successful build the object file `nxp_simtemp.ko` should be present in `kernel/`.

2) Load the module

```bash
# load module (requires root)
sudo insmod kernel/nxp_simtemp.ko

# verify module is mounted
lsmod | grep nxp_simtemp

# check device and sysfs paths (examples)
ls -l /dev/simtemp || true
ls -l /sys/class/simtemp/simtemp || true
```

If `insmod` fails because the module depends on symbols, build with the correct kernel headers or use `modprobe` after installing the module into the kernel tree.

3) 

If the driver is loaded and sysfs attributes exist you can read/write sample values (may require root or appropriate permissions):

```bash
# read current values
cat /sys/class/simtemp/simtemp/sampling_ms
cat /sys/class/simtemp/simtemp/threshold_mC
cat /sys/class/simtemp/simtemp/mode

Complete sysfs configuration
------------------------------

- `sampling_ms` (read/write) — sampling interval in milliseconds. Default: `1000`. Writing `0` is rejected (driver returns `-EINVAL`). Example:

	```bash
	echo 100 | sudo tee /sys/class/simtemp/simtemp/sampling_ms
	```

- `threshold_mC` (read/write) — temperature alert threshold in milli‑Celsius. Default: `45000` (45.0 °C). Set lower to trigger alerts quickly during tests.

	```bash
	echo 35000 | sudo tee /sys/class/simtemp/simtemp/threshold_mC
	```

- `mode` (read/write) — textual mode name that alters the sample generator's behavior. Accepted names: `normal`, `ramp`, `noisy`. Default: `normal`.

	```bash
	echo ramp | sudo tee /sys/class/simtemp/simtemp/mode
	```

- `stats` (read-only) — textual counters for basic diagnostics. Example output:

	```text
	updates=123 alerts=4 drops=0
	```

- `debug` (read/write) — enable or disable verbose logging (0/1). Default: `0`.

	```bash
	echo 1 | sudo tee /sys/class/simtemp/simtemp/debug
	```

These attributes are intended for quick manual testing and the GUI/CLI also rely on the same paths to configure the device during demos and evaluation.

````




