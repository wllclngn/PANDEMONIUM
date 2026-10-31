// vmm-init.c -- PID 1 of every vm-matrix guest.
//
// Static: the guest has no shell and no loader beyond the libraries bundled for
// veristat, montauk and the scheduler. One fixed sequence per boot,
// parameterised by the vmm.* kernel cmdline keys and /etc/vmm/sched.conf:
// verify the scheduler's BPF objects, attach it, drive the load phases under
// it, detach it, ship every result to the host and power off.
//
// Results leave on ttyS1 (raw mode), byte exact:
//   @@KV <key>=<value>\n
//   @@FILE <name> <nbytes> <full|tail>\n<nbytes raw bytes>\n
//   @@END\n
// Each phase's records leave as it completes, so a guest that hangs later has
// already delivered everything before the hang.
//
// As PID 1 it inherits every orphan: one reaper collects all children and keeps
// the exit status of the ones being waited on. Every wait is bounded.

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define RESULTS_TTY   "/dev/ttyS1"
#define RESULTS_PORT  "vmm.results"
// A virtio port write is queued, not waited on (port_fops_write forces
// nonblock), and no tty ioctl reports its queue; the host has the bytes once
// qemu services the kick, well inside this.
#define VIRTIO_SETTLE_MS    1000
#define STREAM_TTY    "/dev/ttyS2"
#define SCHED_CONF    "/etc/vmm/sched.conf"
#define MODULES_ORDER "/etc/vmm/modules.order"
#define MODULES_DIR   "/lib/modules/vmm"
#define VERISTAT      "/bin/veristat"
#define MONTAUK       "/bin/montauk"
#define SCRATCH       "/scratch"
#define WORK          "/tmp/vmm"
#define EVENTS        "/tmp/events.bin"
#define PROM_DIR      "/tmp/montauk-prom"
#define CAP_PROM       (32 * MIB)
#define CAP_ANALYZE    (16 * MIB)
#define ANALYZE_MS     600000
#define SCX           "/sys/kernel/sched_ext"

#define MIB (1024L * 1024L)
#define CAP_VERISTAT_V (16 * MIB)
#define CAP_SCHED_LOG  (8 * MIB)
#define CAP_PHASE_LOG  (1 * MIB)
#define CAP_KMSG       (8 * MIB)
#define CAP_OTHER      (16 * MIB)
#define CAP_EVENTS     (64 * MIB)
#define KMSG_KEEP      (64 * MIB)

#define POLL_MS             100
#define ATTACH_MS           30000
#define DETACH_EXIT_MS      15000
#define DETACH_STATE_MS     10000
#define WORKLOAD_GRACE_MS   15000
#define VERISTAT_MS         600000
#define MONTAUK_READY_MS    15000
#define MONTAUK_STOP_MS     30000
#define KILL_REAP_MS        5000
#define VDA_WAIT_MS         5000
#define DRAIN_MS            30000
// The host reaps a guest that is silent on every port for stall_s (at least
// twice this), so every long wait says it is still alive on the console.
#define HEARTBEAT_MS        30000

// montauk logs this once its BPF programs are loaded and attached.
#define MONTAUK_ATTACHED    "eBPF trace attached"

#define MAX_ARGS    64
#define MAX_OBJS    32
#define MAX_CHILDREN 128

enum { P_VERISTAT, P_ATTACH, P_STRAND, P_IOFLOOD, P_IDLEPING, P_LOCKBLOCK,
       P_AFFINITY, P_MIXED, P_DETACH, P_COUNT };
static const char *const PHASE[P_COUNT] = {
    "veristat", "attach", "strand", "ioflood", "idleping", "lockblock",
    "affinity", "mixed", "detach"};

// One per load phase from strand to affinity; mixed runs every one at once.
#define NWORK 5
static const char *const WORKLOAD[NWORK] = {
    "/bin/pand-strand-load", "/bin/pand-io-flood", "/bin/pand-idle-ping",
    "/bin/pand-lock-block", "/bin/pand-affinity-churn"};

// The drivers the init needs before it can mount the scratch disk.
static const char *const SCRATCH_MODULES[] = {"virtio_pci", "virtio_blk", "ext4"};

struct child {
    pid_t pid;
    int done;
    int status;
};

static struct child g_child[MAX_CHILDREN];
static int g_nchild;

static int g_res = -1;
static int g_res_virtio;
static char g_res_path[300] = RESULTS_TTY;
static int g_kmsg = -1;
static long g_kmsg_bytes;
static char g_cmdline[8192];
static char g_cell[256] = "unknown";
static char g_trace[256] = "pand-";
static int g_stream;
static long g_duration = 20;
static int g_want[P_COUNT];
static int g_ncpu = 1;

static struct {
    char kind[16];
    char label[256];
    char binary[512];
    char ops[128];
    char *argv[MAX_ARGS + 2];
    int nargs;
    char *obj[MAX_OBJS];
    int nobj;
} g_sched;

static struct child *g_schedc;
static int g_attached;
static int g_sched_finished;
static struct child *g_montauk;
static int g_montauk_tried;

static long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void sleep_ms(long ms) {
    struct timespec ts = {ms / 1000, (ms % 1000) * 1000000L};
    while (nanosleep(&ts, &ts) != 0 && errno == EINTR)
        ;
}

static int exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0;
}

static int is_none(void) { return strcmp(g_sched.kind, "none") == 0; }

// Short writes and EINTR loop; a descriptor that makes no progress for ~30 s
// is given up on rather than waited on forever.
static int write_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    int stalls = 0;
    while (n) {
        ssize_t w = write(fd, p, n);
        if (w > 0) {
            p += w;
            n -= (size_t)w;
            stalls = 0;
            continue;
        }
        if (w < 0 && errno == EINTR)
            continue;
        if (w < 0 && errno != EAGAIN)
            return -1;
        if (++stalls > 30)
            return -1;
        struct pollfd pf = {.fd = fd, .events = POLLOUT};
        poll(&pf, 1, 1000);
    }
    return 0;
}

__attribute__((format(printf, 1, 2)))
static void say(const char *fmt, ...) {
    char buf[1536];
    int n = snprintf(buf, sizeof(buf), "[vmm-init] ");
    va_list ap;
    va_start(ap, fmt);
    int m = vsnprintf(buf + n, sizeof(buf) - (size_t)n - 1, fmt, ap);
    va_end(ap);
    size_t len = (size_t)n + (size_t)(m > 0 ? m : 0);
    if (len > sizeof(buf) - 2)
        len = sizeof(buf) - 2;
    buf[len++] = '\n';
    write_all(1, buf, len);
}

static void res_write(const void *buf, size_t n) {
    if (g_res < 0)
        return;
    if (write_all(g_res, buf, n) != 0) {
        say("results channel %s failed (%s); later records are lost", g_res_path,
            strerror(errno));
        close(g_res);
        g_res = -1;
    }
}

__attribute__((format(printf, 2, 3)))
static void kv(const char *key, const char *fmt, ...) {
    char val[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(val, sizeof(val), fmt, ap);
    va_end(ap);
    for (char *q = val; *q; q++)
        if (*q == '\n' || *q == '\r')
            *q = ' ';
    char line[2560];
    int n = snprintf(line, sizeof(line), "@@KV %s=%s\n", key, val);
    if (n < 0)
        return;
    if ((size_t)n >= sizeof(line)) {
        n = (int)sizeof(line) - 1;
        line[n - 1] = '\n';
    }
    res_write(line, (size_t)n);
}

// Ships a file, keeping its tail when it is over cap. The byte count in the
// header is the one sent: a file that shrinks underneath is zero-padded so the
// framing stays exact.
static void send_file(const char *name, const char *path, long cap) {
    if (g_res < 0)
        return;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    struct stat st;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(fd);
        return;
    }
    long long size = st.st_size, off = 0, n = size;
    const char *flag = "full";
    if (size > cap) {
        char k[192];
        snprintf(k, sizeof(k), "file.%s.size", name);
        kv(k, "%lld", size);
        off = size - cap;
        n = cap;
        flag = "tail";
    }
    char hdr[256];
    int h = snprintf(hdr, sizeof(hdr), "@@FILE %s %lld %s\n", name, n, flag);
    res_write(hdr, (size_t)h);
    static char buf[1 << 16];
    long long left = n;
    while (left > 0 && g_res >= 0) {
        size_t want = left < (long long)sizeof(buf) ? (size_t)left : sizeof(buf);
        ssize_t r = pread(fd, buf, want, off);
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0) {
            memset(buf, 0, want);
            r = (ssize_t)want;
        }
        res_write(buf, (size_t)r);
        off += r;
        left -= r;
    }
    res_write("\n", 1);
    close(fd);
    say("sent %s (%lld bytes, %s)", name, n, flag);
}

// Waits for queued output (tty buffer and UART shift register) to leave
// before power-off cuts it, bounded.
static void drain(int fd) {
    if (fd < 0)
        return;
    long end = now_ms() + DRAIN_MS;
    while (now_ms() < end) {
        int queued = 0;
        if (ioctl(fd, TIOCOUTQ, &queued) != 0)
            return;
        unsigned int lsr = 0;
        int empty = ioctl(fd, TIOCSERGETLSR, &lsr) != 0 || (lsr & TIOCSER_TEMT);
        if (queued == 0 && empty)
            return;
        sleep_ms(10);
    }
}

static void read_line(const char *path, char *buf, size_t n, const char *dflt) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        snprintf(buf, n, "%s", dflt);
        return;
    }
    ssize_t r;
    do
        r = read(fd, buf, n - 1);
    while (r < 0 && errno == EINTR);
    close(fd);
    buf[r > 0 ? r : 0] = '\0';
    buf[strcspn(buf, "\n")] = '\0';
    size_t len = strlen(buf);
    while (len && (buf[len - 1] == ' ' || buf[len - 1] == '\t' || buf[len - 1] == '\r'))
        buf[--len] = '\0';
}

// Reads a whole small file; NULL when unreadable. Caller frees.
static char *slurp(const char *path, size_t cap, size_t *len_out) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return NULL;
    char *buf = malloc(cap + 1);
    size_t len = 0;
    while (buf && len < cap) {
        ssize_t r = read(fd, buf + len, cap - len);
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            break;
        len += (size_t)r;
    }
    close(fd);
    if (buf)
        buf[len] = '\0';
    if (len_out)
        *len_out = len;
    return buf;
}

static void write_str(const char *path, const char *s) {
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    write_all(fd, s, strlen(s));
    close(fd);
}

static void sched_state(char *buf, size_t n) { read_line(SCX "/state", buf, n, "absent"); }
static void sched_ops(char *buf, size_t n) { read_line(SCX "/root/ops", buf, n, ""); }

// Bumped by the kernel on every enable; -1 on a kernel without it.
static long long sched_enable_seq(void) {
    char buf[32];
    read_line(SCX "/enable_seq", buf, sizeof(buf), "");
    return buf[0] ? strtoll(buf, NULL, 10) : -1;
}

static void reap(void) {
    int st;
    pid_t p;
    while ((p = waitpid(-1, &st, WNOHANG)) > 0) {
        for (int i = 0; i < g_nchild; i++) {
            if (g_child[i].pid == p && !g_child[i].done) {
                g_child[i].done = 1;
                g_child[i].status = st;
                break;
            }
        }
    }
}

static int wait_child(struct child *c, long ms) {
    long end = now_ms() + ms;
    for (;;) {
        reap();
        if (c->done)
            return 1;
        if (now_ms() >= end)
            return 0;
        sleep_ms(20);
    }
}

// wait_child, with a console line every HEARTBEAT_MS naming what is running.
static int wait_child_said(struct child *c, long ms, const char *what) {
    long t0 = now_ms(), end = t0 + ms;
    for (;;) {
        long left = end - now_ms();
        if (wait_child(c, left < HEARTBEAT_MS ? (left > 0 ? left : 0) : HEARTBEAT_MS))
            return 1;
        if (now_ms() >= end)
            return 0;
        say("%s still running, %ld s", what, (now_ms() - t0) / 1000);
    }
}

// A reaped pid may already belong to someone else, so the pid itself is only
// signalled while unreaped (a zombie still holds it); the group outlives its
// leader for as long as any member does.
static void kill_group(struct child *c) {
    kill(-c->pid, SIGKILL);
    if (!c->done)
        kill(c->pid, SIGKILL);
}

// Sends sig, then SIGKILL to the whole group after grace_ms. Returns 1 when the
// child left within the grace period.
static int stop_child(struct child *c, int sig, long grace_ms) {
    reap();
    if (c->done)
        return 1;
    kill(c->pid, sig);
    if (wait_child(c, grace_ms))
        return 1;
    kill_group(c);
    wait_child(c, KILL_REAP_MS);
    return 0;
}

static void status_str(const struct child *c, char *buf, size_t n) {
    if (!c->done)
        snprintf(buf, n, "running");
    else if (WIFEXITED(c->status))
        snprintf(buf, n, "%d", WEXITSTATUS(c->status));
    else if (WIFSIGNALED(c->status))
        snprintf(buf, n, "sig%d", WTERMSIG(c->status));
    else
        snprintf(buf, n, "status%d", c->status);
}

static int exited_ok(const struct child *c) {
    return c->done && WIFEXITED(c->status) && WEXITSTATUS(c->status) == 0;
}

// fork + exec in its own process group, stdin from /dev/null. A CLOEXEC pipe
// carries execv's errno back, so a missing binary is told apart from one that
// ran and exited 127. NULL on failure with *err set.
static struct child *spawn(char *const argv[], int out_fd, int err_fd, int *err) {
    *err = 0;
    if (g_nchild >= MAX_CHILDREN) {
        *err = EAGAIN;
        return NULL;
    }
    int pfd[2];
    if (pipe2(pfd, O_CLOEXEC) != 0) {
        *err = errno;
        return NULL;
    }
    pid_t pid = fork();
    if (pid < 0) {
        *err = errno;
        close(pfd[0]);
        close(pfd[1]);
        return NULL;
    }
    if (pid == 0) {
        sigset_t none;
        sigemptyset(&none);
        sigprocmask(SIG_SETMASK, &none, NULL);
        setpgid(0, 0);
        int nul = open("/dev/null", O_RDWR);
        if (nul >= 0)
            dup2(nul, 0);
        dup2(out_fd >= 0 ? out_fd : nul, 1);
        dup2(err_fd >= 0 ? err_fd : nul, 2);
        if (nul > 2)
            close(nul);
        execv(argv[0], argv);
        int e = errno;
        if (write(pfd[1], &e, sizeof(e)) < 0)
            _exit(127);
        _exit(127);
    }
    setpgid(pid, pid);
    close(pfd[1]);
    int e = 0;
    ssize_t r;
    do
        r = read(pfd[0], &e, sizeof(e));
    while (r < 0 && errno == EINTR);
    close(pfd[0]);
    struct child *c = &g_child[g_nchild++];
    c->pid = pid;
    c->done = 0;
    c->status = 0;
    if (r == (ssize_t)sizeof(e)) {
        *err = e;
        wait_child(c, KILL_REAP_MS);
        return NULL;
    }
    return c;
}

static int open_log(const char *path) {
    return open(path, O_WRONLY | O_CREAT | O_TRUNC | O_APPEND | O_CLOEXEC, 0644);
}

__attribute__((format(printf, 4, 5)))
static void phase_end(int p, const char *result, long t0, const char *fmt, ...) {
    char detail[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(detail, sizeof(detail), fmt, ap);
    va_end(ap);
    long ms = now_ms() - t0;
    char k[64];
    snprintf(k, sizeof(k), "phase.%s.result", PHASE[p]);
    kv(k, "%s", result);
    snprintf(k, sizeof(k), "phase.%s.detail", PHASE[p]);
    kv(k, "%s", detail);
    snprintf(k, sizeof(k), "phase.%s.ms", PHASE[p]);
    kv(k, "%ld", ms);
    say("phase %s: %s (%s) %ld ms", PHASE[p], result, detail, ms);
}

// Every record since the last drain, dmesg style, appended to kmsg.txt. Called
// between phases so a ring that wraps during a long run still loses nothing
// the drains reached first.
static void kmsg_drain(void) {
    if (g_kmsg < 0)
        return;
    int out = open(WORK "/kmsg.txt", O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (out < 0)
        return;
    static char rec[16384];
    static char line[16448];
    for (int i = 0; i < 1000000; i++) {
        ssize_t r = read(g_kmsg, rec, sizeof(rec) - 1);
        if (r < 0 && errno == EINTR)
            continue;
        if (r < 0 && errno == EPIPE) {
            static const char lost[] = "[kmsg: records overwritten before they were read]\n";
            write_all(out, lost, sizeof(lost) - 1);
            continue;
        }
        if (r <= 0)
            break;
        rec[r] = '\0';
        unsigned long long usec = 0;
        sscanf(rec, "%*u,%*u,%llu", &usec);
        char *msg = strchr(rec, ';');
        msg = msg ? msg + 1 : rec;
        msg[strcspn(msg, "\n")] = '\0';
        int n = snprintf(line, sizeof(line), "[%5llu.%06llu] %s\n", usec / 1000000ULL,
                         usec % 1000000ULL, msg);
        if (n <= 0)
            continue;
        if ((size_t)n >= sizeof(line))
            n = (int)sizeof(line) - 1;
        if (g_kmsg_bytes + n > KMSG_KEEP)
            continue;
        g_kmsg_bytes += n;
        write_all(out, line, (size_t)n);
    }
    close(out);
}

// Every readable file under dir as `path: line`, one output line per content
// line, entries in name order.
static void dump_tree(int out, const char *dir, int depth) {
    DIR *d = opendir(dir);
    if (!d)
        return;
    char *names[256];
    int n = 0;
    struct dirent *e;
    while (n < 256 && (e = readdir(d))) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
            continue;
        names[n] = strdup(e->d_name);
        if (names[n])
            n++;
    }
    closedir(d);
    for (int i = 1; i < n; i++)
        for (int j = i; j > 0 && strcmp(names[j - 1], names[j]) > 0; j--) {
            char *t = names[j];
            names[j] = names[j - 1];
            names[j - 1] = t;
        }
    for (int i = 0; i < n; i++) {
        char path[1024];
        snprintf(path, sizeof(path), "%s/%s", dir, names[i]);
        free(names[i]);
        struct stat st;
        if (lstat(path, &st) != 0)
            continue;
        if (S_ISDIR(st.st_mode)) {
            if (depth < 6)
                dump_tree(out, path, depth + 1);
            continue;
        }
        if (!S_ISREG(st.st_mode))
            continue;
        char *body = slurp(path, 65536, NULL);
        if (!body)
            continue;
        char *save = NULL;
        char *ln = strtok_r(body, "\n", &save);
        if (!ln)
            dprintf(out, "%s: \n", path);
        for (; ln; ln = strtok_r(NULL, "\n", &save))
            dprintf(out, "%s: %s\n", path, ln);
        free(body);
    }
}

static void mount_fs(const char *src, const char *dst, const char *type, const char *opts) {
    mkdir(dst, 0755);
    if (mount(src, dst, type, 0, opts) != 0 && errno != EBUSY)
        say("mount %s on %s failed: %s", type, dst, strerror(errno));
}

static void setup_fs(void) {
    mount_fs("proc", "/proc", "proc", NULL);
    mount_fs("sysfs", "/sys", "sysfs", NULL);
    mount_fs("devtmpfs", "/dev", "devtmpfs", NULL);
    mount_fs("tmpfs", "/dev/shm", "tmpfs", NULL);
    mount_fs("tmpfs", "/tmp", "tmpfs", NULL);
    mount_fs("bpf", "/sys/fs/bpf", "bpf", NULL);
    // tracefs and debugfs: libbpf resolves tracepoint ids through them, and an
    // attach that cannot is misreported as a permissions error.
    mount_fs("tracefs", "/sys/kernel/tracing", "tracefs", NULL);
    mount_fs("debugfs", "/sys/kernel/debug", "debugfs", NULL);
    // cgroup2 with the memory controller: veristat measures each program's
    // verifier memory peak through it, and cgroup-aware schedulers expect it.
    mount_fs("cgroup2", "/sys/fs/cgroup", "cgroup2", NULL);
    write_str("/sys/fs/cgroup/cgroup.subtree_control", "+memory\n");
    write_str("/proc/sys/kernel/perf_event_paranoid", "-1\n");
    mkdir(WORK, 0755);
    mkdir(SCRATCH, 0755);
}

static void cmdline_get(const char *key, char *dst, size_t n) {
    char needle[64];
    int k = snprintf(needle, sizeof(needle), "vmm.%s=", key);
    const char *p = g_cmdline;
    while (*p) {
        while (*p == ' ' || *p == '\n' || *p == '\t')
            p++;
        const char *tok = p;
        while (*p && *p != ' ' && *p != '\n' && *p != '\t')
            p++;
        size_t len = (size_t)(p - tok);
        if (len >= (size_t)k && strncmp(tok, needle, (size_t)k) == 0) {
            size_t vlen = len - (size_t)k;
            if (vlen > n - 1)
                vlen = n - 1;
            memcpy(dst, tok + k, vlen);
            dst[vlen] = '\0';
        }
    }
}

static void parse_cmdline(void) {
    int fd = open("/proc/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        ssize_t r = read(fd, g_cmdline, sizeof(g_cmdline) - 1);
        g_cmdline[r > 0 ? r : 0] = '\0';
        close(fd);
    }
    cmdline_get("cell", g_cell, sizeof(g_cell));
    cmdline_get("trace", g_trace, sizeof(g_trace));
    char stream[8] = "";
    cmdline_get("stream", stream, sizeof(stream));
    g_stream = !strcmp(stream, "1");
    char dur[32] = "";
    cmdline_get("duration", dur, sizeof(dur));
    if (dur[0]) {
        long d = strtol(dur, NULL, 10);
        if (d >= 1 && d <= 86400)
            g_duration = d;
    }
    char phases[512] = "";
    cmdline_get("phases", phases, sizeof(phases));
    if (!phases[0]) {
        for (int p = 0; p < P_COUNT; p++)
            g_want[p] = 1;
        return;
    }
    char *save = NULL;
    for (char *t = strtok_r(phases, ",", &save); t; t = strtok_r(NULL, ",", &save)) {
        int found = 0;
        for (int p = 0; p < P_COUNT; p++)
            if (!strcmp(t, PHASE[p]))
                g_want[p] = found = 1;
        if (!found)
            say("unknown phase %s in vmm.phases, ignored", t);
    }
}

static void read_sched_conf(void) {
    snprintf(g_sched.kind, sizeof(g_sched.kind), "none");
    char *body = slurp(SCHED_CONF, 65536, NULL);
    if (!body) {
        say("WARN: %s missing; running with the kernel's own scheduler", SCHED_CONF);
        return;
    }
    char *save = NULL;
    for (char *ln = strtok_r(body, "\n", &save); ln; ln = strtok_r(NULL, "\n", &save)) {
        char *eq = strchr(ln, '=');
        if (!eq)
            continue;
        *eq = '\0';
        const char *k = ln, *v = eq + 1;
        if (!strcmp(k, "kind"))
            snprintf(g_sched.kind, sizeof(g_sched.kind), "%s", v);
        else if (!strcmp(k, "label"))
            snprintf(g_sched.label, sizeof(g_sched.label), "%s", v);
        else if (!strcmp(k, "binary"))
            snprintf(g_sched.binary, sizeof(g_sched.binary), "%s", v);
        else if (!strcmp(k, "ops"))
            snprintf(g_sched.ops, sizeof(g_sched.ops), "%s", v);
        else if (!strcmp(k, "arg") && g_sched.nargs < MAX_ARGS)
            g_sched.argv[1 + g_sched.nargs++] = strdup(v);
        else if (!strcmp(k, "object") && g_sched.nobj < MAX_OBJS)
            g_sched.obj[g_sched.nobj++] = strdup(v);
    }
    free(body);
    g_sched.argv[0] = g_sched.binary;
    g_sched.argv[1 + g_sched.nargs] = NULL;
}

// The virtio-serial port vmrun names vmm.results, found by its sysfs name.
// Ports appear once the host's PORT_ADD control message lands, so a present
// virtio-ports class is waited on briefly; an absent one means the kernel has
// no virtio_console at boot.
static int open_virtio_results(void) {
    for (int tries = 0; tries < 30; tries++) {
        DIR *d = opendir("/sys/class/virtio-ports");
        if (!d) {
            if (tries >= 2)
                return -1;
            sleep_ms(100);
            continue;
        }
        struct dirent *e;
        while ((e = readdir(d))) {
            if (e->d_name[0] == '.')
                continue;
            char p[320], name[64];
            snprintf(p, sizeof(p), "/sys/class/virtio-ports/%s/name", e->d_name);
            read_line(p, name, sizeof(name), "");
            if (strcmp(name, RESULTS_PORT))
                continue;
            snprintf(g_res_path, sizeof(g_res_path), "/dev/%s", e->d_name);
            closedir(d);
            int fd = open(g_res_path, O_WRONLY | O_CLOEXEC);
            if (fd < 0)
                say("cannot open %s (%s); falling back to %s", g_res_path, strerror(errno),
                    RESULTS_TTY);
            return fd;
        }
        closedir(d);
        sleep_ms(100);
    }
    return -1;
}

static void open_results(void) {
    g_res = open_virtio_results();
    if (g_res >= 0) {
        g_res_virtio = 1;
        say("results over virtio port %s (%s)", RESULTS_PORT, g_res_path);
        return;
    }
    snprintf(g_res_path, sizeof(g_res_path), "%s", RESULTS_TTY);
    g_res = open(RESULTS_TTY, O_RDWR | O_NOCTTY | O_CLOEXEC);
    if (g_res < 0) {
        say("cannot open %s (%s); results are lost", RESULTS_TTY, strerror(errno));
        return;
    }
    struct termios t;
    if (tcgetattr(g_res, &t) != 0) {
        say("tcgetattr %s: %s", RESULTS_TTY, strerror(errno));
        return;
    }
    // Raw: no CR insertion before LF, no flow-control bytes eaten from the data.
    cfmakeraw(&t);
    t.c_iflag &= ~(tcflag_t)(IXOFF | IXANY);
    t.c_cflag &= ~(tcflag_t)CRTSCTS;
    t.c_cflag |= CLOCAL | CREAD;
    cfsetispeed(&t, B115200);
    cfsetospeed(&t, B115200);
    if (tcsetattr(g_res, TCSANOW, &t) != 0)
        say("tcsetattr %s: %s", RESULTS_TTY, strerror(errno));
}

static int builtin_probe(const char *name) {
    if (!strcmp(name, "ext4")) {
        char *fs = slurp("/proc/filesystems", 65536, NULL);
        int hit = fs && strstr(fs, "\text4\n");
        free(fs);
        return hit;
    }
    if (!strcmp(name, "virtio_pci"))
        return exists("/sys/bus/pci/drivers/virtio-pci");
    if (!strcmp(name, "virtio_blk"))
        return exists("/sys/bus/virtio/drivers/virtio_blk");
    char p[256];
    snprintf(p, sizeof(p), "/sys/module/%s", name);
    return exists(p);
}

static void load_modules(void) {
    char seen[64][64];
    int nseen = 0;
    char *order = slurp(MODULES_ORDER, 65536, NULL);
    char *save = NULL;
    for (char *ln = order ? strtok_r(order, "\n", &save) : NULL; ln;
         ln = strtok_r(NULL, "\n", &save)) {
        ln[strcspn(ln, " \t\r")] = '\0';
        if (!ln[0])
            continue;
        char name[64];
        snprintf(name, sizeof(name), "%.*s", (int)strcspn(ln, "."), ln);
        char path[512], key[96];
        snprintf(path, sizeof(path), "%s/%s", MODULES_DIR, ln);
        snprintf(key, sizeof(key), "module.%s", name);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) {
            kv(key, "failed:%d", errno);
            say("module %s: open failed: %s", name, strerror(errno));
        } else {
            long rc = syscall(SYS_finit_module, fd, "", 0);
            int e = errno;
            close(fd);
            if (rc == 0 || e == EEXIST) {
                kv(key, "loaded");
                say("module %s: loaded", name);
            } else {
                kv(key, "failed:%d", e);
                say("module %s: finit_module failed: %s", name, strerror(e));
            }
        }
        if (nseen < 64)
            snprintf(seen[nseen++], sizeof(seen[0]), "%s", name);
    }
    free(order);
    for (size_t i = 0; i < sizeof(SCRATCH_MODULES) / sizeof(SCRATCH_MODULES[0]); i++) {
        const char *m = SCRATCH_MODULES[i];
        int done = 0;
        for (int j = 0; j < nseen; j++)
            done |= !strcmp(seen[j], m);
        if (done)
            continue;
        char key[96];
        snprintf(key, sizeof(key), "module.%s", m);
        kv(key, "%s", builtin_probe(m) ? "builtin" : "missing");
    }
}

static void mount_scratch(void) {
    long end = now_ms() + VDA_WAIT_MS;
    while (!exists("/dev/vda") && now_ms() < end)
        sleep_ms(POLL_MS);
    if (mount("/dev/vda", SCRATCH, "ext4", 0, NULL) == 0) {
        kv("scratch", "ext4");
        say("scratch: ext4 on /dev/vda");
        return;
    }
    int e = errno;
    if (mount("tmpfs", SCRATCH, "tmpfs", 0, NULL) != 0)
        say("scratch: tmpfs mount failed too (%s); /scratch stays on rootfs", strerror(errno));
    kv("scratch", "tmpfs");
    say("scratch: tmpfs (/dev/vda as ext4: %s)", strerror(e));
}

static void say_identity(void) {
    struct utsname u;
    if (uname(&u) == 0) {
        kv("uname.release", "%s", u.release);
        kv("uname.version", "%s", u.version);
        say("kernel %s %s", u.release, u.version);
    }
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    g_ncpu = n > 0 ? (int)n : 1;
    kv("nr_cpus", "%d", g_ncpu);
    kv("btf", "%s", exists("/sys/kernel/btf/vmlinux") ? "yes" : "no");
    kv("sched_ext", "%s", exists(SCX "/state") ? "yes" : "no");
}

// Pulls every failing program out of veristat's csv into a filter file of
// file/prog globs. Returns the number of rows read; 0 when the csv is unusable.
static int parse_veristat_csv(int *nfail, char *names, size_t nn) {
    *nfail = 0;
    names[0] = '\0';
    char *csv = slurp(WORK "/veristat.csv", 8 * MIB, NULL);
    if (!csv)
        return 0;
    int col_file = -1, col_prog = -1, col_verdict = -1, rows = 0, full = 0;
    int ffd = open(WORK "/veristat.filter", O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    char *save = NULL;
    for (char *ln = strtok_r(csv, "\n", &save); ln; ln = strtok_r(NULL, "\n", &save)) {
        char *f[32];
        int nf = 0;
        for (char *p = ln; nf < 32;) {
            f[nf++] = p;
            p = strchr(p, ',');
            if (!p)
                break;
            *p++ = '\0';
        }
        if (col_verdict < 0) {
            for (int i = 0; i < nf; i++) {
                if (!strcmp(f[i], "file_name"))
                    col_file = i;
                else if (!strcmp(f[i], "prog_name"))
                    col_prog = i;
                else if (!strcmp(f[i], "verdict"))
                    col_verdict = i;
            }
            if (col_file < 0 || col_prog < 0 || col_verdict < 0)
                break;
            continue;
        }
        if (nf <= col_file || nf <= col_prog || nf <= col_verdict)
            continue;
        rows++;
        if (strcmp(f[col_verdict], "failure") != 0)
            continue;
        (*nfail)++;
        if (ffd >= 0)
            dprintf(ffd, "%s/%s\n", f[col_file], f[col_prog]);
        size_t used = strlen(names);
        if (full)
            continue;
        if (used + strlen(f[col_prog]) + 6 < nn) {
            snprintf(names + used, nn - used, "%s%s", used ? " " : "", f[col_prog]);
        } else {
            snprintf(names + used, nn - used, " ...");
            full = 1;
        }
    }
    if (ffd >= 0)
        close(ffd);
    free(csv);
    return rows;
}

static void run_bounded(char *const argv[], int out, int err, long ms, const char *what,
                        char *rc, size_t n) {
    int e;
    struct child *c = spawn(argv, out, err, &e);
    if (!c) {
        snprintf(rc, n, "exec:%d", e);
        return;
    }
    if (!wait_child_said(c, ms, what)) {
        kill_group(c);
        wait_child(c, KILL_REAP_MS);
        snprintf(rc, n, "timeout");
        return;
    }
    status_str(c, rc, n);
}

static void phase_veristat(void) {
    long t0 = now_ms();
    if (!g_want[P_VERISTAT]) {
        phase_end(P_VERISTAT, "skip", t0, "not requested");
        return;
    }
    if (is_none()) {
        phase_end(P_VERISTAT, "skip", t0, "kind none");
        return;
    }
    if (!g_sched.nobj) {
        phase_end(P_VERISTAT, "skip", t0, "no BPF objects");
        return;
    }
    say("veristat over %d object(s)", g_sched.nobj);
    char *argv[MAX_OBJS + 12];
    int a = 0;
    argv[a++] = VERISTAT;
    argv[a++] = "-o";
    argv[a++] = "csv";
    for (int i = 0; i < g_sched.nobj; i++)
        argv[a++] = g_sched.obj[i];
    argv[a] = NULL;
    int out = open_log(WORK "/veristat.csv");
    int err = open_log(WORK "/veristat.err");
    char rc[32];
    run_bounded(argv, out, err, VERISTAT_MS, "veristat csv pass", rc, sizeof(rc));
    close(out);
    close(err);
    kv("veristat.rc", "%s", rc);
    if (!strncmp(rc, "exec:", 5)) {
        int e = atoi(rc + 5);
        phase_end(P_VERISTAT, "fail", t0, "exec %s: %s", VERISTAT, strerror(e));
        return;
    }
    // veristat writes its csv only at the end, so a pass that timed out left
    // nothing to narrow a second one to, and a second would only time out too.
    if (!strcmp(rc, "timeout")) {
        kv("veristat.verbose.rc", "skipped");
        phase_end(P_VERISTAT, "ran", t0, "csv pass timed out after %d s", VERISTAT_MS / 1000);
        send_file("veristat.csv", WORK "/veristat.csv", CAP_OTHER);
        send_file("veristat.err", WORK "/veristat.err", CAP_OTHER);
        return;
    }

    // Verbose pass. veristat's -v alone raises the log level to 1 for every
    // program, so: when the csv names failures, only those programs (-f file/prog
    // globs from a filter file) at level 1, each into a rotating buffer whose
    // tail keeps the final error and the "processed N insns" line; otherwise
    // level 4 (stats only) for all, which still carries any error line. The
    // buffer is always sized here: -v otherwise allocates the kernel's maximum
    // (up to 1 GiB) per program.
    int nfail = 0;
    char failing[640];
    int rows = parse_veristat_csv(&nfail, failing, sizeof(failing));
    char logsz[48];
    long per = MIB;
    a = 0;
    argv[a++] = VERISTAT;
    argv[a++] = "-v";
    if (rows && nfail) {
        per = (15 * MIB) / nfail;
        if (per < MIB)
            per = MIB;
        argv[a++] = "-l";
        argv[a++] = "1";
        argv[a++] = "-f";
        argv[a++] = "@" WORK "/veristat.filter";
    } else {
        argv[a++] = "-l";
        argv[a++] = "4";
    }
    snprintf(logsz, sizeof(logsz), "--log-size=%ld", per);
    argv[a++] = logsz;
    for (int i = 0; i < g_sched.nobj; i++)
        argv[a++] = g_sched.obj[i];
    argv[a] = NULL;
    // stderr apart: stdout is fully buffered into the file and libbpf's warnings
    // on stderr are not, so one shared file would splice them into the middle
    // of a program's log tail, where its error line sits.
    int vfd = open_log(WORK "/veristat-v.log");
    int vefd = open_log(WORK "/veristat-v.err");
    char vrc[32];
    run_bounded(argv, vfd, vefd, VERISTAT_MS, "veristat verbose pass", vrc, sizeof(vrc));
    close(vfd);
    close(vefd);
    kv("veristat.verbose.rc", "%s", vrc);
    if (rows)
        phase_end(P_VERISTAT, "ran", t0, "%d programs, %d failing%s%s", rows, nfail,
                  nfail ? ": " : "", failing);
    else
        phase_end(P_VERISTAT, "ran", t0, "no csv rows (rc=%s)", rc);
    send_file("veristat.csv", WORK "/veristat.csv", CAP_OTHER);
    send_file("veristat.err", WORK "/veristat.err", CAP_OTHER);
    send_file("veristat-v.log", WORK "/veristat-v.log", CAP_VERISTAT_V);
    send_file("veristat-v.err", WORK "/veristat-v.err", CAP_OTHER);
}

// Stops the scheduler if it is still running and ships its log. Once.
static void sched_finish(void) {
    if (!g_schedc || g_sched_finished)
        return;
    g_sched_finished = 1;
    stop_child(g_schedc, SIGINT, DETACH_EXIT_MS);
    char st[32];
    status_str(g_schedc, st, sizeof(st));
    kv("sched.exit", "%s", st);
    send_file("sched.log", WORK "/sched.log", CAP_SCHED_LOG);
}

static void phase_attach(void) {
    long t0 = now_ms();
    if (!g_want[P_ATTACH]) {
        phase_end(P_ATTACH, "skip", t0, "not requested");
        return;
    }
    if (is_none()) {
        phase_end(P_ATTACH, "skip", t0, "kind none");
        return;
    }
    char st[64], ops[128];
    if (!g_sched.binary[0]) {
        phase_end(P_ATTACH, "fail", t0, "sched.conf names no binary");
        return;
    }
    // Attached means an enable that happened after the spawn, by a scheduler
    // still running: a state that already read enabled was not this one's doing.
    sched_state(st, sizeof(st));
    long long seq0 = sched_enable_seq();
    if (!strcmp(st, "enabled")) {
        sched_ops(ops, sizeof(ops));
        say("WARN: sched_ext already enabled (ops=%s) before %s started", ops, g_sched.binary);
    }
    int lfd = open_log(WORK "/sched.log");
    int e;
    g_schedc = spawn(g_sched.argv, lfd, lfd, &e);
    if (!g_schedc) {
        if (lfd >= 0)
            dprintf(lfd, "vmm-init: exec %s: %s\n", g_sched.binary, strerror(e));
        close(lfd);
        sched_state(st, sizeof(st));
        sched_ops(ops, sizeof(ops));
        kv("attach.ms", "0");
        kv("attach.state", "%s", st);
        kv("attach.ops", "%s", ops);
        phase_end(P_ATTACH, "fail", t0, "exec %s: %s", g_sched.binary, strerror(e));
        send_file("sched.log", WORK "/sched.log", CAP_SCHED_LOG);
        return;
    }
    close(lfd);
    kv("sched.pid", "%d", (int)g_schedc->pid);
    char why[256] = "";
    for (;;) {
        reap();
        sched_state(st, sizeof(st));
        sched_ops(ops, sizeof(ops));
        int fresh = seq0 < 0 || sched_enable_seq() > seq0;
        if (fresh && !g_schedc->done && !strcmp(st, "enabled") &&
            (!g_sched.ops[0] || !strcmp(ops, g_sched.ops))) {
            g_attached = 1;
            break;
        }
        if (g_schedc->done) {
            char s[32];
            status_str(g_schedc, s, sizeof(s));
            snprintf(why, sizeof(why), "scheduler exited %s before attaching", s);
            break;
        }
        if (now_ms() - t0 >= ATTACH_MS) {
            snprintf(why, sizeof(why), "not attached after %d s", ATTACH_MS / 1000);
            break;
        }
        sleep_ms(POLL_MS);
    }
    kv("attach.ms", "%ld", now_ms() - t0);
    kv("attach.state", "%s", st);
    kv("attach.ops", "%s", ops);
    if (g_attached) {
        phase_end(P_ATTACH, "ok", t0, "state=%s ops=%s", st, ops);
        return;
    }
    phase_end(P_ATTACH, "fail", t0, "%s (state=%s ops=%s want=%s)", why, st, ops,
              g_sched.ops[0] ? g_sched.ops : "any");
    sched_finish();
}

static void montauk_start(void) {
    if (g_montauk_tried)
        return;
    g_montauk_tried = 1;
    int lfd = open_log(WORK "/montauk.log");
    // The pattern is a comma list of name substrings: the workloads' pand-
    // prefix, plus the scheduler's own binary so its userspace side is traced.
    char pat[sizeof(g_trace) + sizeof(g_sched.binary) + 2];
    const char *base = strrchr(g_sched.binary, '/');
    base = base ? base + 1 : g_sched.binary;
    snprintf(pat, sizeof(pat), "%s%s%s", g_trace, base[0] ? "," : "", base);
    mkdir(PROM_DIR, 0755);
    // A 1 MiB ring drops most of a --sched-detail stream on a busy guest; the
    // .prom recording is what montauk's cross-run comparison reads.
    // The ttyS2 stream is opt-in (vmm.stream=1): montauk writes it from the
    // thread that drains its event ring, so the port's rate would also cap
    // what --trace-out keeps.
    char *argv[] = {MONTAUK, "--headless", "--trace", pat, "--sched-detail",
                    "--trace-ring-bytes", "64M", "--trace-out", EVENTS,
                    "--log", PROM_DIR, "--log-interval-ms", "1000",
                    g_stream ? "--stream-out" : NULL, STREAM_TTY, NULL};
    int e;
    g_montauk = spawn(argv, lfd, lfd, &e);
    close(lfd);
    if (!g_montauk) {
        kv("montauk.exit", "exec:%d", e);
        say("montauk: exec failed: %s", strerror(e));
        return;
    }
    kv("montauk.pid", "%d", (int)g_montauk->pid);
    // events.bin exists before the probes load; the log line comes after.
    long end = now_ms() + MONTAUK_READY_MS;
    int ready = 0;
    while (now_ms() < end && !ready) {
        reap();
        if (g_montauk->done)
            break;
        char *text = slurp(WORK "/montauk.log", MIB, NULL);
        ready = text && strstr(text, MONTAUK_ATTACHED);
        free(text);
        if (!ready)
            sleep_ms(POLL_MS);
    }
    if (g_montauk->done) {
        char s[32];
        status_str(g_montauk, s, sizeof(s));
        kv("montauk.exit", "%s", s);
        say("montauk exited %s during startup (see montauk.log)", s);
        return;
    }
    kv("montauk.ready", "%s", ready ? "yes" : "timeout");
    if (ready)
        say("montauk tracing %s%s", g_trace, g_stream ? ", streaming to " STREAM_TTY : "");
    else
        say("WARN: montauk not attached after %d s; the capture may be empty (see montauk.log)",
            MONTAUK_READY_MS / 1000);
}

static void montauk_stop(void) {
    if (!g_montauk)
        return;
    struct child *c = g_montauk;
    g_montauk = NULL;
    stop_child(c, SIGTERM, MONTAUK_STOP_MS);
    char s[32];
    status_str(c, s, sizeof(s));
    kv("montauk.exit", "%s", s);
    say("montauk stopped (%s)", s);
}

static uint32_t fnv1a(const char *s) {
    uint32_t h = 2166136261u;
    for (; *s; s++)
        h = (h ^ (uint8_t)*s) * 16777619u;
    return h;
}

static int clampi(int v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : v; }

static void workload_argv(int w, char *argv[], char store[][32]) {
    int n = g_ncpu, k = 0;
    long d = g_duration;
    argv[k++] = (char *)WORKLOAD[w];
#define ARG(...) (snprintf(store[k], 32, __VA_ARGS__), argv[k] = store[k], k++)
    switch (w) {
    case 0: // held: pinned hogs on all but one CPU against continuous fsync writers
        ARG("held");
        ARG("%d", n > 1 ? n - 1 : 1);
        ARG("%d", clampi(n / 2, 1, 4));
        ARG("%ld", d);
        ARG("150");
        ARG("%s", SCRATCH);
        break;
    case 1:
        ARG("all");
        ARG("%d", n * 4);
        ARG("%ld", d);
        ARG("4");
        ARG("%s", SCRATCH);
        break;
    case 2:
        ARG("%ld", d);
        ARG("%d", clampi(n / 2, 1, 16));
        ARG("0");
        break;
    case 3:
        ARG("%ld", d);
        ARG("%d", n > 2 ? n : 2);
        ARG("4");
        ARG("%s", SCRATCH);
        break;
    case 4:
        ARG("%ld", d);
        ARG("%d", n * 2);
        ARG("%u", fnv1a(g_cell));
        break;
    }
#undef ARG
    argv[k] = NULL;
}

static void load_phase(int p) {
    long t0 = now_ms();
    if (!g_want[p]) {
        phase_end(p, "skip", t0, "not requested");
        return;
    }
    if (!is_none() && !g_attached) {
        phase_end(p, "skip", t0, "scheduler not attached");
        return;
    }
    montauk_start();
    t0 = now_ms();
    char logp[128];
    snprintf(logp, sizeof(logp), WORK "/phase-%s.log", PHASE[p]);
    int lfd = open_log(logp);
    int ws[NWORK], nw = 0;
    if (p == P_MIXED)
        for (int w = 0; w < NWORK; w++)
            ws[nw++] = w;
    else
        ws[nw++] = p - P_STRAND;
    struct child *c[NWORK];
    int eno[NWORK];
    for (int i = 0; i < nw; i++) {
        char *argv[16];
        char store[16][32];
        workload_argv(ws[i], argv, store);
        c[i] = spawn(argv, lfd, lfd, &eno[i]);
        if (!c[i] && lfd >= 0)
            dprintf(lfd, "vmm-init: exec %s: %s\n", WORKLOAD[ws[i]], strerror(eno[i]));
    }
    long end = t0 + g_duration * 1000L + WORKLOAD_GRACE_MS;
    long beat = t0 + HEARTBEAT_MS;
    int timed_out = 0;
    for (;;) {
        reap();
        int running = 0;
        for (int i = 0; i < nw; i++)
            running |= c[i] && !c[i]->done;
        if (!running)
            break;
        if (now_ms() >= end) {
            timed_out = 1;
            break;
        }
        if (now_ms() >= beat) {
            say("phase %s still running, %ld s", PHASE[p], (now_ms() - t0) / 1000);
            beat += HEARTBEAT_MS;
        }
        sleep_ms(POLL_MS);
    }
    // A workload whose main process left can still have children in its group.
    for (int i = 0; i < nw; i++) {
        if (!c[i])
            continue;
        kill_group(c[i]);
        wait_child(c[i], KILL_REAP_MS);
    }
    reap();
    if (lfd >= 0)
        close(lfd);

    int work_ok = 1;
    char detail[768] = "";
    size_t used = 0;
    for (int i = 0; i < nw; i++) {
        const char *name = WORKLOAD[ws[i]] + 5;
        char s[48];
        if (!c[i])
            snprintf(s, sizeof(s), "exec:%d", eno[i]);
        else if (timed_out && WIFSIGNALED(c[i]->status) && WTERMSIG(c[i]->status) == SIGKILL)
            snprintf(s, sizeof(s), "killed at %lds", g_duration + WORKLOAD_GRACE_MS / 1000);
        else
            status_str(c[i], s, sizeof(s));
        if (!c[i] || !exited_ok(c[i]))
            work_ok = 0;
        int w = snprintf(detail + used, sizeof(detail) - used, "%s%s rc=%s", used ? ", " : "",
                         name, s);
        if (w > 0 && used + (size_t)w < sizeof(detail))
            used += (size_t)w;
    }

    char st[64], ops[128], k[64];
    sched_state(st, sizeof(st));
    sched_ops(ops, sizeof(ops));
    snprintf(k, sizeof(k), "health.%s.state", PHASE[p]);
    kv(k, "%s", st);
    snprintf(k, sizeof(k), "health.%s.ops", PHASE[p]);
    kv(k, "%s", ops);
    int healthy = 1;
    if (!is_none()) {
        reap();
        int alive = !g_schedc->done;
        snprintf(k, sizeof(k), "health.%s.alive", PHASE[p]);
        kv(k, "%s", alive ? "yes" : "no");
        healthy = alive && !strcmp(st, "enabled") &&
                  (!g_sched.ops[0] || !strcmp(ops, g_sched.ops));
        snprintf(detail + used, sizeof(detail) - used, "; health %s (state=%s ops=%s alive=%s)",
                 healthy ? "ok" : "bad", st, ops, alive ? "yes" : "no");
    }
    phase_end(p, work_ok && healthy ? "ok" : "fail", t0, "%s", detail);
    char name[64];
    snprintf(name, sizeof(name), "phase-%s.log", PHASE[p]);
    send_file(name, logp, CAP_PHASE_LOG);
}

static void phase_detach(void) {
    long t0 = now_ms();
    if (!g_want[P_DETACH]) {
        phase_end(P_DETACH, "skip", t0, "not requested");
        return;
    }
    if (is_none()) {
        phase_end(P_DETACH, "skip", t0, "kind none");
        return;
    }
    if (!g_attached) {
        phase_end(P_DETACH, "skip", t0, "scheduler not attached");
        return;
    }
    char st[64], s[32];
    reap();
    if (g_schedc->done) {
        status_str(g_schedc, s, sizeof(s));
        sched_state(st, sizeof(st));
        kv("detach.state", "%s", st);
        kv("detach.ms", "0");
        phase_end(P_DETACH, "fail", t0, "scheduler exited %s before detach (state=%s)", s, st);
        sched_finish();
        return;
    }
    kill(g_schedc->pid, SIGINT);
    int killed = !wait_child(g_schedc, DETACH_EXIT_MS);
    if (killed) {
        kill_group(g_schedc);
        wait_child(g_schedc, KILL_REAP_MS);
    }
    long end = now_ms() + DETACH_STATE_MS;
    for (;;) {
        sched_state(st, sizeof(st));
        if (!strcmp(st, "disabled") || now_ms() >= end)
            break;
        sleep_ms(POLL_MS);
    }
    kv("detach.state", "%s", st);
    kv("detach.ms", "%ld", now_ms() - t0);
    status_str(g_schedc, s, sizeof(s));
    int ok = !killed && exited_ok(g_schedc) && !strcmp(st, "disabled");
    phase_end(P_DETACH, ok ? "ok" : "fail", t0, "exit=%s state=%s%s", s, st,
              killed ? " (SIGKILL after 15 s)" : "");
    sched_finish();
}

static void finish(void) {
    sched_finish();
    montauk_stop();
    kmsg_drain();
    send_file("kmsg.txt", WORK "/kmsg.txt", CAP_KMSG);
    int out = open_log(WORK "/sched_ext.txt");
    if (out >= 0) {
        dump_tree(out, SCX, 0);
        close(out);
        send_file("sched_ext.txt", WORK "/sched_ext.txt", CAP_OTHER);
    }
    send_file("montauk.log", WORK "/montauk.log", CAP_PHASE_LOG);
    // A .prom file is a run of whole scrapes; a tail cut would start mid-scrape,
    // so one over the cap is named and skipped rather than sent cut.
    DIR *pd = opendir(PROM_DIR);
    if (pd) {
        struct dirent *e;
        while ((e = readdir(pd))) {
            size_t nl = strlen(e->d_name);
            if (nl < 6 || strcmp(e->d_name + nl - 5, ".prom"))
                continue;
            char path[600];
            snprintf(path, sizeof(path), "%s/%s", PROM_DIR, e->d_name);
            struct stat ps;
            if (stat(path, &ps) != 0)
                continue;
            if (ps.st_size > CAP_PROM)
                kv("file.prom.skipped", "%s %lld", e->d_name, (long long)ps.st_size);
            else
                send_file(e->d_name, path, CAP_PROM);
        }
        closedir(pd);
    }
    struct stat ev;
    if (stat(EVENTS, &ev) == 0) {
        if (ev.st_size > CAP_EVENTS) {
            // Too large to send: the guest's own montauk analyzes it where it
            // lies, and the envelope crosses instead of the capture.
            kv("file.events.bin.skipped", "%lld", (long long)ev.st_size);
            int afd = open_log(WORK "/analyze.txt");
            char *argv[] = {MONTAUK, "--analyze", EVENTS, "--json-out", WORK "/analyze.json", NULL};
            char rc[32];
            run_bounded(argv, afd, afd, ANALYZE_MS, "montauk --analyze", rc, sizeof(rc));
            close(afd);
            kv("montauk.analyze.rc", "%s", rc);
            send_file("analyze.json", WORK "/analyze.json", CAP_ANALYZE);
            send_file("analyze.txt", WORK "/analyze.txt", CAP_PHASE_LOG);
        } else {
            send_file("events.bin", EVENTS, CAP_EVENTS);
        }
    }
    kv("done", "1");
    res_write("@@END\n", 6);
    if (g_res_virtio)
        sleep_ms(VIRTIO_SETTLE_MS);
    else
        drain(g_res);
    sync();
    umount2(SCRATCH, MNT_DETACH);
    say("results complete, powering off");
    drain(1);
    reboot(RB_POWER_OFF);
    say("reboot(RB_POWER_OFF) failed: %s", strerror(errno));
    for (;;) {
        reap();
        sleep_ms(1000);
    }
}

int main(void) {
    setup_fs();
    if (fcntl(1, F_GETFD) < 0) {
        int con = open("/dev/console", O_RDWR | O_NOCTTY);
        if (con >= 0) {
            dup2(con, 0);
            dup2(con, 1);
            dup2(con, 2);
            if (con > 2)
                close(con);
        }
    }
    setenv("PATH", "/bin:/usr/bin", 1);
    setenv("HOME", "/tmp", 1);
    struct rlimit unlimited = {RLIM_INFINITY, RLIM_INFINITY};
    setrlimit(RLIMIT_MEMLOCK, &unlimited);

    parse_cmdline();
    say("vm-matrix guest init, cell %s, duration %ld s", g_cell, g_duration);
    open_results();
    g_kmsg = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    kv("cell", "%s", g_cell);
    say_identity();
    load_modules();
    mount_scratch();
    read_sched_conf();
    kv("sched.kind", "%s", g_sched.kind);
    kv("sched.label", "%s", g_sched.label);
    say("scheduler %s (%s) binary=%s ops=%s objects=%d", g_sched.label, g_sched.kind,
        g_sched.binary[0] ? g_sched.binary : "-", g_sched.ops[0] ? g_sched.ops : "any",
        g_sched.nobj);
    kmsg_drain();

    phase_veristat();
    kmsg_drain();
    phase_attach();
    kmsg_drain();
    for (int p = P_STRAND; p <= P_MIXED; p++) {
        load_phase(p);
        kmsg_drain();
    }
    montauk_stop();
    phase_detach();
    finish();
    return 0;
}
