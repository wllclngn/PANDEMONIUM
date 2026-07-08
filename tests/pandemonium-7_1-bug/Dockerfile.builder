# Dockerfile.builder -- pand-defconfig-builder: the toolchain rootfs
# build-defconfig-guest.py exports (docker create + docker export) into the
# defconfig reproducer's ext4 image. Never runs qemu itself -- it is a
# filesystem source for `make vmlinux`, mke2fs -d packs the export straight
# into the guest disk. python3 is required at the exact path the guest's own
# PID 1 shebang expects (pand-defconfig-init.py: #!/usr/bin/python3), since
# this rootfs becomes the guest's root filesystem verbatim.

FROM archlinux:base-devel

RUN pacman -Syu --noconfirm --needed \
      bc bison flex \
      openssl ncurses libelf \
      perl pahole \
      python \
 && pacman -Scc --noconfirm

WORKDIR /
