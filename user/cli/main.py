#!/usr/bin/env python3
"""Simple CLI to read /dev/simtemp and print parsed records."""
import os
import struct
import time
import argparse
import select
import sys

RECORD_FMT = '<QiI'  # u64, s32, u32
RECORD_SIZE = struct.calcsize(RECORD_FMT)

def parse_record(data):
    ts_ns, temp_mC, flags = struct.unpack(RECORD_FMT, data)
    ts = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ts_ns // 1_000_000_000))
    ms = (ts_ns // 1_000_000) % 1000
    return f"{ts}.{ms:03d}Z temp={temp_mC/1000:.3f}C alert={(flags&2)!=0}"

def run_reader(path, timeout=None, test_mode=False):
    with open(path, 'rb', buffering=0) as f:
        p = select.poll()
        p.register(f, select.POLLIN | select.POLLPRI)
        while True:
            events = p.poll(timeout)
            if not events:
                # on timeout, return False in test_mode, otherwise continue
                if test_mode:
                    return False
                print("timeout")
                continue
            for fd, ev in events:
                if ev & (select.POLLIN | select.POLLPRI):
                    data = f.read(RECORD_SIZE)
                    if not data:
                        time.sleep(0.1)
                        continue
                    if len(data) != RECORD_SIZE:
                        print("partial read")
                        continue
                    # unpack to inspect alert flag for test mode
                    ts_ns, temp_mC, flags = struct.unpack(RECORD_FMT, data)
                    alert = (flags & 2) != 0
                    print(parse_record(data))
                    if test_mode and alert:
                        return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='/dev/simtemp')
    ap.add_argument('--test', action='store_true', help='program sysfs to trigger an alert and exit with 0 on success')
    args = ap.parse_args()
    if not os.path.exists(args.device):
        print(f"Device {args.device} not found")
        return

    if args.test:
        sysfs_base = '/sys/class/simtemp/simtemp'
        try:
            with open(os.path.join(sysfs_base, 'sampling_ms'), 'w') as f:
                f.write('100')
            with open(os.path.join(sysfs_base, 'mode'), 'w') as f:
                f.write('ramp')
            with open(os.path.join(sysfs_base, 'threshold_mC'), 'w') as f:
                f.write('26000')
        except Exception as e:
            print('failed to program sysfs for test:', e)
            sys.exit(2)
        ok = run_reader(args.device, timeout=5000, test_mode=True)
        if ok:
            print('TEST: alert observed')
            sys.exit(0)
        else:
            print('TEST: alert not observed')
            sys.exit(1)

    run_reader(args.device)

if __name__ == '__main__':
    main()
