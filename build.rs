// PANDEMONIUM BUILD SCRIPT
// COMPILES src/bpf/main.bpf.c INTO BPF BYTECODE AND GENERATES RUST SKELETON
// GENERATES vmlinux.h FROM RUNNING KERNEL'S BTF AT BUILD TIME
// MUST USE LOCAL BTF: SCHED_EXT TYPES (p->scx, SCX_DSQ_*, etc.) ARE ONLY
// PRESENT IN KERNELS WITH CONFIG_SCHED_CLASS_EXT=y -- GENERIC vmlinux.h
// FROM GITHUB/LIBBPF WILL NOT WORK.

use std::env;
use std::path::PathBuf;
use std::process::Command;

use libbpf_cargo::SkeletonBuilder;

const BPF_SRC: &str = "src/bpf/main.bpf.c";

fn main() {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());

    // GENERATE vmlinux.h FROM RUNNING KERNEL'S BTF
    // CACHED AT /tmp/pandemonium-vmlinux.h TO AVOID REGENERATING EVERY BUILD.
    // LAYOUT: $OUT_DIR/include/vmlinux/vmlinux.h
    // SCX HEADERS USE ../vmlinux.h RELATIVE TO include/scx/,
    // SO WE NEED include/vmlinux/vmlinux.h AT THE SAME LEVEL AS include/scx/
    let vmlinux_dir = out_dir.join("include").join("vmlinux");
    std::fs::create_dir_all(&vmlinux_dir).expect("failed to create vmlinux dir");

    let vmlinux_h = vmlinux_dir.join("vmlinux.h");
    let cache_path = PathBuf::from("/tmp/pandemonium-vmlinux.h");

    // USE CACHED VERSION IF IT EXISTS AND IS NON-EMPTY
    if cache_path.exists() && cache_path.metadata().map(|m| m.len() > 1000).unwrap_or(false) {
        let raw = std::fs::read_to_string(&cache_path).expect("cached vmlinux.h is not utf-8");
        let patched = patch_vmlinux_c23(&raw);
        std::fs::write(&vmlinux_h, patched.as_bytes()).expect("failed to write vmlinux.h");
    } else {
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
        // CACHE RAW OUTPUT
        std::fs::write(&cache_path, &output.stdout).expect("failed to cache vmlinux.h");
        // PATCH AND WRITE
        let raw = String::from_utf8(output.stdout).expect("vmlinux.h is not utf-8");
        let patched = patch_vmlinux_c23(&raw);
        std::fs::write(&vmlinux_h, patched.as_bytes()).expect("failed to write vmlinux.h");
    }

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
            "-std=gnu23",
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

// PATCH vmlinux.h FOR C23 COMPATIBILITY.
// C23 MAKES true/false/bool KEYWORDS, BUT bpftool EMITS THEM AS
// ENUM VALUES AND A TYPEDEF. RENAME THE CONFLICTS.
fn patch_vmlinux_c23(raw: &str) -> String {
    raw.replace("typedef _Bool bool;", "/* C23: bool is a keyword */")
        .replace("\tfalse = 0,", "\t/* C23: false */ _false = 0,")
        .replace("\ttrue = 1,", "\t/* C23: true */ _true = 1,")
}
