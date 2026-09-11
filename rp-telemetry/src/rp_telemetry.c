/*
 * rp-telemetry -- minimal Zynq die-temperature telemetry daemon for the
 * Red Pitaya STEMlab 125-14 (Gen 1).
 *
 * Design goal: as close to zero CPU as a network service can get. The process
 * spends its entire life blocked in accept(). There is no polling loop, no
 * timer, no thread, no HTTP, no JSON, and no periodic logging. A request costs
 * one accept(), one recv(), one open/read/close of a single sysfs file, one
 * send(), and one close().
 *
 * Protocol (line oriented, one request per connection):
 *
 *     ->  "STATUS\n"     <-  "RPT1 57.34\n"       (degrees Celsius)
 *                        <-  "RPT1 ERR XADC\n"    (sysfs read failed)
 *     ->  "VERSION\n"    <-  "RPT1 VERSION 1.0.0\n"
 *     ->  anything else  <-  "RPT1 ERR COMMAND\n"
 *
 * Temperature source: the Zynq XADC exposed through Linux IIO. The device
 * directory is discovered once at startup by scanning /sys/bus/iio/devices for
 * the entry that provides `in_temp0_raw`; `in_temp0_offset` and
 * `in_temp0_scale` are read once at the same time (they are constants of the
 * XADC transfer function). Only `in_temp0_raw` is re-read per request.
 *
 *     temperature_c = (raw + offset) * scale / 1000.0
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
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#define RPT_VERSION "1.0.0"
#define RPT_PROTOCOL "RPT1"

#define DEFAULT_PORT 18864
#define DEFAULT_IIO_ROOT "/sys/bus/iio/devices"

/* A request is at most "VERSION\r\n". Anything longer is malformed by
 * definition, so the read buffer is a hard cap on what one client can make us
 * buffer. */
#define MAX_REQUEST 64
/* Receive/send timeout on an accepted socket. A client that connects and then
 * says nothing is dropped after this long instead of pinning the daemon. */
#define CLIENT_TIMEOUT_S 2

#define PATH_MAX_LEN 512

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
};

static int path_exists(const char *path)
{
    return access(path, R_OK) == 0;
}

/*
 * Locate the IIO device exposing in_temp0_raw and cache the raw path plus the
 * (constant) offset and scale. Called once at startup; retried lazily only
 * after a failed read, never on the happy path.
 */
static int xadc_discover(struct xadc *x, const char *iio_root)
{
    DIR *dir = opendir(iio_root);
    if (dir == NULL) {
        return -1;
    }
    int found = -1;
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
        break;
    }
    closedir(dir);
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

static void handle_client(int fd, struct xadc *x, const char *iio_root)
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
    char response[64];
    if (len == -2) {
        snprintf(response, sizeof(response), "%s ERR COMMAND\n", RPT_PROTOCOL);
        send_all(fd, response, strlen(response));
        return;
    }

    trim_line(request);

    if (strcmp(request, "STATUS") == 0) {
        double temperature = 0.0;
        if (xadc_read_temperature(x, iio_root, &temperature) == 0) {
            snprintf(response, sizeof(response), "%s %.2f\n", RPT_PROTOCOL,
                     temperature);
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
            "usage: %s [--port N] [--iio-root DIR]\n"
            "\n"
            "  --port N        TCP port to listen on (default %d)\n"
            "  --iio-root DIR  IIO sysfs root (default %s)\n"
            "  --version       print version and exit\n",
            argv0, DEFAULT_PORT, DEFAULT_IIO_ROOT);
}

int main(int argc, char **argv)
{
    int port = DEFAULT_PORT;
    const char *iio_root = DEFAULT_IIO_ROOT;

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
    if (xadc_discover(&x, iio_root) != 0) {
        /* Not fatal: report once and keep serving, answering ERR XADC until
         * the sysfs entries appear. Discovery is retried per request only
         * while it is failing. */
        fprintf(stderr,
                "rp-telemetry: no in_temp0_raw found under %s; "
                "STATUS will report ERR XADC\n",
                iio_root);
    }

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
        handle_client(client, &x, iio_root);
        close(client);
    }

    close(listener);
    return 0;
}
