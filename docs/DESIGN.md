# DESIGN — simtemp

This document summarizes architecture, locking choices and lifetime model.

Overview

- Producer: `delayed_work` (`simdev_work_fn`) periodically generates
  `struct simtemp_sample` and pushes into a `kfifo`.
- Consumer: character device reads pop one record at a time; `poll()` wakes on
  data or threshold alerts.
- Controls: `sysfs` attributes (`sampling_ms`, `threshold_mC`, `mode`, `debug`, `stats`).

Locking

- `spinlock_t lock`: protects the `kfifo` and `alert_pending`. Used in the
  producer and in `poll()`/`read()` when manipulating FIFO or alert flag. Chosen
  because `delayed_work` runs in soft-context where sleeping is undesirable when
  the critical region is small (The critical region is very small (check space, copy a small struct, update a flag), so the cost of briefly spinning is lower than the sleep/wake overhead of a mutex. A spinlock therefore protects the kfifo and alert flag efficiently; using a sleeping lock would add scheduler overhead and higher latency under high-rate access).
- `mutex attr_lock`: protects configuration fields that are manipulated via
  sysfs (`sampling_ms`, `threshold_mC`, `mode`, `debug`). These handlers sleep
  and the mutex is appropriate.

Lifetime model

 - Current approach: `get_device()`/`put_device()` are used to pin (temporarily
   increment the device's reference count to prevent it from being freed while in use)
   the underlying `struct device` during sysfs handlers and while files are open.
   `simdev_remove` sets `s->stopping` and uses `cancel_delayed_work_sync()` to
   ensure no producer work remains scheduled, then removes sysfs and destroys
   device/class entries, i did not implement the kref-based lifetime
  model in this iteration primarily to keep the driver simple and focused for
  the submission. Converting to a `kref`-owned `struct simdev` would require a
  refactor of many code paths (work, open/close, and sysfs handlers) and a
  more extensive test matrix to validate corner-case races. The current
  `get_device()`/`put_device()` plus `cancel_delayed_work_sync()` approach is
  straightforward, easier to review, and adequate for the expected demo and
  evaluation scenarios.

DT binding

- `of_match_table` includes `{ .compatible = "nxp,simtemp" }`.
- Probe reads `sampling-ms` and `threshold-mC` properties if present.

DT binding and how the demo binds in QEMU

- Binding modes supported:
  - Device‑Tree (preferred on DT-enabled systems): the driver exports an `of_match_table` that includes `{ .compatible = "nxp,simtemp" }`. When a matching DT node is present at boot the kernel will attach the driver and the probe will read optional DT properties (`sampling-ms`, `threshold-mC`) to initialize defaults.
  - Module-created / runtime platform device: on non‑DT systems, the execution flow loads the out‑of‑tree module from the initramfs (`insmod nxp_simtemp.ko`). The module registers the platform/character device during init so the driver's probe runs immediately after load; this produces `/sys/class/simtemp/simtemp/` and `/dev/simtemp` which are used by the GUI and tests.

Scaling notes and possible improves

- At very high sampling rates, using `delayed_work` and `kfifo` may become a
  bottleneck.

With more time I would prototype and validate one or more targeted
improvements, these are the likely options and
their rationale:

- hrtimers + realtime worker thread
  - Use an hrtimer for low-jitter periodic wakeups and wake a high-priority
    kthread or realtime workqueue to perform sample generation/commit. This
    reduces timer jitter and keeps the producer off general purpose workqueues.

Testing and validation

- Unit tests
  - Add KUnit tests where practical. Add userspace unit tests for the CLI, installer logic and
    parsing/formatting helpers.

- Integration tests
  - Automated VM based tests that build and load the module, exercise a range
    of `sampling_ms` values, verify sample integrity, measure drop rate and
    latency.

## API contract (record format, sysfs, semantics)

This section collects the low-level API contract exposed by the `nxp_simtemp`
driver. 
The implementation is in `kernel/nxp_simtemp.c` / `kernel/nxp_simtemp.h` and userspace clients
(`user/cli`, `user/gui/app.py`) it relies on the following.

- Record layout (binary)
  - Struct name: `struct simtemp_sample` (refer to `kernel/nxp_simtemp.h`). Fields:
    - `timestamp_ns`: u64, nanoseconds due to epoch.
    - `temp_mC`: s32, temperature in milli-Celsius.
    - `flags`: u32, bitfield (see flags below).
  - Python client format string (used in `user/gui/app.py`): `SAMPLE_FMT = "=QiI"`.
    The leading `=` forces native byte order with standard sizes which matches
    the kernel struct packing used by the driver; this is portable across
    typical x86_64 and ARM systems but note that on big-endian hosts byte order
    differs and clients must use the same native byte order to interpret
    samples correctly.

- Flags (bit meanings)
  - `SIMTEMP_FLAG_NEW_SAMPLE` = 0x1 : record is a fresh sample.
  - `SIMTEMP_FLAG_THRESHOLD` = 0x2 : sample met or exceeded the configured threshold.

- Device & paths
  - Character device node: `/dev/simtemp` (major/minor assigned at probe time).
  - Sysfs class path: `/sys/class/simtemp/simtemp/` (prioritized by GUI).
  - Alternative platform sysfs path: `/sys/devices/platform/nxp_simtemp/`.

- Read semantics (user read of `/dev/simtemp`)
  - Each read should request at least the sizeof(record) (check SAMPLE_SIZE).
  - If `count < sizeof(record)` the driver returns `-EINVAL`.
  - Blocking reads: read blocks until a full record is available or the device
    is being torn down. If the FIFO is empty and the file was opened with
    `O_NONBLOCK` the driver returns `-EAGAIN`.
  - On successful read the driver copies a full packed `simtemp_sample` into
    user space and returns the record size.
  - If `copy_to_user()` fails the driver returns `-EFAULT`.
  - If the device is stopping or in an invalid state the driver returns `-EIO`.

- poll/select/epoll semantics
  - The driver uses a wait queue to integrate with `poll()`/`select()`/`epoll`.
  - When data is available the driver reports `POLLIN | POLLRDNORM`.
  - When an alert (threshold crossing) is pending the driver reports `POLLPRI`.
  - Alert state is cleared when a threshold-bearing sample is consumed by a
    read (design choice implemented in the driver).

- Sysfs attributes (names, access, defaults)
  - `sampling_ms` (read/write): unsigned integer milliseconds. Default: 1000.
    Writing a new value reschedules the producer; write `0` is rejected with
    `-EINVAL`.
  - `threshold_mC` (read/write): signed integer milli-Celsius. Default: 45000.
  - `mode` (read/write): textual mode name. Accepted names: `normal`, `ramp`, `noisy`.
    Default: `normal`.
  - `stats` (read-only): textual counters `updates=<n> alerts=<n> drops=<n>`.
  - `debug` (read/write): 0/1 to toggle verbose logging.

- Device Tree (optional)
  - The driver reads DT properties `sampling-ms` and `threshold-mC` if present to
    override defaults at probe time.

- Error modes & behavior summary
  - `-EINVAL` for invalid arguments (e.g., small read buffer, invalid sysfs value).
  - `-EAGAIN` for non-blocking reads with no data.
  - `-EIO` when the device is stopping or on unexpected IO teardown races.
  - `-EFAULT` when copy_to_user fails.
  - `-ENODEV` may be returned by sysfs handlers if driver data is unavailable.

- Endianness / portability note
  - The driver exposes a packed struct using native host endianness and
    standard C type sizes; user-space clients must use the same byte-order and
    sizes when unpacking. `user/gui/app.py` uses `SAMPLE_FMT = "=QiI"` which
    matches the kernel layout on the host (native byte-order).

