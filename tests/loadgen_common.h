// loadgen_common: shared primitives for the loadgen pattern driver.
//
// Extracted from stormwork.c and coldwork.c -- the pin/timing/spin/futex/MSR/CSV
// scaffolding both duplicated byte for byte (identical pin_to, same LCG constant).
// Patterns live in loadgen.c and compose these; new patterns reuse them instead
// of re-rolling the fiddly, correctness-critical boilerplate.
#ifndef LOADGEN_COMMON_H
#define LOADGEN_COMMON_H

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#include <time.h>

// monotonic time
uint64_t now_ns(void);
long     ts_ns(struct timespec t);
long     elapsed_ns(struct timespec a, struct timespec b);

// CPU burn. spin_ns busy-waits for ns via an LCG; cpu_quantum runs `iters` of the
// same LCG and returns the accumulator (assign to a volatile sink so it survives).
void          spin_ns(uint64_t ns);
unsigned long cpu_quantum(unsigned long iters);

// pin the calling thread (or process) to one CPU
void pin_to(int cpu);

// futex wait/wake on a 32-bit word
int futex_op(int *uaddr, int op, int val);

// model-specific register read (APERF/MPERF); fd from /dev/cpu/N/msr, 0 on failure
uint64_t rdmsr(int fd, off_t reg);

// parse a comma-separated list of unsigned longs into out[]; returns count (<= cap)
int parse_csv(const char *csv, unsigned long *out, int cap);

// pattern dispatch: a pattern receives the args that follow its name and runs its
// own control structure (sustained pool / table sweep / phased) over the primitives.
struct loadctx { int argc; char **argv; };
struct load_pattern { const char *name; int (*run)(struct loadctx *); };

#endif // LOADGEN_COMMON_H
