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
#   4. REPLACES build.rs WITH scx_cargo::BpfBuilder (MATCHES OTHER SCHEDULERS)
#   5. SWAPS libbpf-cargo FOR scx_cargo PATH+VERSION DEP
#   6. STRIPS default-features=false FROM libbpf-rs (ENABLES VENDORED LIBBPF FOR CI)
#   7. PATCHES bpf_skel.rs INCLUDE PATH FOR BpfBuilder OUTPUT
#   8. PATCHES intf.h WITH BINDGEN-COMPATIBLE TYPE DEFINITIONS
#   9. ADDS WORKSPACE MEMBER TO ROOT Cargo.toml IF MISSING
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
    "src/",
    "veristat/",
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


SCX_BUILD_RS = """\
use std::process::Command;

fn main() {
    // BUILD ID: GIT SHA, DIRTY FLAG, TARGET TRIPLE
    // MATCHES scx_utils::build_id FORMAT USED BY OTHER SCX SCHEDULERS.
    let git_sha = Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    let git_dirty = Command::new("git")
        .args(["status", "--porcelain"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| !o.stdout.is_empty())
        .unwrap_or(false);

    let build_id = if git_sha.is_empty() {
        String::new()
    } else if git_dirty {
        format!("-g{}-dirty", git_sha)
    } else {
        format!("-g{}", git_sha)
    };

    let target = std::env::var("TARGET").unwrap_or_default();

    println!("cargo:rustc-env=PANDEMONIUM_BUILD_ID={}", build_id);
    println!("cargo:rustc-env=PANDEMONIUM_TARGET={}", target);

    scx_cargo::BpfBuilder::new()
        .unwrap()
        .enable_intf("src/bpf/intf.h", "bpf_intf.rs")
        .enable_skel("src/bpf/main.bpf.c", "main")
        .build()
        .unwrap();
}
"""

def replace_build_rs(dst_root):
    """Replace standalone build.rs with scx_cargo::BpfBuilder version."""
    build_rs = os.path.join(dst_root, "build.rs")
    open(build_rs, "w").write(SCX_BUILD_RS)
    print("  REPLACE: build.rs -> scx_cargo::BpfBuilder")
    return 1


def read_scx_cargo_version(scx_root):
    """Read scx_cargo version from the monorepo's rust/scx_cargo/Cargo.toml."""
    cargo_path = os.path.join(scx_root, "rust", "scx_cargo", "Cargo.toml")
    if not os.path.exists(cargo_path):
        print(f"  WARNING: {cargo_path} not found, falling back to wildcard version")
        return "*"
    text = open(cargo_path).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        print(f"  WARNING: no version found in {cargo_path}, falling back to wildcard")
        return "*"
    return m.group(1)


def swap_build_deps(dst_root, scx_root):
    """Replace libbpf-cargo with scx_cargo path+version dep (version read from repo)."""
    version = read_scx_cargo_version(scx_root)
    scx_cargo_dep = f'scx_cargo = {{ path = "../../../rust/scx_cargo", version = "{version}" }}'

    cargo_path = os.path.join(dst_root, "Cargo.toml")
    text = open(cargo_path).read()
    new_text = re.sub(
        r'libbpf-cargo\s*=\s*"[^"]*"',
        scx_cargo_dep,
        text,
    )
    if new_text != text:
        open(cargo_path, "w").write(new_text)
        print(f"  SWAP: libbpf-cargo -> scx_cargo (version {version})")
        return 1
    print("  WARNING: libbpf-cargo not found in [build-dependencies]")
    return 0


def fix_libbpf_vendoring(dst_root):
    """Enable libbpf vendoring by stripping default-features = false from libbpf-rs.

    Standalone builds use system libbpf (default-features = false).
    scx CI doesn't have system libbpf on the linker path -- needs vendored build.
    Removing default-features = false re-enables libbpf-sys's vendored-libbpf
    feature, so libbpf.a gets built from source and linked statically.
    """
    cargo_path = os.path.join(dst_root, "Cargo.toml")
    text = open(cargo_path).read()
    new_text = re.sub(
        r'(libbpf-rs\s*=\s*\{[^}]*),\s*default-features\s*=\s*false',
        r'\1',
        text,
    )
    if new_text != text:
        open(cargo_path, "w").write(new_text)
        print("  FIX: libbpf-rs default-features = false stripped (enables vendored libbpf)")
        return 1
    print("  FIX: libbpf-rs already uses default features")
    return 0


def normalize_dep_versions(dst_root):
    """Normalize dependency versions to API boundaries (e.g. "0.2.175" -> "0.2").

    Tejun Heo's convention for the scx monorepo: only specify the API-breaking
    boundary, not exact versions. Cargo.lock pins the exact versions.
    """
    cargo_path = os.path.join(dst_root, "Cargo.toml")
    text = open(cargo_path).read()

    def normalize_version(m):
        """Reduce "X.Y.Z" to "X.Y" for 0.x or "X" for 1.x+."""
        prefix = m.group(1)
        ver = m.group(2)
        parts = ver.split(".")
        if len(parts) >= 2 and parts[0] == "0":
            # 0.x.y -> 0.x (minor is breaking for 0.x)
            normalized = f"{parts[0]}.{parts[1]}"
        elif len(parts) >= 1 and int(parts[0]) >= 1:
            # X.y.z -> X (major is breaking for 1.x+)
            normalized = parts[0]
        else:
            normalized = ver
        return f'{prefix}"{normalized}"'

    # Match: key = "X.Y.Z" (simple string version deps)
    new_text = re.sub(
        r'((?:libc|anyhow|clap|ctrlc|flate2|regex|libbpf-rs)\s*=\s*(?:\{[^}]*version\s*=\s*|))"(\d+\.\d+\.\d+)"',
        normalize_version,
        text,
    )
    # Also handle exact pin: "=X.Y.Z" -> "=X.Y.Z" (leave exact pins alone)
    # The regex above won't match "=0.26.1" because of the = prefix, so we're safe.

    if new_text != text:
        open(cargo_path, "w").write(new_text)
        print("  NORMALIZE: dependency versions reduced to API boundaries")
        return 1
    print("  NORMALIZE: versions already normalized")
    return 0


def patch_intf_types(dst_root):
    """Add portable type definitions so bindgen can parse intf.h without vmlinux.h."""
    intf_path = os.path.join(dst_root, "src", "bpf", "intf.h")
    if not os.path.exists(intf_path):
        print("  WARNING: src/bpf/intf.h not found")
        return 0

    text = open(intf_path).read()

    # Insert unconditional typedefs after the include guard.
    # C11+ (and C23 which PANDEMONIUM targets) allows duplicate compatible
    # typedefs, so these coexist safely with vmlinux.h during BPF compilation.
    # scx_utils defines __bpf__ during bindgen, so a #ifndef guard won't work.
    type_compat = (
        "\n// BINDGEN/SCX COMPATIBILITY: provide kernel types unconditionally.\n"
        "// vmlinux.h also typedefs these in BPF context; C11+ permits\n"
        "// duplicate compatible typedefs, so no conflict.\n"
        "typedef unsigned long long u64;\n"
        "typedef unsigned char u8;\n"
    )

    anchor = "#define __INTF_H\n"
    if anchor not in text:
        print("  WARNING: intf.h missing expected include guard, skipping type patch")
        return 0

    if "typedef unsigned long long u64;" in text:
        print("  PATCH: intf.h type compatibility already present")
        return 0

    new_text = text.replace(anchor, anchor + type_compat)
    open(intf_path, "w").write(new_text)
    print("  PATCH: intf.h type compatibility (u64/u8 for bindgen)")
    return 1


def patch_dsq_move_to_local(dst_root):
    """Add enq_flags=0 to scx_bpf_dsq_move_to_local() calls for scx monorepo.

    Standalone uses a 1-arg compat macro. scx monorepo uses the 2-arg native API.
    """
    bpf_path = os.path.join(dst_root, "src", "bpf", "main.bpf.c")
    if not os.path.exists(bpf_path):
        print("  WARNING: src/bpf/main.bpf.c not found")
        return 0

    text = open(bpf_path).read()
    token = "scx_bpf_dsq_move_to_local("
    parts = []
    count = 0
    i = 0

    while i < len(text):
        j = text.find(token, i)
        if j == -1:
            parts.append(text[i:])
            break

        parts.append(text[i:j + len(token)])
        i = j + len(token)

        # find matching close paren
        depth = 1
        k = i
        while k < len(text) and depth > 0:
            if text[k] == '(':
                depth += 1
            elif text[k] == ')':
                depth -= 1
            k += 1

        inner = text[i:k - 1]
        if not inner.rstrip().endswith(", 0"):
            parts.append(inner + ", 0)")
            count += 1
        else:
            parts.append(inner + ")")
        i = k

    if count:
        open(bpf_path, "w").write("".join(parts))
        print(f"  PATCH: scx_bpf_dsq_move_to_local -> 2-arg ({count} calls)")
        return count
    print("  PATCH: scx_bpf_dsq_move_to_local already 2-arg")
    return 0


def read_scx_utils_version(scx_root):
    """Read scx_utils version from the monorepo's rust/scx_utils/Cargo.toml."""
    cargo_path = os.path.join(scx_root, "rust", "scx_utils", "Cargo.toml")
    if not os.path.exists(cargo_path):
        print(f"  WARNING: {cargo_path} not found, falling back to wildcard version")
        return "*"
    text = open(cargo_path).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        print(f"  WARNING: no version found in {cargo_path}, falling back to wildcard")
        return "*"
    return m.group(1)


def add_scx_utils_dep(dst_root, scx_root):
    """Add scx_utils dependency to Cargo.toml [dependencies]."""
    version = read_scx_utils_version(scx_root)
    cargo_path = os.path.join(dst_root, "Cargo.toml")
    text = open(cargo_path).read()

    if "scx_utils" in text:
        print("  DEP: scx_utils already present")
        return 0

    scx_utils_line = f'scx_utils = {{ path = "../../../rust/scx_utils", version = "{version}" }}'
    # Insert after the [dependencies] header
    new_text = text.replace(
        "[dependencies]\n",
        f"[dependencies]\n{scx_utils_line}\n",
    )
    if new_text != text:
        open(cargo_path, "w").write(new_text)
        print(f"  DEP: added scx_utils (version {version})")
        return 1
    print("  WARNING: could not find [dependencies] section")
    return 0


def patch_version_display(dst_root):
    """Patch main.rs to use scx_utils::build_id for version display.

    Replaces custom PANDEMONIUM_BUILD_ID/PANDEMONIUM_TARGET env vars with
    scx_utils::build_id::full_version(), matching other scx schedulers.
    """
    main_path = os.path.join(dst_root, "src", "main.rs")
    if not os.path.exists(main_path):
        print("  WARNING: src/main.rs not found")
        return 0

    text = open(main_path).read()
    changes = 0

    # Add use scx_utils::build_id import after existing use statements
    if "use scx_utils::build_id" not in text:
        text = text.replace(
            "use scheduler::Scheduler;",
            "use scheduler::Scheduler;\nuse scx_utils::build_id;",
        )
        changes += 1

    # Replace clap version attribute with disable_version_flag + manual flag
    old_clap = (
        '#[command(version = concat!(env!("CARGO_PKG_VERSION"), '
        'env!("PANDEMONIUM_BUILD_ID"), " ", env!("PANDEMONIUM_TARGET")))]\n'
        '#[command(about = "PANDEMONIUM -- ADAPTIVE LINUX SCHEDULER")]'
    )
    new_clap = (
        "#[command(\n"
        "    version,\n"
        "    disable_version_flag = true,\n"
        '    about = "PANDEMONIUM -- ADAPTIVE LINUX SCHEDULER"\n'
        ")]"
    )
    if old_clap in text:
        text = text.replace(old_clap, new_clap)
        changes += 1

    # Add --version flag to Cli struct (after verbose field)
    version_field = (
        "    /// Print scheduler version and exit.\n"
        "    #[arg(long)]\n"
        "    version: bool,"
    )
    if "version: bool" not in text:
        text = text.replace(
            "    #[arg(short, long)]\n    verbose: bool,",
            "    #[arg(short, long)]\n    verbose: bool,\n\n" + version_field,
        )
        changes += 1

    # Replace banner log_info with build_id::full_version
    old_banner = (
        '    log_info!(\n'
        '        "scx_pandemonium {}{} {} SMT {}",\n'
        '        env!("CARGO_PKG_VERSION"),\n'
        '        env!("PANDEMONIUM_BUILD_ID"),\n'
        '        env!("PANDEMONIUM_TARGET"),\n'
        '        if smt_on { "on" } else { "off" }\n'
        '    );'
    )
    new_banner = (
        '    log_info!(\n'
        '        "scx_pandemonium {} SMT {}",\n'
        '        build_id::full_version(env!("CARGO_PKG_VERSION")),\n'
        '        if smt_on { "on" } else { "off" }\n'
        '    );'
    )
    if old_banner in text:
        text = text.replace(old_banner, new_banner)
        changes += 1

    # Add --version handler in main() before the match block
    version_handler = (
        '\n    if cli.version {\n'
        '        println!(\n'
        '            "scx_pandemonium {}",\n'
        '            build_id::full_version(env!("CARGO_PKG_VERSION"))\n'
        '        );\n'
        '        return Ok(());\n'
        '    }\n'
    )
    if "if cli.version {" not in text:
        text = text.replace(
            "    match cli.command {",
            version_handler + "\n    match cli.command {",
        )
        changes += 1

    if changes:
        open(main_path, "w").write(text)
        print(f"  PATCH: main.rs version display -> scx_utils::build_id ({changes} changes)")
        return changes
    print("  PATCH: main.rs version display already patched")
    return 0


def patch_ops_version_suffix(dst_root):
    """Inject version suffix into struct_ops name for scx_loader GUI display.

    scx_loader reads /sys/kernel/sched_ext/root/ops to get the scheduler name
    and version. Without this, the version appears in logs but not in the GUI.
    Matches the pattern used by scx_cake, scx_flash, etc.
    """
    sched_path = os.path.join(dst_root, "src", "scheduler.rs")
    if not os.path.exists(sched_path):
        print("  WARNING: src/scheduler.rs not found")
        return 0

    text = open(sched_path).read()

    if "ops_version_suffix" in text:
        print("  PATCH: ops version suffix already present")
        return 0

    # Insert version suffix injection between open and load
    inject_block = (
        '        let mut open_skel = builder.open(open_object)?;\n'
        '\n'
        '        // INJECT VERSION SUFFIX INTO OPS NAME FOR scx_loader GUI\n'
        '        {\n'
        '            let ops = open_skel.struct_ops.pandemonium_ops_mut();\n'
        '            let name_field = &mut ops.name;\n'
        '            let version_suffix = scx_utils::build_id::ops_version_suffix(env!("CARGO_PKG_VERSION"));\n'
        '            let bytes = version_suffix.as_bytes();\n'
        '            let mut i = 0;\n'
        '            let mut bytes_idx = 0;\n'
        '            let mut found_null = false;\n'
        '            while i < name_field.len() - 1 {\n'
        '                found_null |= name_field[i] == 0;\n'
        '                if !found_null {\n'
        '                    i += 1;\n'
        '                    continue;\n'
        '                }\n'
        '                if bytes_idx < bytes.len() {\n'
        '                    name_field[i] = bytes[bytes_idx] as i8;\n'
        '                    bytes_idx += 1;\n'
        '                } else {\n'
        '                    break;\n'
        '                }\n'
        '                i += 1;\n'
        '            }\n'
        '            name_field[i] = 0;\n'
        '        }'
    )

    old_anchor = '        let mut open_skel = builder.open(open_object)?;'
    if old_anchor not in text:
        print("  WARNING: could not find open_skel anchor in scheduler.rs")
        return 0

    new_text = text.replace(old_anchor, inject_block)
    open(sched_path, "w").write(new_text)
    print("  PATCH: scheduler.rs ops version suffix (scx_loader GUI)")
    return 1


def patch_bpf_skel_include(dst_root):
    """Patch bpf_skel.rs include path for BpfBuilder output filename."""
    skel_path = os.path.join(dst_root, "src", "bpf_skel.rs")
    if not os.path.exists(skel_path):
        print("  WARNING: src/bpf_skel.rs not found")
        return 0
    text = open(skel_path).read()
    new_text = text.replace("/bpf.skel.rs", "/main_skel.rs")
    if new_text != text:
        open(skel_path, "w").write(new_text)
        print("  PATCH: bpf_skel.rs include path (bpf.skel.rs -> main_skel.rs)")
        return 1
    return 0


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
    print("\n#1: COPY SOURCE FILES")
    copied = copy_tree(pand_root, dst_root)

    # STEP 2: RENAME CRATE
    print("\n#2: RENAME CRATE")
    renamed = rename_crate(dst_root)

    # STEP 3: STRIP PROFILE
    print("\n#3: STRIP RELEASE PROFILE")
    stripped = strip_profile_release(dst_root)

    # STEP 4: BUILD SYSTEM (MATCH scx CONVENTION)
    print("\n#4: BUILD SYSTEM")
    replace_build_rs(dst_root)
    swap_build_deps(dst_root, scx_root)
    fix_libbpf_vendoring(dst_root)
    normalize_dep_versions(dst_root)
    patch_bpf_skel_include(dst_root)
    patch_intf_types(dst_root)
    patch_dsq_move_to_local(dst_root)
    add_scx_utils_dep(dst_root, scx_root)
    patch_version_display(dst_root)
    patch_ops_version_suffix(dst_root)

    # STEP 5: WORKSPACE REGISTRATION
    print("\n#5: WORKSPACE REGISTRATION")
    registered = add_workspace_member(scx_root)

    # STEP 6: CARGO FMT
    print("\n#6: FORMAT")
    result = subprocess.run(
        ["cargo", "fmt", "--manifest-path", os.path.join(dst_root, "Cargo.toml")],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  FMT: cargo fmt applied")
    else:
        print(f"  FMT: cargo fmt failed ({result.stderr.strip()})")
        print("  FMT: run manually: cargo fmt --manifest-path", os.path.join(dst_root, "Cargo.toml"))

    # STEP 7: UPDATE CARGO.LOCK (CI BUILDS WITH --locked)
    print("\n#7: UPDATE CARGO.LOCK")
    result = subprocess.run(
        ["cargo", "update", "-p", CRATE_NEW],
        capture_output=True, text=True, cwd=scx_root,
    )
    if result.returncode == 0:
        print(f"  LOCK: cargo update -p {CRATE_NEW} succeeded")
    else:
        print(f"  LOCK: cargo update failed ({result.stderr.strip()})")
        print(f"  LOCK: run manually: cd {scx_root} && cargo update -p {CRATE_NEW}")

    # SUMMARY
    print(f"\nDONE: {copied} files copied, {renamed} crate renames, "
          f"{stripped} profile stripped, {registered} workspace update")
    print(f"\nNext steps:")
    print(f"  cd {scx_root}")
    print(f"  cargo build -p {CRATE_NEW} --release")
    print(f"  git add -A && git diff --cached --stat")


if __name__ == "__main__":
    main()
