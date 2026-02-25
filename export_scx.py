#!/usr/bin/env python3
# PANDEMONIUM -> scx MONOREPO EXPORT
# AUTOMATES THE IMPORT PROCESS FOR sched-ext/scx.
#
# USAGE:
#   ./export_scx.py /path/to/scx
#
# WHAT IT DOES:
#   1. COPIES SOURCE FILES INTO scheds/rust/scx_pandemonium/
#   2. RENAMES CRATE: pandemonium -> scx_pandemonium
#   3. STRIPS [profile.release] (WORKSPACE PROVIDES ITS OWN)
#   4. REPLACES build.rs (USES MONOREPO BUNDLED vmlinux.h, NO bpftool)
#   5. ADDS WORKSPACE MEMBER TO ROOT Cargo.toml IF MISSING
#
# WHAT IT DOES NOT DO:
#   - DOES NOT COMMIT OR PUSH ANYTHING
#   - DOES NOT MODIFY Cargo.lock (RUN cargo update AFTER)

import os
import re
import shutil
import subprocess
import sys

CRATE_OLD = "pandemonium"
CRATE_NEW = "scx_pandemonium"
DEST_REL = os.path.join("scheds", "rust", CRATE_NEW)

# FILES TO COPY (RELATIVE TO PANDEMONIUM ROOT)
# MATCHES WHAT PIOTR IMPORTED IN HIS pandemonium-import BRANCH
INCLUDE = [
    "Cargo.toml",
    "LICENSE",
    "README.md",
    "build.rs",
    "pandemonium.py",
    "pandemonium_common.py",
    "include/",
    "src/",
    "tests/",
]

# FILES TO SKIP (NEVER EXPORT)
EXCLUDE = {
    ".git",
    ".gitignore",
    ".gitattributes",
    "target",
    "benchmark-results",
    "package-audit.txt",
    "COMMIT_MESSAGE.txt",
    "export_scx.py",
    "Cargo.lock",
}

def copy_tree(src_root, dst_root):
    """Copy INCLUDE paths from src_root to dst_root, skipping EXCLUDE."""
    copied = 0
    for entry in INCLUDE:
        src = os.path.join(src_root, entry)
        dst = os.path.join(dst_root, entry)

        if not os.path.exists(src):
            print(f"  SKIP (missing): {entry}")
            continue

        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDE))
            count = sum(len(files) for _, _, files in os.walk(dst))
            print(f"  COPY DIR: {entry} ({count} files)")
            copied += count
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  COPY: {entry}")
            copied += 1

    return copied


def rename_crate(dst_root):
    """Rename pandemonium -> scx_pandemonium in Cargo.toml and .rs files."""
    changes = 0

    # Cargo.toml: package name
    cargo_path = os.path.join(dst_root, "Cargo.toml")
    if os.path.exists(cargo_path):
        text = open(cargo_path).read()
        new_text = text.replace(
            f'name = "{CRATE_OLD}"',
            f'name = "{CRATE_NEW}"',
        )
        if new_text != text:
            open(cargo_path, "w").write(new_text)
            print(f"  RENAME: Cargo.toml package name -> {CRATE_NEW}")
            changes += 1

    # .rs files: use pandemonium:: -> use scx_pandemonium::
    for dirpath, _, filenames in os.walk(dst_root):
        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            fpath = os.path.join(dirpath, fname)
            text = open(fpath).read()
            new_text = text.replace(
                f"use {CRATE_OLD}::",
                f"use {CRATE_NEW}::",
            ).replace(
                f"extern crate {CRATE_OLD}",
                f"extern crate {CRATE_NEW}",
            ).replace(
                f"from {CRATE_OLD}::tuning",
                f"from {CRATE_NEW}::tuning",
            )
            if new_text != text:
                open(fpath, "w").write(new_text)
                rel = os.path.relpath(fpath, dst_root)
                print(f"  RENAME: {rel}")
                changes += 1

    return changes


def strip_profile_release(dst_root):
    """Remove [profile.release] block from Cargo.toml."""
    cargo_path = os.path.join(dst_root, "Cargo.toml")
    text = open(cargo_path).read()

    # Remove [profile.release] and all following key=value lines until next section or EOF
    pattern = r'\n\[profile\.release\]\n(?:[^\[]*)'
    new_text = re.sub(pattern, '\n', text)

    if new_text != text:
        open(cargo_path, "w").write(new_text.rstrip() + "\n")
        print("  STRIP: [profile.release] (workspace provides its own)")
        return 1
    return 0


# MONOREPO build.rs: USES BUNDLED vmlinux.h FROM scheds/vmlinux/
# NO bpftool DEPENDENCY, NO C23 PATCHING, NO BORE RENAMING.
# CRATE LIVES AT scheds/rust/scx_pandemonium/, VMLINUX AT scheds/vmlinux/
SCX_BUILD_RS = r'''// PANDEMONIUM BUILD SCRIPT (scx MONOREPO)
// USES BUNDLED vmlinux.h FROM scheds/vmlinux/ -- NO bpftool REQUIRED

use std::env;
use std::path::PathBuf;

use libbpf_cargo::SkeletonBuilder;

const BPF_SRC: &str = "src/bpf/main.bpf.c";

fn main() {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());

    // MONOREPO LAYOUT: scheds/rust/scx_pandemonium/ -> scheds/vmlinux/
    let vmlinux_search = manifest_dir.join("../../vmlinux");
    let vmlinux_dir = vmlinux_search.canonicalize().unwrap_or_else(|_| {
        panic!(
            "vmlinux directory not found at {:?} -- is this an scx monorepo checkout?",
            vmlinux_search
        )
    });

    // ARCHITECTURE-SPECIFIC vmlinux.h (x86 -> arch/x86/vmlinux.h)
    let arch = if cfg!(target_arch = "x86_64") || cfg!(target_arch = "x86") {
        "x86"
    } else if cfg!(target_arch = "aarch64") {
        "arm64"
    } else if cfg!(target_arch = "arm") {
        "arm"
    } else if cfg!(target_arch = "riscv64") || cfg!(target_arch = "riscv32") {
        "riscv"
    } else if cfg!(target_arch = "s390x") {
        "s390"
    } else if cfg!(target_arch = "powerpc64") || cfg!(target_arch = "powerpc") {
        "powerpc"
    } else {
        panic!("unsupported architecture for vmlinux.h")
    };

    let arch_vmlinux = vmlinux_dir.join("arch").join(arch).join("vmlinux.h");
    if !arch_vmlinux.exists() {
        panic!("vmlinux.h not found at {:?}", arch_vmlinux);
    }

    // INCLUDE PATHS: arch-specific vmlinux.h dir + monorepo scx headers
    let scx_include = manifest_dir.join("include");
    let arch_include = arch_vmlinux.parent().unwrap();

    let skel_out = out_dir.join("bpf.skel.rs");

    SkeletonBuilder::new()
        .source(BPF_SRC)
        .clang_args([
            "-I",
            arch_include.to_str().unwrap(),
            "-I",
            scx_include.to_str().unwrap(),
            "-I",
            vmlinux_dir.to_str().unwrap(),
        ])
        .build_and_generate(&skel_out)
        .unwrap();

    println!("cargo:rerun-if-changed={BPF_SRC}");
    println!("cargo:rerun-if-changed=src/bpf/intf.h");
    println!("cargo:rerun-if-changed=include/scx");
}
'''


def replace_build_rs(dst_root):
    """Replace standalone build.rs with monorepo version using bundled vmlinux.h."""
    build_rs = os.path.join(dst_root, "build.rs")
    open(build_rs, "w").write(SCX_BUILD_RS.lstrip("\n"))
    print("  REPLACE: build.rs -> monorepo vmlinux.h (no bpftool)")
    return 1


def add_workspace_member(scx_root):
    """Add scx_pandemonium to workspace Cargo.toml members if missing."""
    cargo_path = os.path.join(scx_root, "Cargo.toml")
    if not os.path.exists(cargo_path):
        print(f"  WARNING: {cargo_path} not found, skipping workspace registration")
        return 0

    text = open(cargo_path).read()
    member_line = f'    "{DEST_REL}",'

    if DEST_REL in text:
        print("  WORKSPACE: already registered")
        return 0

    # Find the members array and insert alphabetically
    lines = text.split("\n")
    new_lines = []
    inserted = False
    in_members = False

    for line in lines:
        if line.strip() == "members = [":
            in_members = True
            new_lines.append(line)
            continue

        if in_members and not inserted:
            stripped = line.strip()
            if stripped == "]":
                # End of members, insert before closing bracket
                new_lines.append(member_line)
                inserted = True
            elif stripped.startswith('"') and stripped.rstrip(",") > f'"{DEST_REL}"':
                # Insert before this line (alphabetical order)
                new_lines.append(member_line)
                inserted = True
                in_members = False

        new_lines.append(line)

    if inserted:
        open(cargo_path, "w").write("\n".join(new_lines))
        print(f"  WORKSPACE: added {DEST_REL} to members")
        return 1

    print(f"  WARNING: could not find insertion point in workspace Cargo.toml")
    return 0


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/scx")
        sys.exit(1)

    scx_root = os.path.abspath(sys.argv[1])
    pand_root = os.path.dirname(os.path.abspath(__file__))
    dst_root = os.path.join(scx_root, DEST_REL)

    # VALIDATE
    if not os.path.isdir(scx_root):
        print(f"ERROR: {scx_root} is not a directory")
        sys.exit(1)

    workspace_cargo = os.path.join(scx_root, "Cargo.toml")
    if not os.path.exists(workspace_cargo):
        print(f"ERROR: {workspace_cargo} not found (is this an scx checkout?)")
        sys.exit(1)

    print(f"PANDEMONIUM -> scx export")
    print(f"  Source: {pand_root}")
    print(f"  Target: {dst_root}")
    print()

    # CLEAN DESTINATION
    if os.path.exists(dst_root):
        shutil.rmtree(dst_root)
        print(f"  CLEAN: removed existing {DEST_REL}/")

    os.makedirs(dst_root, exist_ok=True)

    # STEP 1: COPY
    print("\n[1] COPY SOURCE FILES")
    copied = copy_tree(pand_root, dst_root)

    # STEP 2: RENAME CRATE
    print("\n[2] RENAME CRATE")
    renamed = rename_crate(dst_root)

    # STEP 3: STRIP PROFILE
    print("\n[3] STRIP RELEASE PROFILE")
    stripped = strip_profile_release(dst_root)

    # STEP 4: REPLACE build.rs FOR MONOREPO
    print("\n[4] BUILD SYSTEM")
    replace_build_rs(dst_root)

    # STEP 5: WORKSPACE REGISTRATION
    print("\n[5] WORKSPACE REGISTRATION")
    registered = add_workspace_member(scx_root)

    # STEP 6: CARGO FMT
    print("\n[6] FORMAT")
    result = subprocess.run(
        ["cargo", "fmt", "--manifest-path", os.path.join(dst_root, "Cargo.toml")],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  FMT: cargo fmt applied")
    else:
        print(f"  FMT: cargo fmt failed ({result.stderr.strip()})")
        print("  FMT: run manually: cargo fmt --manifest-path", os.path.join(dst_root, "Cargo.toml"))

    # SUMMARY
    print(f"\nDONE: {copied} files copied, {renamed} crate renames, "
          f"{stripped} profile stripped, {registered} workspace update")
    print(f"\nNext steps:")
    print(f"  cd {scx_root}")
    print(f"  cargo update -p {CRATE_NEW}")
    print(f"  cargo build -p {CRATE_NEW} --release")
    print(f"  git add -A && git diff --cached --stat")


if __name__ == "__main__":
    main()
