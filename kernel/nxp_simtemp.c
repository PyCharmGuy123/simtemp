#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/cdev.h>
#include <linux/poll.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/kfifo.h>
#include <linux/spinlock.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <linux/jiffies.h>
#include <linux/delay.h>
#include <linux/uaccess.h>
#include <linux/of.h>
#include <linux/mutex.h>
#include <linux/timekeeping.h>
#include <linux/wait.h>

#include "nxp_simtemp.h"

struct simdev {
    struct platform_device *pdev;
    struct cdev cdev;
    dev_t devt;
    struct kfifo fifo;
    spinlock_t lock;
    struct delayed_work work;
    unsigned int sampling_ms;
    int threshold_mC;
    wait_queue_head_t read_wq;
    atomic_t updates;
    atomic_t alerts;
    atomic_t drops;
    bool alert_pending;
    bool debug;
    struct mutex attr_lock;
    struct device *class_dev;
    int ramp;
    int mode;
    bool stopping;
};

static struct class *simtemp_class;
static struct platform_device *nxp_local_pd;

static void simdev_schedule_next(struct simdev* s)
{
    if (!s)
        return;
    schedule_delayed_work(&s->work, msecs_to_jiffies(s->sampling_ms));
}

static void simdev_work_fn(struct work_struct* work)
{
    struct simdev* s = container_of(work, struct simdev, work.work);
    struct simtemp_sample sample;
    unsigned long flags;
    int written;
    int cur_mode;

    if (!s)
        return;

    if (s->stopping)
        return;

    sample.timestamp_ns = ktime_get_ns();
    /* capture mode under attr_lock to avoid races with sysfs writers */
    mutex_lock(&s->attr_lock);
    cur_mode = s->mode;
    mutex_unlock(&s->attr_lock);

    /* generate sample according to mode */
    if (cur_mode == 1) { /* ramp */
        sample.temp_mC = 25000 + ((s->ramp * 200) % 40000);
        s->ramp++;
    } else if (cur_mode == 2) { /* noisy */
        /* base around 30000 with a small pseudo-random jitter */
        sample.temp_mC = 30000 + ((s->ramp * 37) % 4001) - 2000;
        s->ramp++;
    } else { /* normal */
        sample.temp_mC = 30000 + (s->ramp % 20000);
        s->ramp++;
    }
    sample.flags = SIMTEMP_FLAG_NEW_SAMPLE;

    /* decide flags but defer alert state update until after successful enqueue */
    spin_lock_irqsave(&s->lock, flags);
    if (sample.temp_mC >= s->threshold_mC)
        sample.flags |= SIMTEMP_FLAG_THRESHOLD;

    if (kfifo_avail(&s->fifo) < sizeof(sample)) {
        struct simtemp_sample tmp;
        int evicted = kfifo_out(&s->fifo, &tmp, sizeof(tmp));
        if (evicted == sizeof(tmp)) {
            /* removed one old sample */
            atomic_inc(&s->drops);
        } else {
            /* eviction failed: cannot make room, count incoming sample as dropped */
            atomic_inc(&s->drops);
            spin_unlock_irqrestore(&s->lock, flags);
            /* schedule next sample and return without waking readers */
            if (!s->stopping)
                simdev_schedule_next(s);
            return;
        }
    }

    written = kfifo_in(&s->fifo, &sample, sizeof(sample));
    if (written != sizeof(sample)) {
        /* insertion failed despite eviction; count drop and skip waking */
        atomic_inc(&s->drops);
        spin_unlock_irqrestore(&s->lock, flags);
        if (!s->stopping)
            simdev_schedule_next(s);
        return;
    }

    /* insertion succeeded: update alert state and counters while still under lock */
    if ((sample.flags & SIMTEMP_FLAG_THRESHOLD) && !s->alert_pending) {
        s->alert_pending = true;
        atomic_inc(&s->alerts);
    }
    /* count successful update */
    atomic_inc(&s->updates);
    spin_unlock_irqrestore(&s->lock, flags);

    /* wake readers: POLLIN for new data, POLLPRI for alerts */
    wake_up_interruptible(&s->read_wq);

    if (!s->stopping)
        simdev_schedule_next(s);
}

static int simdev_open(struct inode *inode, struct file *file)
{
    struct simdev *s;

    if (!inode || !inode->i_cdev)
        return -ENODEV;

    s = container_of(inode->i_cdev, struct simdev, cdev);
    if (!s)
        return -ENODEV;
    if (s->stopping)
        return -EIO;
    /* pin the underlying device while the file is open */
    if (s->pdev && s->pdev->dev.parent)
        get_device(&s->pdev->dev);
    file->private_data = s;
    return 0;
}

static int simdev_release(struct inode *inode, struct file *file)
{
    struct simdev *s = file->private_data;
    if (s) {
        if (s->pdev && s->pdev->dev.parent)
            put_device(&s->pdev->dev);
    }
    file->private_data = NULL;
    return 0;
}

static ssize_t simdev_read(struct file *file, char __user *buf, size_t count, loff_t *ppos)
{
    struct simdev *s = file->private_data;
    if (!s)
        return -ENODEV;
    struct simtemp_sample sample;
    int ret;

    if (count < sizeof(sample))
        return -EINVAL;

    /* wait for data */
    if (kfifo_is_empty(&s->fifo)) {
        if (file->f_flags & O_NONBLOCK)
            return -EAGAIN;
        ret = wait_event_interruptible(s->read_wq, !kfifo_is_empty(&s->fifo) || s->stopping);
        if (ret)
            return ret;
    }

    if (s->stopping)
        return -EIO;

    /* pop one record */
    if (kfifo_out(&s->fifo, &sample, sizeof(sample)) != sizeof(sample))
        return -EIO;

    if (copy_to_user(buf, &sample, sizeof(sample)))
        return -EFAULT;

    /* if this read consumed the alert clear it under lock */
    if (sample.flags & SIMTEMP_FLAG_THRESHOLD) {
        unsigned long __flags;
        spin_lock_irqsave(&s->lock, __flags);
        s->alert_pending = false;
        spin_unlock_irqrestore(&s->lock, __flags);
    }

    return sizeof(sample);
}

static unsigned int simdev_poll(struct file *file, struct poll_table_struct *wait)
{
    struct simdev *s = file->private_data;
    if (!s)
        return POLLERR;
    unsigned int mask = 0;
    unsigned long flags;

    poll_wait(file, &s->read_wq, wait);

    spin_lock_irqsave(&s->lock, flags);
    if (!kfifo_is_empty(&s->fifo))
        mask |= POLLIN | POLLRDNORM;
    if (s->alert_pending)
        mask |= POLLPRI;
    spin_unlock_irqrestore(&s->lock, flags);

    return mask;
}

static const struct file_operations simdev_fops = {
    .owner = THIS_MODULE,
    .open = simdev_open,
    .release = simdev_release,
    .read = simdev_read,
    .poll = simdev_poll,
};

/* sysfs attributes */
static ssize_t sampling_ms_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct simdev *s;
    ssize_t ret;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    mutex_lock(&s->attr_lock);
    ret = sprintf(buf, "%u\n", s->sampling_ms);
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return ret;
}

static ssize_t sampling_ms_store(struct device *dev, struct device_attribute *attr,
                              const char *buf, size_t count)
{
    struct simdev *s;
    unsigned int v;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    if (kstrtouint(buf, 0, &v)) {
        put_device(dev);
        return -EINVAL;
    }

    if (v == 0) {
        put_device(dev);
        return -EINVAL;
    }

    mutex_lock(&s->attr_lock);
    s->sampling_ms = v;
    mutex_unlock(&s->attr_lock);
    /* reschedule with new period */
    cancel_delayed_work_sync(&s->work);
    if (!s->stopping)
        simdev_schedule_next(s);
    put_device(dev);
    return count;
}

static DEVICE_ATTR(sampling_ms, 0644, sampling_ms_show, sampling_ms_store);

static ssize_t threshold_mC_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct simdev *s;
    ssize_t ret;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    mutex_lock(&s->attr_lock);
    ret = sprintf(buf, "%d\n", s->threshold_mC);
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return ret;
}

static ssize_t threshold_mC_store(struct device *dev, struct device_attribute *attr,
                               const char *buf, size_t count)
{
    struct simdev *s;
    int v;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    if (kstrtoint(buf, 0, &v)) {
        put_device(dev);
        return -EINVAL;
    }

    mutex_lock(&s->attr_lock);
    s->threshold_mC = v;
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return count;
}

static DEVICE_ATTR(threshold_mC, 0644, threshold_mC_show, threshold_mC_store);

static ssize_t stats_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct simdev *s;
    int updates, alerts, drops;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }
    updates = atomic_read(&s->updates);
    alerts = atomic_read(&s->alerts);
    drops = atomic_read(&s->drops);
    put_device(dev);
    return sprintf(buf, "updates=%d alerts=%d drops=%d\n", updates, alerts, drops);
}

static DEVICE_ATTR(stats, 0444, stats_show, NULL);

static ssize_t debug_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct simdev *s;
    ssize_t ret;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    mutex_lock(&s->attr_lock);
    ret = sprintf(buf, "%d\n", s->debug ? 1 : 0);
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return ret;
}

static ssize_t debug_store(struct device *dev, struct device_attribute *attr,
                          const char *buf, size_t count)
{
    struct simdev *s;
    int v;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    if (kstrtoint(buf, 0, &v)) {
        put_device(dev);
        return -EINVAL;
    }

    mutex_lock(&s->attr_lock);
    s->debug = (v != 0);
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return count;
}

static DEVICE_ATTR(debug, 0644, debug_show, debug_store);

static const char *mode_names[] = { "normal", "ramp", "noisy" };

static ssize_t mode_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct simdev *s;
    ssize_t ret;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    mutex_lock(&s->attr_lock);
    ret = sprintf(buf, "%s\n", mode_names[s->mode < 0 || s->mode > 2 ? 0 : s->mode]);
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return ret;
}

static ssize_t mode_store(struct device *dev, struct device_attribute *attr,
                         const char *buf, size_t count)
{
    struct simdev *s;
    char tmp[32];
    int i;

    get_device(dev);
    s = dev_get_drvdata(dev);
    if (!s) {
        put_device(dev);
        return -ENODEV;
    }

    if (count >= sizeof(tmp)) {
        put_device(dev);
        return -EINVAL;
    }
    memcpy(tmp, buf, count);
    tmp[count] = '\0';
    /* strip trailing newline */
    if (count && tmp[count-1] == '\n')
        tmp[count-1] = '\0';

    mutex_lock(&s->attr_lock);
    for (i = 0; i < ARRAY_SIZE(mode_names); i++) {
        if (!strcmp(tmp, mode_names[i])) {
            s->mode = i;
            mutex_unlock(&s->attr_lock);
            put_device(dev);
            return count;
        }
    }
    mutex_unlock(&s->attr_lock);
    put_device(dev);
    return -EINVAL;
}

static DEVICE_ATTR(mode, 0644, mode_show, mode_store);

static int simdev_probe(struct platform_device *pdev)
{
    struct simdev *s;
    int ret;
    struct device_node *np = pdev->dev.of_node;

    s = devm_kzalloc(&pdev->dev, sizeof(*s), GFP_KERNEL);
    if (!s)
        return -ENOMEM;

    platform_set_drvdata(pdev, s);
    s->pdev = pdev;
    spin_lock_init(&s->lock);
    init_waitqueue_head(&s->read_wq);
    mutex_init(&s->attr_lock);
    atomic_set(&s->updates, 0);
    atomic_set(&s->alerts, 0);
    atomic_set(&s->drops, 0);
    s->debug = false;
    s->sampling_ms = 1000; /* default 1s */
    s->threshold_mC = 45000; /* default 45°C */
    s->mode = 0; /* normal */
    s->ramp = 0;
    s->stopping = false;

    /* Binding for compatible Device Trees */
    if (np) {
        u32 val;
        if (!of_property_read_u32(np, "sampling-ms", &val))
            s->sampling_ms = val;
        if (!of_property_read_u32(np, "threshold-mC", &val))
            s->threshold_mC = (int)val;
    }

    ret = kfifo_alloc(&s->fifo, SIMTEMP_FIFO_ENTRIES * sizeof(struct simtemp_sample), GFP_KERNEL);
    if (ret)
        return -ENOMEM;
    /* register char device */
    if (alloc_chrdev_region(&s->devt, 0, 1, "simtemp") < 0) {
        kfifo_free(&s->fifo);
        return -ENOMEM;
    }
    cdev_init(&s->cdev, &simdev_fops);
    s->cdev.owner = THIS_MODULE;
    ret = cdev_add(&s->cdev, s->devt, 1);
    if (ret) {
        unregister_chrdev_region(s->devt, 1);
        kfifo_free(&s->fifo);
        return ret;
    }
    if (simtemp_class) {
        /* create a named device under /sys/class/simtemp/simtemp */
        s->class_dev = device_create(simtemp_class, NULL, s->devt, NULL, "simtemp");
        if (IS_ERR(s->class_dev))
            s->class_dev = NULL;
        else
            /* mirror driver data so class-device sysfs handlers can find `s` */
            dev_set_drvdata(s->class_dev, s);
    }

    /* create simple sysfs attributes on the platform device */
    /* expose attributes under the platform device and class device */
    dev_set_drvdata(&pdev->dev, s);
    /* create attributes on the platform device */
    if (device_create_file(&pdev->dev, &dev_attr_sampling_ms))
        dev_warn(&pdev->dev, "failed to create sampling_ms sysfs\n");
    if (device_create_file(&pdev->dev, &dev_attr_threshold_mC))
        dev_warn(&pdev->dev, "failed to create threshold_mC sysfs\n");
    if (device_create_file(&pdev->dev, &dev_attr_stats))
        dev_warn(&pdev->dev, "failed to create stats sysfs\n");
    if (device_create_file(&pdev->dev, &dev_attr_debug))
        dev_warn(&pdev->dev, "failed to create debug sysfs\n");
    if (device_create_file(&pdev->dev, &dev_attr_mode))
        dev_warn(&pdev->dev, "failed to create mode sysfs\n");
    /* expose the same attributes under the class device for /sys/class/simtemp/ */
    if (s->class_dev) {
        if (device_create_file(s->class_dev, &dev_attr_sampling_ms))
            dev_warn(&pdev->dev, "failed to create class sampling_ms sysfs\n");
        if (device_create_file(s->class_dev, &dev_attr_threshold_mC))
            dev_warn(&pdev->dev, "failed to create class threshold_mC sysfs\n");
        if (device_create_file(s->class_dev, &dev_attr_stats))
            dev_warn(&pdev->dev, "failed to create class stats sysfs\n");
        if (device_create_file(s->class_dev, &dev_attr_debug))
            dev_warn(&pdev->dev, "failed to create class debug sysfs\n");
        if (device_create_file(s->class_dev, &dev_attr_mode))
            dev_warn(&pdev->dev, "failed to create class mode sysfs\n");
    }

    INIT_DELAYED_WORK(&s->work, simdev_work_fn);
    /* schedule first sample */
    simdev_schedule_next(s);

    dev_info(&pdev->dev, "nxp_simtemp probed, device major=%d minor=%d\n", MAJOR(s->devt), MINOR(s->devt));
    return 0;
}

static void simdev_remove(struct platform_device *pdev)
{
    struct simdev *s = dev_get_drvdata(&pdev->dev);

    if (!s)
        return;

    if (s->debug)
        dev_info(&pdev->dev, "nxp_simtemp remove: start\n");

    s->stopping = true;
    cancel_delayed_work_sync(&s->work);
    if (s->debug)
        dev_info(&pdev->dev, "nxp_simtemp remove: cancelled work\n");
    device_remove_file(&pdev->dev, &dev_attr_sampling_ms);
    device_remove_file(&pdev->dev, &dev_attr_threshold_mC);
    device_remove_file(&pdev->dev, &dev_attr_stats);
    device_remove_file(&pdev->dev, &dev_attr_debug);
    device_remove_file(&pdev->dev, &dev_attr_mode);
    if (s->class_dev && simtemp_class) {
        /* remove class-device attributes first, then destroy the device */
        device_remove_file(s->class_dev, &dev_attr_sampling_ms);
        device_remove_file(s->class_dev, &dev_attr_threshold_mC);
        device_remove_file(s->class_dev, &dev_attr_stats);
        device_remove_file(s->class_dev, &dev_attr_debug);
        device_remove_file(s->class_dev, &dev_attr_mode);
        /* clear drvdata so sysfs handlers that race in won't see a stale pointer */
        dev_set_drvdata(s->class_dev, NULL);
        device_destroy(simtemp_class, s->devt);
        s->class_dev = NULL;
    }
    cdev_del(&s->cdev);
    unregister_chrdev_region(s->devt, 1);
    kfifo_free(&s->fifo);
    wake_up_interruptible_all(&s->read_wq);

    if (s->debug)
        dev_info(&pdev->dev, "nxp_simtemp remove: finished teardown\n");

    dev_info(&pdev->dev, "nxp_simtemp removed\n");
    return;
}

static const struct of_device_id simdev_of_match[] = {
    { .compatible = "nxp,simtemp" },
    { }
};
MODULE_DEVICE_TABLE(of, simdev_of_match);

static struct platform_driver simdev_driver = {
    .probe = simdev_probe,
    .remove = simdev_remove,
    .driver = {
        .name = "nxp_simtemp",
        .of_match_table = simdev_of_match,
    },
};

static int __init nxp_simtemp_init(void)
{
    int ret;

    /* create class 'simtemp' so device appears under /sys/class/simtemp/ */
    simtemp_class = class_create("simtemp");
    if (IS_ERR(simtemp_class))
        simtemp_class = NULL;

    ret = platform_driver_register(&simdev_driver);
    if (ret)
        return ret;

    /* create a local platform device for desktop testing */
    nxp_local_pd = platform_device_register_simple("nxp_simtemp", -1, NULL, 0);
    if (IS_ERR(nxp_local_pd)) {
        pr_warn("nxp_simtemp: failed to register local platform_device (non-fatal)\n");
        nxp_local_pd = NULL;
    }

    pr_info("nxp_simtemp module loaded\n");
    return 0;
}

static void __exit nxp_simtemp_exit(void)
{
    platform_driver_unregister(&simdev_driver);
    if (simtemp_class)
        class_destroy(simtemp_class);
    if (nxp_local_pd)
        platform_device_unregister(nxp_local_pd);
    pr_info("nxp_simtemp unloaded\n");
}

module_init(nxp_simtemp_init);
module_exit(nxp_simtemp_exit);

MODULE_AUTHOR("Diego Roldán Camacho");
MODULE_LICENSE("GPL v2");
MODULE_DESCRIPTION("Virtual sensor nxp_simtemp");
