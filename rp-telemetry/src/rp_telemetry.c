/*
 * rp-telemetry -- minimal Zynq die-temperature telemetry daemon for the
 * Red Pitaya STEMlab 125-14 (Gen 1).
 *
 * Design goal: as close to zero CPU as a network service can get. The process
 * spends its entire life blocked in accept(). There is no polling loop, no
 * timer, no thread, no HTTP, no JSON, and no periodic logging. A request costs
 * one accept(), one recv(), one open/read/close of a single sysfs file, one
 * send(), and one close() -- plus the few /proc reads and the one extra
 * sysfs read described below.
 *
 * Protocol (line oriented, one request per connection):
 *
 *     ->  "STATUS\n"     <-  "RPT1 57.34 cpu=3.2 load1=0.41 memtotal=509216
 *                             memavail=311044 uptime=690.2 rootfree=1204880
 *                             vccaux=1.802\n"
 *                             (one line; temperature in degrees Celsius,
 *                              followed by zero or more key=value host metrics)
 *                        <-  "RPT1 ERR XADC\n"    (sysfs read failed)
 *     ->  "VERSION\n"    <-  "RPT1 VERSION 1.4.0\n"
 *     ->  anything else  <-  "RPT1 ERR COMMAND\n"
 *
 * The key=value tail is an *extension*: every key is independently optional,
 * and a reader that does not recognise one must ignore it. A client too old to
 * know about the tail parses the temperature exactly as before, which is why
 * this is not a new command -- see rp_telemetry.py's parse_status_line().
 *
 * Host metrics come from /proc and statvfs(), one small read each, on the same
 * request that reads the temperature. Nothing here samples on a timer: `cpu` is
 * the busy fraction *since the previous STATUS request*, computed from cached
 * /proc/stat counters, so a 30 s poll yields a 30 s average for free.
 *
 * Temperature source: the Zynq *processing-system* XADC exposed through Linux
 * IIO. The device directory is discovered once at startup by scanning
 * /sys/bus/iio/devices; the FPGA-backed XADC wizard, which carries the same
 * IIO name and whose registers vanish when the bitstream is reprogrammed, is
 * refused -- as is anything else that is not demonstrably the PS XADC (see
 * xadc_rank()). `in_temp0_offset` and `in_temp0_scale` are read
 * once at the same time (they are constants of the XADC transfer function).
 * Only `in_temp0_raw` is re-read per request.
 *
 *     temperature_c = (raw + offset) * scale / 1000.0
 *
 * Rail voltage (`vccaux`, since 1.4.0): the FPGA auxiliary supply, nominally
 * 1.8 V, read from the same PS XADC device already chosen for the temperature
 * -- never by scanning for it -- so it inherits the PS-only guarantee above.
 * `<channel>_vccaux_scale` is in mV per LSB and is read once at discovery;
 * `<channel>_vccaux_raw` is read per request.
 *
 *     vccaux = raw * scale / 1000.0
 *
 * The channel is found by *name suffix*, not by a fixed index: 1.3.0 tried to
 * read the +5 V input through the XADC's VP/VN pair at a hardcoded
 * `in_voltage8_vpvn_raw` and reported nothing on every board, because the
 * xilinx-xadc driver gives VP/VN no name suffix and, more to the point, this
 * board's devicetree declares no external channels at all. Only the internal
 * rails exist on the PS device. See TROUBLESHOOTING.md.
 *
 * What this is and is not: `vccaux` is a *regulated output*, not the board's
 * +5 V input, so a supply that sags a little is hidden by the regulator. It is
 * sampled once per STATUS request (every ~30 s from the gateway), so it shows
 * static or slowly varying rail levels, never millisecond droops. A missing
 * channel or an implausible value just leaves `vccaux` out.
 *
 * The service answers only the fixed protocol above: there is no code path
 * that executes anything supplied by a client. It is intended for a trusted
 * lab LAN, consistent with the rest of this repo's security model.
 */

/*
 * Must precede every include. On 32-bit ARM (the Red Pitaya's target), glibc's
 * non-LFS readdir() fails with EOVERFLOW whenever a directory entry's inode
 * number does not fit in 32 bits, which would make XADC discovery fail
 * outright. Defined here rather than in the build flags so the daemon is
 * correct however it happens to be compiled.
 */
#define _FILE_OFFSET_BITS 64

#include <dirent.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#define RPT_VERSION "1.4.0"
#define RPT_PROTOCOL "RPT1"

#define DEFAULT_PORT 18864
#define DEFAULT_IIO_ROOT "/sys/bus/iio/devices"
#define DEFAULT_PROC_ROOT "/proc"
/* Filesystem whose free space is reported. On every Red Pitaya image this
 * is the SD card, and filling it is a common and otherwise silent death. */
#define ROOT_FS_PATH "/"

/* A request is at most "VERSION\r\n". Anything longer is malformed by
 * definition, so the read buffer is a hard cap on what one client can make us
 * buffer. */
#define MAX_REQUEST 64
/* A STATUS line carrying every metric is ~110 bytes. The margin is for future
 * keys; it must stay at or below MAX_RESPONSE_BYTES in rp_telemetry.py, which
 * is the gateway's read limit for one line. */
#define MAX_RESPONSE 256
/* Receive/send timeout on an accepted socket. A client that connects and then
 * says nothing is dropped after this long instead of pinning the daemon. */
#define CLIENT_TIMEOUT_S 2

#define PATH_MAX_LEN 512

/*
 * The Zynq-7000 processing-system XADC, at a fixed address on every board.
 * See xadc_rank() for why the device has to be identified by address rather
 * than by its IIO name.
 */
#define PS_XADC_MARKER "f8007100"
/* Substring of the PL XADC wizard's device path ("83c00000.xadc_wiz"). */
#define PL_XADC_MARKER "adc_wiz"

/*
 * The rail reported alongside the temperature: the FPGA auxiliary supply,
 * nominally 1.8 V. Matched by name suffix over the chosen device's channels
 * rather than by a fixed index -- the index is stable in the xilinx-xadc
 * channel table today, but this daemon has already shipped one release that
 * reported nothing because it hardcoded a channel filename.
 */
#define RAIL_KEY "vccaux"
#define RAIL_CHANNEL_PREFIX "in_voltage"
#define RAIL_SUFFIX_RAW "_" RAIL_KEY "_raw"
#define RAIL_SUFFIX_SCALE "_" RAIL_KEY "_scale"
/* Rejects garbled sysfs values only; this is not a health threshold. */
#define RAIL_MIN_PLAUSIBLE_V 0.5
#define RAIL_MAX_PLAUSIBLE_V 3.0

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* --- sysfs helpers ----------------------------------------------------- */

/* Read a small text file into `buf` (NUL terminated). Returns 0 on success. */
static int read_small_file(const char *path, char *buf, size_t len)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    ssize_t n;
    do {
        n = read(fd, buf, len - 1);
    } while (n < 0 && errno == EINTR);
    close(fd);
    if (n <= 0) {
        return -1;
    }
    buf[n] = '\0';
    return 0;
}

static int read_long_file(const char *path, long *out)
{
    char buf[64];
    if (read_small_file(path, buf, sizeof(buf)) != 0) {
        return -1;
    }
    char *end = NULL;
    errno = 0;
    long value = strtol(buf, &end, 10);
    if (end == buf || errno == ERANGE) {
        return -1;
    }
    *out = value;
    return 0;
}

static int read_double_file(const char *path, double *out)
{
    char buf[64];
    if (read_small_file(path, buf, sizeof(buf)) != 0) {
        return -1;
    }
    char *end = NULL;
    errno = 0;
    double value = strtod(buf, &end);
    if (end == buf || errno == ERANGE) {
        return -1;
    }
    *out = value;
    return 0;
}

struct xadc {
    int ready;
    char raw_path[PATH_MAX_LEN];
    long offset;
    double scale;
    /* Rail channel on the same device; see xadc_read_rail(). */
    int have_rail;
    char rail_raw_path[PATH_MAX_LEN];
    double rail_scale;
    /* Refuse anything but the PS XADC. Set when scanning the real
     * /sys/bus/iio/devices; see main(). */
    int strict;
    /* Refusals are latched: discovery re-runs per request while it is
     * failing, and a warning per request is exactly the kind of steady
     * background work this daemon exists to avoid. */
    int warned_pl;
    int warned_unknown;
};

static int path_exists(const char *path)
{
    return access(path, R_OK) == 0;
}

/*
 * Classify one IIO device by where it actually lives on the SoC.
 *
 * A Red Pitaya exposes *two* IIO devices, and both are named "xadc":
 *
 *     iio:device0 -> /sys/devices/soc0/axi/f8007100.adc/       (PS XADC)
 *     iio:device1 -> /sys/devices/soc0/axi/83c00000.xadc_wiz/  (PL XADC wizard)
 *
 * The first is in the processing system and is always present and always
 * safe. The second is a core inside the FPGA bitstream. linien-server
 * reprograms the FPGA, and its bitstream has nothing at 0x83c00000 -- so
 * reading in_temp0_raw from that device issues an AXI access that nothing
 * answers: the bus hangs and the watchdog reboots the board. Since the name is
 * identical, the only way to tell them apart is the resolved device path.
 *
 * Returns 0 for the PS XADC, 1 for a device that cannot be classified, and
 * -1 for anything PL-backed or unresolvable, which must never be read.
 *
 * Note that rank 1 is not by itself a licence to read: matching on the name
 * `adc_wiz` only catches the wizard as Xilinx's tooling happens to name it,
 * and a PL peripheral under any other name would rank 1 and reboot the board.
 * So on a real Red Pitaya (see `strict`) rank 1 is refused too, and the
 * fallback exists only for a caller that pointed us somewhere else on purpose.
 */
static int xadc_rank(const char *iio_root, const char *name)
{
    char dir[PATH_MAX_LEN];
    if (snprintf(dir, sizeof(dir), "%s/%s", iio_root, name) >=
        (int)sizeof(dir)) {
        return -1;
    }
    /* The entries under /sys/bus/iio/devices are symlinks into
     * /sys/devices/...; the target is what names the hardware. */
    char *resolved = realpath(dir, NULL);
    if (resolved == NULL) {
        return -1; /* cannot prove it is safe, so do not touch it */
    }
    int rank = 1;
    if (strstr(resolved, PL_XADC_MARKER) != NULL) {
        rank = -1;
    } else if (strstr(resolved, PS_XADC_MARKER) != NULL) {
        rank = 0;
    }
    free(resolved);
    return rank;
}

/*
 * True for a channel file named like "in_voltage1_vccaux_raw". Matching on the
 * suffix rather than a fixed index is deliberate; see RAIL_KEY above.
 */
static int is_rail_raw_file(const char *name)
{
    size_t prefix_len = strlen(RAIL_CHANNEL_PREFIX);
    size_t suffix_len = strlen(RAIL_SUFFIX_RAW);
    size_t len = strlen(name);
    if (len <= prefix_len + suffix_len) {
        return 0;
    }
    if (strncmp(name, RAIL_CHANNEL_PREFIX, prefix_len) != 0) {
        return 0;
    }
    return strcmp(name + len - suffix_len, RAIL_SUFFIX_RAW) == 0;
}

/*
 * Look for the rail channel in `device_dir` -- the directory of the device just
 * chosen for the temperature, and no other. Never fails the discovery: a device
 * without the channel simply reports no `vccaux`.
 */
static void xadc_discover_rail(struct xadc *x, const char *device_dir)
{
    char channel[PATH_MAX_LEN] = "";
    char raw[PATH_MAX_LEN];
    char scale_path[PATH_MAX_LEN];
    double scale = 0.0;
    x->have_rail = 0;
    if (device_dir[0] == '\0') {
        return;
    }
    DIR *dir = opendir(device_dir);
    if (dir != NULL) {
        struct dirent *entry;
        while ((entry = readdir(dir)) != NULL) {
            if (is_rail_raw_file(entry->d_name)) {
                if (snprintf(channel, sizeof(channel), "%s", entry->d_name) >=
                    (int)sizeof(channel)) {
                    channel[0] = '\0';
                }
                break;
            }
        }
        closedir(dir);
    }
    /* The scale file is the same channel with the suffix swapped. */
    size_t stem = channel[0] == '\0'
                      ? 0
                      : strlen(channel) - strlen(RAIL_SUFFIX_RAW);
    if (channel[0] == '\0' ||
        snprintf(raw, sizeof(raw), "%s/%s", device_dir, channel) >=
            (int)sizeof(raw) ||
        snprintf(scale_path, sizeof(scale_path),
                 "%s/%.*s" RAIL_SUFFIX_SCALE, device_dir, (int)stem,
                 channel) >= (int)sizeof(scale_path) ||
        !path_exists(raw) || read_double_file(scale_path, &scale) != 0 ||
        !isfinite(scale) || scale <= 0.0) {
        fprintf(stderr,
                "rp-telemetry: no usable " RAIL_KEY " channel in %s; "
                RAIL_KEY " will not be reported\n",
                device_dir);
        return;
    }
    memcpy(x->rail_raw_path, raw, strlen(raw) + 1);
    x->rail_scale = scale;
    x->have_rail = 1;
    fprintf(stderr, "rp-telemetry: reading " RAIL_KEY " from %s\n", raw);
}

/*
 * Locate the IIO device exposing in_temp0_raw and cache the raw path plus the
 * (constant) offset and scale. Prefers the PS XADC and refuses FPGA-backed
 * devices outright; see xadc_rank(). Called once at startup; retried lazily
 * only after a failed read, never on the happy path.
 */
static int xadc_discover(struct xadc *x, const char *iio_root)
{
    DIR *dir = opendir(iio_root);
    if (dir == NULL) {
        return -1;
    }
    int found = -1;
    int best_rank = 2; /* worse than any acceptable rank */
    char best_dir[PATH_MAX_LEN] = "";
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        if (entry->d_name[0] == '.') {
            continue;
        }
        char raw[PATH_MAX_LEN];
        char offset_path[PATH_MAX_LEN];
        char scale_path[PATH_MAX_LEN];
        if (snprintf(raw, sizeof(raw), "%s/%s/in_temp0_raw", iio_root,
                     entry->d_name) >= (int)sizeof(raw)) {
            continue;
        }
        if (!path_exists(raw)) {
            continue;
        }
        int rank = xadc_rank(iio_root, entry->d_name);
        if (rank < 0) {
            if (!x->warned_pl) {
                x->warned_pl = 1;
                fprintf(stderr,
                        "rp-telemetry: ignoring FPGA-backed XADC %s/%s "
                        "(reading it can hang the AXI bus)\n",
                        iio_root, entry->d_name);
            }
            continue;
        }
        if (rank > 0 && x->strict) {
            if (!x->warned_unknown) {
                x->warned_unknown = 1;
                fprintf(stderr,
                        "rp-telemetry: ignoring unrecognised XADC %s/%s "
                        "(only the PS XADC at " PS_XADC_MARKER
                        " is safe to read)\n",
                        iio_root, entry->d_name);
            }
            continue;
        }
        if (rank >= best_rank) {
            continue; /* already holding something at least as good */
        }
        if (snprintf(offset_path, sizeof(offset_path), "%s/%s/in_temp0_offset",
                     iio_root, entry->d_name) >= (int)sizeof(offset_path)) {
            continue;
        }
        if (snprintf(scale_path, sizeof(scale_path), "%s/%s/in_temp0_scale",
                     iio_root, entry->d_name) >= (int)sizeof(scale_path)) {
            continue;
        }
        long offset = 0;
        double scale = 0.0;
        if (read_long_file(offset_path, &offset) != 0) {
            continue;
        }
        if (read_double_file(scale_path, &scale) != 0) {
            continue;
        }
        /* Copy only the initialized bytes: `raw` is a 512-byte stack buffer
         * that snprintf filled to the NUL, so copying sizeof() would read
         * uninitialized stack (harmless here, but sanitizers flag it). */
        memcpy(x->raw_path, raw, strlen(raw) + 1);
        x->offset = offset;
        x->scale = scale;
        x->ready = 1;
        found = 0;
        best_rank = rank;
        /* Always fits: `raw` is this directory plus a file name. */
        if (snprintf(best_dir, sizeof(best_dir), "%s/%s", iio_root,
                     entry->d_name) >= (int)sizeof(best_dir)) {
            best_dir[0] = '\0';
        }
        if (rank == 0) {
            break; /* the PS XADC; nothing can be better */
        }
    }
    closedir(dir);
    if (found == 0) {
        fprintf(stderr, "rp-telemetry: reading temperature from %s\n",
                x->raw_path);
        xadc_discover_rail(x, best_dir);
    } else {
        x->have_rail = 0;
    }
    return found;
}

/* Read the current die temperature in degrees Celsius. Returns 0 on success. */
static int xadc_read_temperature(struct xadc *x, const char *iio_root,
                                 double *out)
{
    if (!x->ready && xadc_discover(x, iio_root) != 0) {
        return -1;
    }
    long raw = 0;
    if (read_long_file(x->raw_path, &raw) != 0) {
        /* The device may have been renumbered (e.g. a module reload).
         * Re-discover once, then give up until the next request. */
        x->ready = 0;
        if (xadc_discover(x, iio_root) != 0) {
            return -1;
        }
        if (read_long_file(x->raw_path, &raw) != 0) {
            x->ready = 0;
            return -1;
        }
    }
    *out = ((double)(raw + x->offset)) * x->scale / 1000.0;
    return 0;
}

/* Rail volts from a raw reading: the XADC scale is mV per LSB. An internal
 * rail is measured directly, so there is no divider to undo. */
static double rail_volts_from_raw(long raw, double scale_mv)
{
    return (double)raw * scale_mv / 1000.0;
}

/*
 * Read the rail in volts. Returns 0 on success. Best-effort: it never re-runs
 * discovery (the temperature read owns that) and never logs, so a board whose
 * channel vanished just stops reporting `vccaux`.
 */
static int xadc_read_rail(const struct xadc *x, double *out)
{
    if (!x->ready || !x->have_rail) {
        return -1;
    }
    long raw = 0;
    if (read_long_file(x->rail_raw_path, &raw) != 0) {
        return -1;
    }
    double volts = rail_volts_from_raw(raw, x->rail_scale);
    if (!isfinite(volts) || volts < RAIL_MIN_PLAUSIBLE_V ||
        volts > RAIL_MAX_PLAUSIBLE_V) {
        return -1;
    }
    *out = volts;
    return 0;
}

/* --- host metrics ------------------------------------------------------- */

/*
 * Everything below is best-effort and independently optional: a metric that
 * cannot be read is left out of the response rather than failing it, so a
 * board with an unexpected /proc still reports its temperature.
 */

/* Minimum /proc/stat movement before a new CPU figure is computed, in jiffies
 * summed over all cores (~50 ms on a 2-core board at HZ=100). Below this the
 * sample window is too short to mean anything, and two clients polling at once
 * would otherwise turn a 30 s average into noise. */
#define CPU_MIN_DELTA_JIFFIES 10

struct metrics {
    const char *proc_root;
    /* /proc/stat counters from the previous STATUS request. CPU usage is a
     * delta, and this daemon has no timer to take one against -- so the
     * previous request is the baseline, making `cpu` the busy fraction over
     * the caller's own polling interval. */
    int have_prev_cpu;
    unsigned long long prev_total;
    unsigned long long prev_busy;
    /* Last computed percentage, re-served when a request arrives too soon
     * after the previous one to measure a fresh window. */
    int have_cpu_pct;
    double cpu_pct;
};

static int proc_path(const struct metrics *m, const char *name, char *out,
                     size_t len)
{
    if (snprintf(out, len, "%s/%s", m->proc_root, name) >= (int)len) {
        return -1;
    }
    return 0;
}

/*
 * Sum the aggregate "cpu" line of /proc/stat into total and busy jiffies.
 * busy is everything except idle and iowait -- a core waiting on I/O is not
 * doing work, and counting it as such makes an idle board look loaded.
 */
static int read_cpu_jiffies(const struct metrics *m, unsigned long long *total,
                            unsigned long long *busy)
{
    char path[PATH_MAX_LEN];
    char buf[512];
    if (proc_path(m, "stat", path, sizeof(path)) != 0) {
        return -1;
    }
    if (read_small_file(path, buf, sizeof(buf)) != 0) {
        return -1;
    }
    if (strncmp(buf, "cpu ", 4) != 0 && strncmp(buf, "cpu\t", 4) != 0) {
        return -1;
    }
    const char *cursor = buf + 3;
    unsigned long long sum = 0;
    unsigned long long idle = 0;
    int field = 0;
    while (*cursor != '\0' && *cursor != '\n') {
        while (*cursor == ' ' || *cursor == '\t') {
            cursor++;
        }
        if (!isdigit((unsigned char)*cursor)) {
            break;
        }
        char *end = NULL;
        errno = 0;
        unsigned long long value = strtoull(cursor, &end, 10);
        if (end == cursor || errno == ERANGE) {
            return -1;
        }
        cursor = end;
        sum += value;
        /* Fields are user, nice, system, idle, iowait, ... */
        if (field == 3 || field == 4) {
            idle += value;
        }
        field++;
    }
    if (field < 4) {
        return -1; /* not a /proc/stat we recognise */
    }
    *total = sum;
    *busy = sum - idle;
    return 0;
}

/* Busy percentage since the previous successful sample. Returns 0 on success;
 * -1 while no window has been measured yet (the first request after start). */
static int cpu_percent(struct metrics *m, double *out)
{
    unsigned long long total = 0;
    unsigned long long busy = 0;
    if (read_cpu_jiffies(m, &total, &busy) == 0) {
        if (!m->have_prev_cpu || total < m->prev_total) {
            /* First sample, or the counters restarted under us. Anything
             * computed from the old baseline would be fiction. */
            m->have_prev_cpu = 1;
            m->have_cpu_pct = 0;
            m->prev_total = total;
            m->prev_busy = busy;
        } else if (total - m->prev_total >= CPU_MIN_DELTA_JIFFIES) {
            unsigned long long delta_total = total - m->prev_total;
            unsigned long long delta_busy =
                busy >= m->prev_busy ? busy - m->prev_busy : 0;
            double pct = 100.0 * (double)delta_busy / (double)delta_total;
            if (pct < 0.0) {
                pct = 0.0;
            }
            if (pct > 100.0) {
                pct = 100.0;
            }
            m->cpu_pct = pct;
            m->have_cpu_pct = 1;
            m->prev_total = total;
            m->prev_busy = busy;
        }
        /* Otherwise the window was too short: keep the old baseline so the
         * next request measures against it rather than against a sliver. */
    }
    if (!m->have_cpu_pct) {
        return -1;
    }
    *out = m->cpu_pct;
    return 0;
}

static int read_load1(const struct metrics *m, double *out)
{
    char path[PATH_MAX_LEN];
    if (proc_path(m, "loadavg", path, sizeof(path)) != 0) {
        return -1;
    }
    double value = 0.0;
    if (read_double_file(path, &value) != 0) {
        return -1;
    }
    if (value < 0.0) {
        return -1;
    }
    *out = value;
    return 0;
}

static int read_uptime(const struct metrics *m, double *out)
{
    char path[PATH_MAX_LEN];
    if (proc_path(m, "uptime", path, sizeof(path)) != 0) {
        return -1;
    }
    double value = 0.0;
    if (read_double_file(path, &value) != 0) {
        return -1;
    }
    if (value < 0.0) {
        return -1;
    }
    *out = value;
    return 0;
}

/* Find "Key:  12345 kB" in a /proc/meminfo body. Returns 0 on success. */
static int meminfo_field(const char *body, const char *key, long *out)
{
    size_t key_len = strlen(key);
    const char *line = body;
    while (line != NULL && *line != '\0') {
        if (strncmp(line, key, key_len) == 0 && line[key_len] == ':') {
            const char *cursor = line + key_len + 1;
            char *end = NULL;
            errno = 0;
            long value = strtol(cursor, &end, 10);
            if (end == cursor || errno == ERANGE || value < 0) {
                return -1;
            }
            *out = value;
            return 0;
        }
        line = strchr(line, '\n');
        if (line != NULL) {
            line++;
        }
    }
    return -1;
}

/*
 * MemTotal and MemAvailable, in kB.
 *
 * MemAvailable is the kernel's own estimate of what a new allocation could
 * get, which is what "free memory" should mean; MemFree ignores reclaimable
 * cache and makes every healthy Linux box look nearly full. Kernels before
 * 3.14 have no MemAvailable, so fall back to MemFree there rather than
 * reporting nothing.
 */
static int read_meminfo(const struct metrics *m, long *total_kb, long *avail_kb)
{
    char path[PATH_MAX_LEN];
    char buf[1024];
    if (proc_path(m, "meminfo", path, sizeof(path)) != 0) {
        return -1;
    }
    if (read_small_file(path, buf, sizeof(buf)) != 0) {
        return -1;
    }
    if (meminfo_field(buf, "MemTotal", total_kb) != 0) {
        return -1;
    }
    if (meminfo_field(buf, "MemAvailable", avail_kb) != 0 &&
        meminfo_field(buf, "MemFree", avail_kb) != 0) {
        return -1;
    }
    return 0;
}

/* Free space on the root filesystem in kB, as seen by an unprivileged
 * writer (f_bavail, not f_bfree -- the reserved blocks are not available). */
static int read_root_free_kb(long *out)
{
    struct statvfs st;
    if (statvfs(ROOT_FS_PATH, &st) != 0) {
        return -1;
    }
    unsigned long unit = st.f_frsize != 0 ? st.f_frsize : st.f_bsize;
    if (unit == 0) {
        return -1;
    }
    double free_kb = ((double)st.f_bavail * (double)unit) / 1024.0;
    if (free_kb < 0.0) {
        return -1;
    }
    *out = (long)free_kb;
    return 0;
}

/*
 * Append " key=value" to `out` if it fits, and report whether it did.
 *
 * A metric is dropped whole rather than truncated: half a key=value pair on
 * the wire is a parse error at the other end, while a missing one is an
 * expected and handled condition.
 */
static void append_kv(char *out, size_t len, const char *fmt, ...)
{
    size_t used = strlen(out);
    if (used >= len) {
        return;
    }
    char scratch[64];
    va_list args;
    va_start(args, fmt);
    int written = vsnprintf(scratch, sizeof(scratch), fmt, args);
    va_end(args);
    if (written < 0 || (size_t)written >= sizeof(scratch)) {
        return;
    }
    if (used + (size_t)written + 1 > len - 1) {
        return; /* would not fit alongside the trailing newline */
    }
    memcpy(out + used, scratch, (size_t)written + 1);
}

/* Build the key=value tail of a STATUS response into `out` (which must
 * already hold the "RPT1 <temp>" prefix). */
static void append_metrics(char *out, size_t len, struct metrics *m)
{
    double cpu = 0.0;
    if (cpu_percent(m, &cpu) == 0) {
        append_kv(out, len, " cpu=%.1f", cpu);
    }
    double load1 = 0.0;
    if (read_load1(m, &load1) == 0) {
        append_kv(out, len, " load1=%.2f", load1);
    }
    long mem_total = 0;
    long mem_avail = 0;
    if (read_meminfo(m, &mem_total, &mem_avail) == 0) {
        append_kv(out, len, " memtotal=%ld", mem_total);
        append_kv(out, len, " memavail=%ld", mem_avail);
    }
    double uptime = 0.0;
    if (read_uptime(m, &uptime) == 0) {
        append_kv(out, len, " uptime=%.1f", uptime);
    }
    long root_free = 0;
    if (read_root_free_kb(&root_free) == 0) {
        append_kv(out, len, " rootfree=%ld", root_free);
    }
}

/* --- networking -------------------------------------------------------- */

static int send_all(int fd, const char *buf, size_t len)
{
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = send(fd, buf + sent, len - sent, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            return -1;
        }
        sent += (size_t)n;
    }
    return 0;
}

/*
 * Read one line (up to MAX_REQUEST bytes) from the client.
 * Returns the request length on success, -1 on error/EOF-without-data, and
 * -2 when the client exceeded MAX_REQUEST without sending a newline.
 */
static int recv_request(int fd, char *buf, size_t len)
{
    size_t used = 0;
    while (used < len - 1) {
        ssize_t n = recv(fd, buf + used, len - 1 - used, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1; /* timeout (EAGAIN) or hard error */
        }
        if (n == 0) {
            /* Peer closed. Treat a complete-but-unterminated request as
             * valid; an empty one as a bare disconnect. */
            break;
        }
        used += (size_t)n;
        buf[used] = '\0';
        if (memchr(buf, '\n', used) != NULL) {
            return (int)used;
        }
    }
    buf[used] = '\0';
    if (used == 0) {
        return -1;
    }
    if (memchr(buf, '\n', used) == NULL && used >= len - 1) {
        return -2; /* over-long request, no newline in sight */
    }
    return (int)used;
}

static void trim_line(char *buf)
{
    char *nl = strchr(buf, '\n');
    if (nl != NULL) {
        *nl = '\0';
    }
    size_t n = strlen(buf);
    while (n > 0 && (buf[n - 1] == '\r' || buf[n - 1] == ' ' ||
                     buf[n - 1] == '\t')) {
        buf[--n] = '\0';
    }
}

static void handle_client(int fd, struct xadc *x, const char *iio_root,
                          struct metrics *m)
{
    struct timeval tv;
    tv.tv_sec = CLIENT_TIMEOUT_S;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    char request[MAX_REQUEST];
    int len = recv_request(fd, request, sizeof(request));
    if (len == -1) {
        /* Silent or vanished client -- nothing to answer. */
        return;
    }
    char response[MAX_RESPONSE];
    if (len == -2) {
        snprintf(response, sizeof(response), "%s ERR COMMAND\n", RPT_PROTOCOL);
        send_all(fd, response, strlen(response));
        return;
    }

    trim_line(request);

    if (strcmp(request, "STATUS") == 0) {
        double temperature = 0.0;
        if (xadc_read_temperature(x, iio_root, &temperature) == 0) {
            snprintf(response, sizeof(response), "%s %.2f", RPT_PROTOCOL,
                     temperature);
            append_metrics(response, sizeof(response), m);
            double rail = 0.0;
            if (xadc_read_rail(x, &rail) == 0) {
                append_kv(response, sizeof(response), " " RAIL_KEY "=%.3f",
                          rail);
            }
            /* append_metrics never fills the buffer to the brim; it reserves
             * room for exactly this. */
            size_t used = strlen(response);
            response[used] = '\n';
            response[used + 1] = '\0';
        } else {
            snprintf(response, sizeof(response), "%s ERR XADC\n", RPT_PROTOCOL);
        }
    } else if (strcmp(request, "VERSION") == 0) {
        snprintf(response, sizeof(response), "%s VERSION %s\n", RPT_PROTOCOL,
                 RPT_VERSION);
    } else {
        snprintf(response, sizeof(response), "%s ERR COMMAND\n", RPT_PROTOCOL);
    }
    send_all(fd, response, strlen(response));
}

static int make_listener(int port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("rp-telemetry: socket");
        return -1;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((unsigned short)port);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("rp-telemetry: bind");
        close(fd);
        return -1;
    }
    if (listen(fd, 8) != 0) {
        perror("rp-telemetry: listen");
        close(fd);
        return -1;
    }
    return fd;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
            "usage: %s [--port N] [--iio-root DIR] [--proc-root DIR]\n"
            "\n"
            "  --port N        TCP port to listen on (default %d)\n"
            "  --iio-root DIR  IIO sysfs root (default %s)\n"
            "  --proc-root DIR procfs root for host metrics (default %s)\n"
            "  --version       print version and exit\n",
            argv0, DEFAULT_PORT, DEFAULT_IIO_ROOT, DEFAULT_PROC_ROOT);
}

int main(int argc, char **argv)
{
    int port = DEFAULT_PORT;
    const char *iio_root = DEFAULT_IIO_ROOT;
    const char *proc_root = DEFAULT_PROC_ROOT;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            char *end = NULL;
            long value = strtol(argv[++i], &end, 10);
            if (end == argv[i] || *end != '\0' || value <= 0 || value > 65535) {
                fprintf(stderr, "rp-telemetry: invalid port\n");
                return 2;
            }
            port = (int)value;
        } else if (strcmp(argv[i], "--iio-root") == 0 && i + 1 < argc) {
            iio_root = argv[++i];
        } else if (strcmp(argv[i], "--proc-root") == 0 && i + 1 < argc) {
            proc_root = argv[++i];
        } else if (strcmp(argv[i], "--version") == 0) {
            printf("%s\n", RPT_VERSION);
            return 0;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    /* A client that disappears mid-response must not kill the daemon. */
    signal(SIGPIPE, SIG_IGN);
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    struct xadc x;
    memset(&x, 0, sizeof(x));
    /* A caller that overrides the IIO root is a test harness or unusual
     * hardware, and takes responsibility for what it points us at. The default
     * root means a Red Pitaya, where reading the wrong peripheral resets the
     * board -- so there, nothing but the PS XADC is touched. */
    x.strict = (strcmp(iio_root, DEFAULT_IIO_ROOT) == 0);
    if (x.strict) {
        fprintf(stderr,
                "rp-telemetry: restricting discovery to the PS XADC ("
                PS_XADC_MARKER ")\n");
    }
    if (xadc_discover(&x, iio_root) != 0) {
        /* Not fatal: report once and keep serving, answering ERR XADC until
         * the sysfs entries appear. Discovery is retried per request only
         * while it is failing. */
        fprintf(stderr,
                "rp-telemetry: no in_temp0_raw found under %s; "
                "STATUS will report ERR XADC\n",
                iio_root);
    }

    struct metrics m;
    memset(&m, 0, sizeof(m));
    m.proc_root = proc_root;

    int listener = make_listener(port);
    if (listener < 0) {
        return 1;
    }
    /* The only routine startup message. After this the daemon is silent. */
    fprintf(stderr, "rp-telemetry %s listening on port %d\n", RPT_VERSION, port);
    fflush(stderr);

    while (!g_stop) {
        int client = accept(listener, NULL, NULL);
        if (client < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == ECONNABORTED) {
                /* Client vanished during the handshake; back to accept(). */
                continue;
            }
            if (errno == EMFILE || errno == ENFILE || errno == ENOBUFS ||
                errno == ENOMEM) {
                /* Resource exhaustion. Retrying immediately would spin the
                 * CPU, which is exactly what this daemon must never do --
                 * back off before returning to accept(). */
                sleep(1);
                continue;
            }
            /* Unexpected and unrecoverable. Exit non-zero so systemd's
             * Restart=on-failure brings the daemon back -- returning 0 here
             * would look like a clean shutdown and leave the unit dead, with
             * the board silently reporting no temperature until someone
             * noticed. */
            perror("rp-telemetry: accept");
            close(listener);
            return 1;
        }
        handle_client(client, &x, iio_root, &m);
        close(client);
    }

    close(listener);
    return 0;
}
