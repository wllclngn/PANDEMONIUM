# Dockerfile.builder -- assembles the guest initramfs. Nothing here runs qemu.
#
# This exists so the reproducer does not require a compiler, a linker, an
# archive toolchain, or a montauk install on the host. The previous builder ran
# on the bare host: it invoked the host's gcc to compile the guest workloads,
# the host's ldd to resolve the tracer's shared libraries, and the host's
# bsdtar/zstd/cpio/gzip to assemble the image. Every one of those is a thing a
# volunteer does not have, and together they made the suite buildable only on
# the machine it was written on.
#
# Same distribution as the host by design: the tracer binary is bundled with the
# shared libraries resolved HERE, so this image's C library lineage must match
# the one the tracer was linked against.

FROM archlinux:base-devel

RUN pacman -Syu --noconfirm --needed \
      gcc glibc binutils \
      libarchive zstd \
      cpio gzip findutils \
      python \
      cmake clang llvm bpf libbpf libelf zlib \
 && pacman -Scc --noconfirm

# NOTE, load-bearing: NVML is deliberately ABSENT from this image. The tracer's
# CMake probes for nvml.h and libnvidia-ml and compiles GPU support out when it
# finds neither, which is exactly what a headless guest wants. Building the
# guest tracer on a host that HAS an NVIDIA driver links libnvidia-ml.so.1 into
# it, and the guest then dies with "error while loading shared libraries"
# because no NVIDIA library exists inside a VM with no GPU. Do not add CUDA or
# nvidia-utils here to "fix" a build error; that reintroduces the failure.
#
# libbpf + bpf (bpftool) + clang are NOT optional: without them the tracer's
# CMake prints "libbpf/bpftool/clang not found: --trace disabled" and builds a
# tracer that cannot trace, which fails silently at capture time rather than at
# build time. payloads/build_montauk.py asserts trace support survived.

WORKDIR /build
