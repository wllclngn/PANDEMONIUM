#!/usr/bin/env python3
# PANDEMONIUM -> scx MONOREPO EXPORT
# AUTOMATES THE IMPORT PROCESS FOR sched-ext/scx.
#
# USAGE:
#   ./export_scx.py /path/to/scx [--ported HASH ...]
#
# BEFORE ANYTHING IS TOUCHED:
#   FETCHES THE sched-ext REMOTE AND REFUSES IF ANY COMMIT TOUCHED THE CRATE
#   SINCE OUR LAST RELEASE THERE. THE EXPORT REPLACES THE CRATE WHOLESALE, SO AN
#   UNPORTED UPSTREAM EDIT WOULD BE REVERTED. PORT EACH ONE INTO THIS TREE, THEN
#   RERUN WITH --ported NAMING EVERY LISTED HASH
#   READS scx's SCHEDULER CATALOG (services/catalog.json), WHICH scxcli AND
#   scxtui START SCHEDULERS FROM, AND REFUSES IF IT NO LONGER LISTS
#   scx_pandemonium FEATURED OR STABLE, OR IF catalog/profiles.json BREAKS
#   THE CATALOG's RULES FOR A CUSTOM PROFILE
#
# WHAT IT DOES:
#   1. COPIES SOURCE FILES INTO scheds/rust/scx_pandemonium/, catalog/profiles.json
#      INCLUDED: THE BPF ARM's PROFILE FOR AN ADMINISTRATOR's /etc/scx/catalog.json,
#      SINCE scx's SHIPPED CATALOG TAKES NO PROFILE WITH OPTIONS
#   2. RENAMES CRATE: pandemonium -> scx_pandemonium
#   3. STRIPS [profile.release] (WORKSPACE PROVIDES ITS OWN)
#   4. REPLACES build.rs WITH scx_cargo::BpfBuilder (MATCHES OTHER SCHEDULERS)
#   5. SWAPS libbpf-cargo FOR scx_cargo PATH+VERSION DEP
#   6. STRIPS default-features=false FROM libbpf-rs (ENABLES VENDORED LIBBPF FOR CI)
#   7. PATCHES bpf_skel.rs INCLUDE PATH FOR BpfBuilder OUTPUT
#   8. ADDS WORKSPACE MEMBER TO ROOT Cargo.toml IF MISSING
#   9. FORMATS, UPDATES Cargo.lock, THEN FAILS ON cargo fmt --check OR A FAILED
#      cargo build OF THE CRATE
#  10. WRITES THE PR BODY FROM COMMIT_MESSAGE.txt TO /tmp/pandemonium/ WHEN THE
#      TREE HAS ONE (THE PUBLISHED TREE DOES NOT)
#
# THE BPF SOURCE IS COPIED AS IT STANDS: IT IS WRITTEN AGAINST THE SAME scx
# HEADER API THE MONOREPO BUILDS IT WITH.
#
# WHAT IT DOES NOT DO:
#   - DOES NOT COMMIT OR PUSH ANYTHING

import json
import os
import re
import shutil
import subprocess
import sys

from pandemonium_common import (
    get_git_info, get_version, log, log_error, log_info, log_warn, scx_profiles,
)

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
    "catalog/",
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


def assert_export_is_exactly_include(dst_root):
    """The export tree may contain WHAT INCLUDE NAMES AND NOTHING ELSE.

    An allowlist that is only consulted while copying is a property of a list
    someone can edit; this checks the RESULT. Anything that reaches the tree by
    any route -- a new INCLUDE entry, a stray copy, a file dropped in by hand --
    fails the export rather than shipping. Nothing needs to be enumerated as
    forbidden, which is the point: A denylist has to predict what to fear and
    names it where anyone can read it.
    """
    permitted = {e.rstrip("/").split("/")[0] for e in INCLUDE}
    present = set(os.listdir(dst_root))
    unexpected = sorted(present - permitted)
    if unexpected:
        log_error("the export tree holds entries INCLUDE does not name: "
                  + ", ".join(unexpected))
        shutil.rmtree(dst_root, ignore_errors=True)
        raise SystemExit(2)
    log_info(f"export tree matches INCLUDE exactly ({len(present)} entries)")

def copy_tree(src_root, dst_root):
    """Copy INCLUDE paths from src_root to dst_root, skipping EXCLUDE."""
    copied = 0
    for entry in INCLUDE:
        src = os.path.join(src_root, entry)
        dst = os.path.join(dst_root, entry)

        if not os.path.exists(src):
            log_warn(f"skipped {entry}: missing")
            continue

        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDE))
            count = sum(len(files) for _, _, files in os.walk(dst))
            log_info(f"copied {entry} ({count} files)")
            copied += count
        else:
            # EXCLUDE reaches the directory walk through ignore_patterns and never
            # reached this branch, so a skipped name listed in INCLUDE would have
            # copied cleanly. Check it here too.
            if os.path.basename(entry) in EXCLUDE:
                log_warn(f"skipped {entry}: excluded")
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            log_info(f"copied {entry}")
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
            log_info(f"renamed the Cargo.toml package to {CRATE_NEW}")
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
                log_info(f"renamed crate references in {rel}")
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
        log_info("stripped [profile.release] (the workspace provides its own)")
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
    log_info("replaced build.rs with scx_cargo::BpfBuilder")
    return 1


def read_scx_cargo_version(scx_root):
    """Read scx_cargo version from the monorepo's rust/scx_cargo/Cargo.toml."""
    cargo_path = os.path.join(scx_root, "rust", "scx_cargo", "Cargo.toml")
    if not os.path.exists(cargo_path):
        log_warn(f"{cargo_path} not found, falling back to wildcard version")
        return "*"
    text = open(cargo_path).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        log_warn(f"no version found in {cargo_path}, falling back to wildcard")
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
        log_info(f"swapped libbpf-cargo for scx_cargo {version}")
        return 1
    log_warn("libbpf-cargo not found in [build-dependencies]")
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
        log_info("stripped default-features = false from libbpf-rs (vendored libbpf)")
        return 1
    log_info("libbpf-rs already uses default features")
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
        log_info("reduced dependency versions to API boundaries")
        return 1
    log_info("dependency versions already normalized")
    return 0


def read_scx_utils_version(scx_root):
    """Read scx_utils version from the monorepo's rust/scx_utils/Cargo.toml."""
    cargo_path = os.path.join(scx_root, "rust", "scx_utils", "Cargo.toml")
    if not os.path.exists(cargo_path):
        log_warn(f"{cargo_path} not found, falling back to wildcard version")
        return "*"
    text = open(cargo_path).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        log_warn(f"no version found in {cargo_path}, falling back to wildcard")
        return "*"
    return m.group(1)


def add_scx_utils_dep(dst_root, scx_root):
    """Add scx_utils dependency to Cargo.toml [dependencies]."""
    version = read_scx_utils_version(scx_root)
    cargo_path = os.path.join(dst_root, "Cargo.toml")
    text = open(cargo_path).read()

    if "scx_utils" in text:
        log_info("scx_utils dependency already present")
        return 0

    scx_utils_line = f'scx_utils = {{ path = "../../../rust/scx_utils", version = "{version}" }}'
    # Insert after the [dependencies] header
    new_text = text.replace(
        "[dependencies]\n",
        f"[dependencies]\n{scx_utils_line}\n",
    )
    if new_text != text:
        open(cargo_path, "w").write(new_text)
        log_info(f"added scx_utils {version}")
        return 1
    log_warn("could not find the [dependencies] section")
    return 0


def patch_version_display(dst_root):
    """Patch main.rs to use scx_utils::build_id for version display.

    Replaces custom PANDEMONIUM_BUILD_ID/PANDEMONIUM_TARGET env vars with
    scx_utils::build_id::full_version(), matching other scx schedulers.
    """
    main_path = os.path.join(dst_root, "src", "main.rs")
    if not os.path.exists(main_path):
        log_warn("src/main.rs not found")
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
        log_info(f"patched the main.rs version display to scx_utils::build_id ({changes} changes)")
        return changes
    log_info("main.rs version display already patched")
    return 0


def patch_ops_version_suffix(dst_root):
    """Inject version suffix into struct_ops name for scx_loader GUI display.

    scx_loader reads /sys/kernel/sched_ext/root/ops to get the scheduler name
    and version. Without this, the version appears in logs but not in the GUI.
    Matches the pattern used by scx_cake, scx_flash, etc.
    """
    sched_path = os.path.join(dst_root, "src", "scheduler.rs")
    if not os.path.exists(sched_path):
        log_warn("src/scheduler.rs not found")
        return 0

    text = open(sched_path).read()

    if "ops_version_suffix" in text:
        log_info("ops version suffix already present")
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
        log_warn("could not find the open_skel anchor in scheduler.rs")
        return 0

    new_text = text.replace(old_anchor, inject_block)
    open(sched_path, "w").write(new_text)
    log_info("patched scheduler.rs with the ops version suffix (scx_loader GUI)")
    return 1


def patch_bpf_skel_include(dst_root):
    """Patch bpf_skel.rs include path for BpfBuilder output filename."""
    skel_path = os.path.join(dst_root, "src", "bpf_skel.rs")
    if not os.path.exists(skel_path):
        log_warn("src/bpf_skel.rs not found")
        return 0
    text = open(skel_path).read()
    new_text = text.replace("/bpf.skel.rs", "/main_skel.rs")
    if new_text != text:
        open(skel_path, "w").write(new_text)
        log_info("patched the bpf_skel.rs include path to main_skel.rs")
        return 1
    return 0


def add_workspace_member(scx_root):
    """Add scx_pandemonium to workspace Cargo.toml members if missing."""
    cargo_path = os.path.join(scx_root, "Cargo.toml")
    if not os.path.exists(cargo_path):
        log_warn(f"{cargo_path} not found, skipping workspace registration")
        return 0

    text = open(cargo_path).read()
    member_line = f'    "{DEST_REL}",'

    if DEST_REL in text:
        log_info("workspace member already registered")
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
        log_info(f"added {DEST_REL} to workspace members")
        return 1

    log_warn("could not find an insertion point in workspace Cargo.toml")
    return 0


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def upstream_remote(scx_root):
    """The remote that points at sched-ext/scx."""
    r = git(scx_root, "remote", "-v")
    for line in r.stdout.splitlines():
        name, url = line.split()[:2]
        if "sched-ext/scx" in url:
            return name
    log_error(f"no remote in {scx_root} points at sched-ext/scx")
    sys.exit(1)


def require_upstream_ported(scx_root, ported):
    """Refuse while a crate commit since our last upstream release is unported.

    The last release is the newest commit on the remote's main whose subject
    starts PANDEMONIUMv or scx_pandemoniumv and that touches the crate.
    """
    remote = upstream_remote(scx_root)
    r = git(scx_root, "fetch", "-q", remote, "main")
    if r.returncode != 0:
        log_error(f"git fetch {remote} main failed: {r.stderr.strip()}")
        sys.exit(1)
    main_ref = f"{remote}/main"
    r = git(scx_root, "log", main_ref, "-1", "--format=%h", "-i", "-E",
            "--grep=^(scx_)?pandemoniumv[0-9]", "--", DEST_REL)
    last = r.stdout.strip()
    if not last:
        log_error(f"no PANDEMONIUM release found on {main_ref}")
        sys.exit(1)
    r = git(scx_root, "log", "--no-merges", "--format=%h%x09%an%x09%s",
            f"{last}..{main_ref}", "--", DEST_REL)
    since = [line.split("\t") for line in r.stdout.splitlines()]
    unported = [c for c in since
                if not any(c[0].startswith(p) or p.startswith(c[0]) for p in ported)]
    log_info(f"last release {last}, {len(since)} crate commit(s) since")
    if unported:
        log_error(f"{len(unported)} upstream commit(s) touched the crate since "
                  f"{last}, and the export would revert them")
        log_error("port each into this tree, then rerun with --ported naming every hash")
        log.report("\n".join([f"{'HASH':<12}{'AUTHOR':<20}SUBJECT"]
                             + [f"{h:<12}{author:<20}{subject}"
                                for h, author, subject in unported]))
        log_info(f"REVIEW: git -C {scx_root} show HASH -- {DEST_REL}")
        sys.exit(1)


def require_catalog(scx_root):
    """scx's catalog lists scx_pandemonium featured or stable, and the profiles
    the crate ships pass the catalog's rules and take no name the catalog
    already uses. scxcli and scxtui start schedulers from this catalog, and its
    tier is how they present one."""
    try:
        profiles = scx_profiles()
    except (OSError, ValueError) as e:
        log_error(str(e))
        sys.exit(1)
    path = os.path.join(scx_root, "services", "catalog.json")
    if not os.path.exists(path):
        log_warn("services/catalog.json not found -- this scx predates the "
                 "catalog; the profiles ship in the crate regardless")
        return
    with open(path) as f:
        catalog = json.load(f)
    tiers = [t for t in ("featured", "stable", "experimental", "hidden")
             if CRATE_NEW in (catalog.get(t) or [])]
    if not ({"featured", "stable"} & set(tiers)):
        where = f"listed {'/'.join(tiers)} in" if tiers else "missing from"
        log_error(f"{CRATE_NEW} is {where} scx's catalog "
                  f"(services/catalog.json); scxcli and scxtui would not "
                  f"present it as stable. Settle that in scx first")
        sys.exit(1)
    taken = ({r.get("id") for r in catalog.get("recommended", [])}
             | {s for t in ("featured", "hidden", "stable", "experimental")
                for s in (catalog.get(t) or [])})
    clash = [p["id"] for p in profiles if p["id"] in taken]
    if clash:
        log_error(f"profile id(s) already taken in scx's catalog: {', '.join(clash)}")
        sys.exit(1)
    log_info(f"catalog: {CRATE_NEW} is {tiers[0]}; ships profile(s) "
             + ", ".join(p["id"] for p in profiles))


def run_checked(cmd, cwd, label):
    """Run a command; print its output tail and exit on failure."""
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        log_error(f"{label} failed: {' '.join(cmd)}")
        log.report("\n".join((r.stdout + r.stderr).strip().splitlines()[-20:]))
        sys.exit(1)
    log_info(f"{label}: ok")


def write_pr_body(pand_root):
    """The PR description: COMMIT_MESSAGE.txt without the subject or epigraph,
    one line per paragraph, bullet and section header. COMMIT_MESSAGE.txt is not
    in the published tree, so an export from a public clone has none to write."""
    message = os.path.join(pand_root, "COMMIT_MESSAGE.txt")
    if not os.path.exists(message):
        log_info("no COMMIT_MESSAGE.txt in this tree, so no PR body is written")
        return None
    lines = open(message).read().splitlines()
    # BODY STARTS AFTER THE EPIGRAPH'S ATTRIBUTION LINE, OR AFTER THE SUBJECT.
    start = next((i + 1 for i, l in enumerate(lines) if l.startswith("— ")), 1)
    blocks, items = [], []
    for line in lines[start:] + [""]:
        if not line.strip():
            if items:
                blocks.append(items)
                items = []
            continue
        # A BULLET STARTS AN ITEM; ANY OTHER LINE (A WRAPPED CONTINUATION, OR A
        # SECTION'S FILE LIST AFTER ITS HEADER) JOINS THE ITEM BEFORE IT.
        if line.startswith("- ") or not items:
            items.append(line)
        else:
            items[-1] += " " + line.strip()
    version = re.search(r'^version\s*=\s*"([^"]+)"',
                        open(os.path.join(pand_root, "Cargo.toml")).read(),
                        re.MULTILINE).group(1)
    out_dir = os.path.join("/tmp", CRATE_OLD)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"scx-pr-body-v{version}.md")
    open(out, "w").write("\n\n".join("\n".join(b) for b in blocks) + "\n")
    return out


def main():
    args = sys.argv[1:]
    ported = []
    if "--ported" in args:
        i = args.index("--ported")
        ported = [h for h in args[i + 1:] for h in h.split(",") if h]
        args = args[:i]
    if len(args) != 1:
        log_error(f"usage: {sys.argv[0]} /path/to/scx [--ported HASH ...]")
        sys.exit(1)

    scx_root = os.path.abspath(args[0])
    pand_root = os.path.dirname(os.path.abspath(__file__))
    dst_root = os.path.join(scx_root, DEST_REL)

    # VALIDATE
    if not os.path.isdir(scx_root):
        log_error(f"{scx_root} is not a directory")
        sys.exit(1)

    workspace_cargo = os.path.join(scx_root, "Cargo.toml")
    if not os.path.exists(workspace_cargo):
        log_error(f"{workspace_cargo} not found (is this an scx checkout?)")
        sys.exit(1)

    git_info = get_git_info()
    dirty = "-dirty" if git_info.get("dirty") else ""
    log_info(f"export_scx v{get_version()} [{git_info['commit']}{dirty}] "
             f"[scx={scx_root}]")
    log_info(f"TARGET: {dst_root}")

    # GUARD: NOTHING IS TOUCHED UNTIL IT PASSES.
    log.section("upstream: CHECK")
    require_upstream_ported(scx_root, ported)
    log.section("catalog: CHECK")
    require_catalog(scx_root)

    log.section("source: COPY")
    if os.path.exists(dst_root):
        shutil.rmtree(dst_root)
        log_info(f"removed the existing {DEST_REL}/")
    os.makedirs(dst_root, exist_ok=True)
    copied = copy_tree(pand_root, dst_root)
    assert_export_is_exactly_include(dst_root)

    log.section("crate: RENAME")
    renamed = rename_crate(dst_root)

    # BUILD SYSTEM: MATCH THE scx CONVENTION
    log.section("build system: PATCH")
    stripped = strip_profile_release(dst_root)
    replace_build_rs(dst_root)
    swap_build_deps(dst_root, scx_root)
    fix_libbpf_vendoring(dst_root)
    normalize_dep_versions(dst_root)
    patch_bpf_skel_include(dst_root)
    add_scx_utils_dep(dst_root, scx_root)
    patch_version_display(dst_root)
    patch_ops_version_suffix(dst_root)

    log.section("workspace: REGISTER")
    registered = add_workspace_member(scx_root)

    # FORMAT, LOCK (CI BUILDS WITH --locked), THEN THE CHECKS CI ENFORCES PLUS A
    # BUILD OF THE CRATE
    log.section("cargo: VERIFY")
    run_checked(["cargo", "fmt", "--manifest-path",
                 os.path.join(dst_root, "Cargo.toml")], scx_root, "cargo fmt")
    run_checked(["cargo", "update", "-p", CRATE_NEW], scx_root, "cargo update")
    run_checked(["cargo", "fmt", "--check", "-p", CRATE_NEW], scx_root,
                "cargo fmt --check")
    run_checked(["cargo", "build", "-p", CRATE_NEW, "--release"], scx_root,
                "cargo build")

    body = write_pr_body(pand_root)

    log.blank()
    log_info(f"EXPORT COMPLETE: {copied} files copied, {renamed} crate renames, "
             f"{stripped} profile stripped, {registered} workspace update")
    if body:
        log_info(f"PR BODY: {body}")
    log_info(f"NEXT: cd {scx_root} && git add -A && git diff --cached --stat")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
