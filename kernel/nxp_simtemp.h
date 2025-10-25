#ifndef _NXP_SIMTEMP_H
#define _NXP_SIMTEMP_H

#include <linux/types.h>

/* Sample record exposed to userspace */
struct simtemp_sample {
    __u64 timestamp_ns; /* nanoseconds */
    __s32 temp_mC;      /* milli-Celsius */
    __u32 flags;        /* bitmask (NEW_SAMPLE, THRESHOLD_CROSSED) */
} __attribute__((packed));

/* Flags for simtemp_sample.flags */
#define SIMTEMP_FLAG_NEW_SAMPLE    0x1
#define SIMTEMP_FLAG_THRESHOLD     0x2

/* FIFO capacity in number of records */
#define SIMTEMP_FIFO_ENTRIES 128

#endif /* _NXP_SIMTEMP_H */
