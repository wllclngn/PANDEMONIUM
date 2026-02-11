// PANDEMONIUM BUILD SCRIPT
// COMPILES src/bpf/main.bpf.c INTO BPF BYTECODE AND GENERATES RUST SKELETON
// GENERATES vmlinux.h FROM RUNNING KERNEL'S BTF AT BUILD TIME (NO VENDORING)

use std::env;
use std::path::PathBuf;
use std::process::Command;

use libbpf_cargo::SkeletonBuilder;

const BPF_SRC: &str = "src/bpf/main.bpf.c";

fn main() {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());

    // GENERATE vmlinux.h FROM RUNNING KERNEL'S BTF
    // LAYOUT: $OUT_DIR/include/vmlinux/vmlinux.h
    // SCX HEADERS USE ../vmlinux.h RELATIVE TO include/scx/,
    // SO WE NEED include/vmlinux/vmlinux.h AT THE SAME LEVEL AS include/scx/
    let vmlinux_dir = out_dir.join("include").join("vmlinux");
    std::fs::create_dir_all(&vmlinux_dir).expect("failed to create vmlinux dir");

    let vmlinux_h = vmlinux_dir.join("vmlinux.h");
    let output = Command::new("bpftool")
        .args(["btf", "dump", "file", "/sys/kernel/btf/vmlinux", "format", "c"])
        .output()
        .expect("bpftool not found -- install bpftool (pacman -S bpf)");

    if !output.status.success() {
        panic!(
            "bpftool failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    std::fs::write(&vmlinux_h, &output.stdout).expect("failed to write vmlinux.h");

    // SYMLINK: $OUT_DIR/include/vmlinux.h -> vmlinux/vmlinux.h
    // SO THAT #include "../vmlinux.h" FROM scx/ HEADERS RESOLVES
    let vmlinux_symlink = out_dir.join("include").join("vmlinux.h");
    let _ = std::fs::remove_file(&vmlinux_symlink);
    std::os::unix::fs::symlink(
        vmlinux_dir.join("vmlinux.h"),
        &vmlinux_symlink,
    )
    .expect("failed to symlink vmlinux.h");

    let gen_include = out_dir.join("include");
    let skel_out = out_dir.join("bpf.skel.rs");

    SkeletonBuilder::new()
        .source(BPF_SRC)
        .clang_args([
            "-I",
            "include",
            "-I",
            gen_include.to_str().unwrap(),
            "-I",
            vmlinux_dir.to_str().unwrap(),
        ])
        .build_and_generate(&skel_out)
        .unwrap();

    println!("cargo:rerun-if-changed={BPF_SRC}");
    println!("cargo:rerun-if-changed=src/bpf/intf.h");
    println!("cargo:rerun-if-changed=include/scx");
}
