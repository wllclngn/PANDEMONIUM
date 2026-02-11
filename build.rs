// PANDEMONIUM BUILD SCRIPT
// COMPILES src/bpf/main.bpf.c INTO BPF BYTECODE AND GENERATES RUST SKELETON

use std::env;
use std::path::PathBuf;

use libbpf_cargo::SkeletonBuilder;

const BPF_SRC: &str = "src/bpf/main.bpf.c";

fn main() {
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("bpf.skel.rs");

    SkeletonBuilder::new()
        .source(BPF_SRC)
        .clang_args([
            "-I", "include",
            "-I", "include/vmlinux",
        ])
        .build_and_generate(&out)
        .unwrap();

    println!("cargo:rerun-if-changed={BPF_SRC}");
    println!("cargo:rerun-if-changed=src/bpf/intf.h");
    println!("cargo:rerun-if-changed=include/scx");
    println!("cargo:rerun-if-changed=include/vmlinux");
}
