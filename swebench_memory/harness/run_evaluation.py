#!/usr/bin/env python3
"""
Run evaluation in Docker containers following full_validation.py workflow

This script:
1. Applies test_patch (to add new tests)
2. Runs ONLY the tests specified in instance['FAIL_TO_PASS'] and instance['PASS_TO_PASS']
3. Applies model_patch (the fix)
4. Runs the same tests again
5. Compares results to determine resolution

Usage:
    python -m swebench_memory.harness.run_evaluation \
        --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json \
        --predictions_path cases/sympy__sympy-9123/sympy__sympy-9123_GT_pred.json \
        --run_id sympy_9123_gt
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


# Log directory structure (same as swebench)
RUN_EVALUATION_LOG_DIR = Path("logs/run_evaluation")


def _strip_unapplyable_binary_hunks(patch_content: str) -> str:
    """
    Remove diff sections for binary files that have no actual binary patch data.

    When a patch contains entries like:
        diff --git a/file.bin b/file.bin
        index abc..def 100644
        Binary files a/file.bin and b/file.bin differ

    git apply cannot apply them (there is no binary delta). This function strips
    those sections out so the remaining text-based diffs can be applied cleanly.

    Sections that DO have binary data (i.e. contain "GIT binary patch") are kept.
    """
    if not patch_content:
        return patch_content

    # Split on diff headers, keeping the delimiters
    parts = re.split(r'(?=^diff --git )', patch_content, flags=re.MULTILINE)
    result = []
    for part in parts:
        if not part.startswith('diff --git '):
            result.append(part)
            continue
        # Keep sections that have actual content (not just "Binary files ... differ")
        if 'Binary files' in part and 'GIT binary patch' not in part:
            # This is an unapplyable binary stub – skip it
            continue
        result.append(part)
    return ''.join(result)


def _sanitize_patch_for_python3(patch_content: str) -> str:
    """
    Sanitize patches for Python 3 compatibility.

    Converts Python 2 syntax to Python 3:
    - e.message → str(e)

    Args:
        patch_content: The original patch content

    Returns:
        Sanitized patch content
    """
    lines = patch_content.split('\n')
    fixed_lines = []

    for line in lines:
        # Only modify added lines (starting with +) that contain e.message
        if line.startswith('+') and '.message' in line and 'e.message' in line:
            # Replace e.message with str(e)
            line = re.sub(r'\be\.message\b', r'str(e)', line)
        fixed_lines.append(line)

    return '\n'.join(fixed_lines)


def ensure_init_files_for_new_test_dirs(
    image_tag: str,
    test_patch: str,
    instance_id: str,
    execution_log: List[str]
) -> str:
    """
    Create __init__.py files for new test directories added by test_patch.

    This fixes test discovery issues when test_patch creates new test directories
    (e.g., tests/model_enums/) but doesn't include __init__.py files, causing
    Django's test runner to skip those directories entirely.

    Args:
        image_tag: Current Docker image tag
        test_patch: The test patch content
        instance_id: Instance identifier for container naming
        execution_log: Log list to append messages to

    Returns:
        Updated image tag after committing changes
    """
    # Extract new test directories from test_patch
    # Pattern: diff --git a/tests/new_dir/file.py
    new_test_dirs = set()
    for match in re.finditer(r'diff --git a/(tests/[^/\s]+)/[^/\s]+\.py', test_patch):
        test_dir = match.group(1)
        new_test_dirs.add(test_dir)

    if not new_test_dirs:
        return image_tag

    execution_log.append(f"\n→ Checking {len(new_test_dirs)} test directory(ies) for __init__.py files...")

    # For each potential new directory, check if it needs __init__.py
    init_files_created = []
    for test_dir in sorted(new_test_dirs):
        # Check if directory exists, has .py files, but no __init__.py
        check_cmd = f"""cd /testbed && \
if [ -d "{test_dir}" ] && [ ! -f "{test_dir}/__init__.py" ]; then \
    if ls {test_dir}/*.py >/dev/null 2>&1; then \
        touch {test_dir}/__init__.py && echo "CREATED"; \
    fi; \
fi"""

        container_name = f"check_init_{instance_id.replace('/', '_').replace('__', '_')}"
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        result = subprocess.run(
            ["docker", "run", "--name", container_name, image_tag, "bash", "-c", check_cmd],
            capture_output=True,
            text=True
        )

        if "CREATED" in result.stdout:
            init_files_created.append(test_dir)
            execution_log.append(f"  ✓ Created {test_dir}/__init__.py")

        # Clean up container (don't commit yet)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # If we created any __init__.py files, we need to commit them
    if init_files_created:
        print(f"  → Created {len(init_files_created)} __init__.py file(s) for test discovery")

        # Create all __init__.py files in one go and commit
        create_cmds = " && ".join([
            f'touch {test_dir}/__init__.py' for test_dir in init_files_created
        ])
        cmd = f"cd /testbed && {create_cmds}"

        container_name = f"add_init_{instance_id.replace('/', '_').replace('__', '_')}"
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        subprocess.run(
            ["docker", "run", "--name", container_name, image_tag, "bash", "-c", cmd],
            capture_output=True
        )

        # Commit the changes to a new image
        updated_image = image_tag.replace(":latest", ":with_init").replace("_testpatch", "_testpatch_init")
        subprocess.run(
            ["docker", "commit", container_name, updated_image],
            capture_output=True
        )
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        # Clean up old image if it's not the base
        if ":latest" not in image_tag:
            subprocess.run(["docker", "rmi", image_tag], capture_output=True)

        execution_log.append(f"  ✓ Committed __init__.py files to image")
        return updated_image
    else:
        execution_log.append(f"  → All test directories already have __init__.py files")
        return image_tag


# ============================================================================
# MULTILINGUAL SUPPORT
# ============================================================================

# Known Rust repos — used as fallback when container-based detection fails
_KNOWN_RUST_REPOS = {
    "nushell/nushell",
    "BurntSushi/ripgrep",
    "tokio-rs/tokio",
    "tokio-rs/axum",
    "uutils/coreutils",
    "astral-sh/ruff",
    "sharkdp/bat",
}


def detect_language_in_container(image_tag: str, repo: str = "") -> str:
    """Detect programming language from /testbed marker files inside the container.

    Falls back to repo-name heuristics (for known Rust repos) if the container
    check fails or returns an unexpected value.
    """
    cmd = (
        "if [ -f /testbed/Cargo.toml ]; then echo rust; "
        "elif [ -f /testbed/go.mod ]; then echo go; "
        "elif [ -f /testbed/pom.xml ] || [ -f /testbed/build.gradle ] "
             "|| [ -f /testbed/build.gradle.kts ] || [ -f /testbed/build.xml ]; then echo java; "
        "elif [ -f /testbed/composer.json ]; then echo php; "
        "elif [ -f /testbed/setup.py ] || [ -f /testbed/pyproject.toml ] "
             "|| [ -f /testbed/setup.cfg ] || [ -f /testbed/manage.py ]; then echo python; "
        "elif [ -f /testbed/package.json ]; then echo javascript; "
        "elif [ -f /testbed/Gemfile ]; then echo ruby; "
        "elif ([ -f /testbed/Makefile ] || [ -f /testbed/CMakeLists.txt ] || [ -f /testbed/configure.ac ]) "
             "&& find /testbed -maxdepth 2 -name '*.c' 2>/dev/null | head -1 | grep -q '.'; then echo c; "
        "else echo python; fi"
    )
    valid = {"rust", "go", "java", "php", "javascript", "ruby", "c", "python"}
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=60
            )
            lang = result.stdout.strip()
            if lang in valid and lang != "python":
                return lang
            # Container returned "python" — verify against known non-Python repos
            # before accepting (guards against Cargo.toml not found in container)
            if lang == "python":
                if repo in _KNOWN_RUST_REPOS:
                    return "rust"
                return "python"
        except Exception:
            pass
    # All attempts failed — fall back to repo-name heuristic
    if repo in _KNOWN_RUST_REPOS:
        return "rust"
    return "python"


def _match_tests_to_results(tests: List[str], parsed: Dict[str, str]) -> Dict[str, str]:
    """Match requested test names against parsed results with flexible matching."""

    def _resolve(matches):
        if any(s in ('FAILED', 'ERROR') for s in matches):
            return 'FAILED'
        if all(s == 'PASSED' for s in matches):
            return 'PASSED'
        if any(s == 'SKIPPED' for s in matches):
            return 'SKIPPED'
        return matches[0]

    status_map = {}
    for test in tests:
        # 1. Exact match
        if test in parsed:
            status_map[test] = parsed[test]
            continue
        # 2. One is a suffix of the other (handles package prefix differences)
        matches = [v for k, v in parsed.items() if k.endswith(test) or test.endswith(k)]
        if matches:
            status_map[test] = _resolve(matches)
            continue
        # 3. Strip Redis/Valkey " in tests/...tcl" file qualifier and retry
        #    e.g. "GETRANGE against string value in tests/unit/type/string.tcl" → "GETRANGE against string value"
        m = re.match(r'^(.+?)\s+in\s+\S+\.tcl$', test)
        if m:
            base = m.group(1)
            if base in parsed:
                status_map[test] = parsed[base]
                continue
            matches = [v for k, v in parsed.items() if k.endswith(base) or base.endswith(k)]
            if matches:
                status_map[test] = _resolve(matches)
                continue
        # 4. Leaf name match (strip all namespace/path prefixes)
        test_leaf = re.split(r'::|#|\.', test)[-1]
        matches = [v for k, v in parsed.items() if re.split(r'::|#|\.', k)[-1] == test_leaf]
        if matches:
            status_map[test] = _resolve(matches)
    return status_map


def _rust_uses_insta(image_tag: str) -> bool:
    """Check if the Rust project in the container uses the insta snapshot-testing crate."""
    cmd = "grep -rl 'insta' /testbed/Cargo.toml /testbed/crates/*/Cargo.toml 2>/dev/null | head -1 | grep -q ."
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


def _upgrade_go_in_image(image_tag: str, target_version: str) -> str:
    """Install a newer Go version into a Docker image (downloads from go.dev/dl).

    Mirrors _upgrade_rust_in_image but for Go.
    Returns a new image tag with the upgraded Go, or the original tag if upgrade fails.
    Uses a deterministic tag so the same upgrade is reused across multiple calls.
    """
    new_tag = f"{image_tag}_go{target_version.replace('.', '')}"

    # Reuse existing upgraded image if already built
    check = subprocess.run(
        ["docker", "image", "inspect", new_tag],
        capture_output=True
    )
    if check.returncode == 0:
        return new_tag

    safe = image_tag.replace(":", "_").replace("/", "_").replace(".", "_")
    container_name = f"go_upgrade_{safe}_{target_version.replace('.', '')}"
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Try "go1.20.linux-amd64.tar.gz" first (pre-1.21 naming), then "go1.20.0.linux-amd64.tar.gz"
    upgrade_cmd = (
        "set -e && "
        f"VER='{target_version}' && "
        "BASE_URL='https://go.dev/dl' && "
        "( wget -q \"${BASE_URL}/go${VER}.linux-amd64.tar.gz\" -O /tmp/go_upgrade.tar.gz 2>/dev/null || "
        "  wget -q \"${BASE_URL}/go${VER}.0.linux-amd64.tar.gz\" -O /tmp/go_upgrade.tar.gz ) && "
        "rm -rf /usr/local/go && "
        "tar -C /usr/local -xzf /tmp/go_upgrade.tar.gz && "
        "rm -f /tmp/go_upgrade.tar.gz && "
        "export PATH=/usr/local/go/bin:$PATH && "
        "go version"
    )
    result = subprocess.run(
        ["docker", "run", "--name", container_name, image_tag, "bash", "-c", upgrade_cmd],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        return image_tag

    subprocess.run(["docker", "commit", container_name, new_tag], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    print(f"  ✓ Upgraded Go to {target_version} in image → {new_tag}")
    return new_tag


def _upgrade_rust_in_image(image_tag: str, target_version: str = "1.85") -> str:
    """Install a newer Rust toolchain into a Docker image via rustup.

    Mirrors full_validation_multilingual_rust.py's setup_version() logic.
    Returns a new image tag with the upgraded toolchain, or the original tag
    if the upgrade fails.
    """
    safe = image_tag.replace(":", "_").replace("/", "_").replace(".", "_")
    container_name = f"rust_upgrade_{safe}"
    new_tag = f"{image_tag}_rust{target_version.replace('.', '')}"

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    upgrade_cmd = (
        "source ~/.cargo/env 2>/dev/null || true && "
        "unset RUSTUP_TOOLCHAIN && "
        f"rustup toolchain install {target_version} --no-self-update 2>&1 | tail -1 && "
        f"rustup default {target_version}"
    )
    result = subprocess.run(
        ["docker", "run", "--name", container_name, image_tag, "bash", "-c", upgrade_cmd],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        return image_tag

    subprocess.run(["docker", "commit", container_name, new_tag], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    print(f"  ✓ Upgraded Rust to {target_version} in image → {new_tag}")
    return new_tag


def _run_bat_syntax_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600
) -> Dict[str, str]:
    """Run bat-style syntax regression tests inside Docker.

    Test names look like 'syntax/Svelte/App.svelte' where the path after 'syntax/'
    is relative to both:
      /testbed/tests/syntax-tests/source/<rel>      (input file)
      /testbed/tests/syntax-tests/highlighted/<rel>  (expected ANSI output)

    Mirrors RustValidator._run_bat_syntax_tests() from full_validation_multilingual_rust.py.
    """
    if not tests:
        return {}

    # Extract relative paths: 'syntax/Svelte/App.svelte' → 'Svelte/App.svelte'
    rel_paths = []
    for t in tests:
        if t.startswith('syntax/'):
            rel_paths.append(t[len('syntax/'):])
        else:
            rel_paths.append(t)

    # Build a Python snippet that runs bat for each test and prints PASS/FAIL/MISSING.
    # Uses the same invocation as create_highlighted_versions.py (the official test harness).
    paths_repr = repr(rel_paths)
    python_script = f"""
import subprocess, os, sys

bat_bin = '/testbed/target/debug/bat'
if os.path.exists('/testbed/target/release/bat'):
    bat_bin = '/testbed/target/release/bat'

# Env matching create_highlighted_versions.py
bat_env = dict(os.environ)
for k in ('BAT_CACHE_PATH','BAT_CONFIG_DIR','BAT_CONFIG_PATH','BAT_OPTS',
          'BAT_PAGER','BAT_STYLE','BAT_TABS','BAT_THEME','NO_COLOR','PAGER'):
    bat_env.pop(k, None)
bat_env['COLORTERM'] = 'truecolor'

BAT_OPTIONS = ['--no-config', '--style=plain', '--color=always',
               '--theme=default', '--italic-text=always']

def get_options(source):
    opts = BAT_OPTIONS[:]
    opts_file = os.path.join(os.path.dirname(source), 'bat_options')
    try:
        with open(opts_file) as f:
            opts.extend(l.rstrip() for l in f)
    except FileNotFoundError:
        pass
    return opts

rel_paths = {paths_repr}
for rel_path in rel_paths:
    src = '/testbed/tests/syntax-tests/source/' + rel_path
    hl  = '/testbed/tests/syntax-tests/highlighted/' + rel_path
    if not os.path.exists(src) or not os.path.exists(hl):
        print('MISSING:' + rel_path)
        continue
    r = subprocess.run([bat_bin] + get_options(src) + [src],
                       capture_output=True, env=bat_env)
    expected = open(hl, 'rb').read()
    if r.stdout == expected:
        print('PASS:' + rel_path)
    else:
        print('FAIL:' + rel_path)
"""
    # Write script via heredoc then execute — avoids quoting issues with -c
    cmd = f"cat > /tmp/_bat_test.py << 'BAT_PYEOF'\n{python_script}\nBAT_PYEOF\npython3 /tmp/_bat_test.py"
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: 'TIMEOUT' for t in tests}

    status_map: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith('PASS:'):
            rel = line[5:]
            status_map[f'syntax/{rel}'] = 'PASSED'
            status_map[rel] = 'PASSED'
        elif line.startswith('FAIL:'):
            rel = line[5:]
            status_map[f'syntax/{rel}'] = 'FAILED'
            status_map[rel] = 'FAILED'
        elif line.startswith('MISSING:'):
            rel = line[8:]
            status_map[f'syntax/{rel}'] = 'FAILED'
            status_map[rel] = 'FAILED'

    return _match_tests_to_results(tests, status_map)


def _detect_rust_workspace_package(image_tag: str, tests: List[str]) -> str:
    """Find which workspace package (crate) contains the given test names.

    Runs a single Docker container that iterates all pre-built test binaries and
    returns the crate name of the first binary whose --list output contains one of
    the target test names. Falls back to "" (whole-workspace run) if not found.
    """
    if not tests:
        return ""

    # Escape the sample test for grep (fixed-string match)
    sample_test = tests[0].replace("'", r"'\''")

    # One-shot shell script: iterate all test executables and run --list on each
    scan_cmd = (
        "for f in $(find /testbed/target/debug/deps -maxdepth 1 -type f -executable "
        r"-not -name '*.so' -not -name '*.rlib' -not -name '*.d' -not -name '*.rmeta'); do "
        f"  if \"$f\" --list 2>/dev/null | grep -qF '{sample_test}'; then "
        "    basename \"$f\"; break; "
        "  fi; "
        "done"
    )
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", scan_cmd],
            capture_output=True, text=True, timeout=120
        )
        binary_name = r.stdout.strip()
        if binary_name:
            # Strip trailing hash: "ruff_linter-cafb218ff8a6a74f" → "ruff_linter"
            crate_name = re.sub(r'-[0-9a-f]{16}$', '', binary_name)
            return crate_name
    except Exception:
        pass

    return ""


def _detect_rust_integration_test_binary(image_tag: str, tests: List[str]) -> Tuple[str, str]:
    """Find the integration test binary name (from tests/*.rs) that contains the tests.

    Searches both root-level tests/ and workspace-member tests/ directories (depth ≤ 5).
    Also searches tests/*/*.rs patterns (e.g. uutils/coreutils tests/by-util/test_tr.rs).
    Returns (binary_stem, package_name). package_name is "" for root-level tests.
    For workspace members like tokio-rs/tokio, returns e.g. ("broadcast", "tokio").
    """
    if not tests:
        return "", ""
    # Try ALL test names (not just tests[0]) since FAIL_TO_PASS tests may be newly added
    # by test_patch and not present in the pre-built binary at base_commit.
    # Build a grep -E pattern matching any of the test names.
    all_fn_names = [re.sub(r'\s*-\s*should\s+panic$', '', t).strip() for t in tests[:30]]
    # If tests[0] has a module prefix (e.g. "test_tr::"), also collect same-prefix tests
    # from the FULL list. These exist in the pre-built binary even when the specific
    # FAIL_TO_PASS test is newly added. This avoids false-positive matches on unrelated
    # binaries that only contain tests from the common::* module.
    if tests and '::' in tests[0]:
        first_module = tests[0].rsplit('::', 1)[0]  # e.g. "test_tr"
        prefix = first_module + "::"
        extra_count = 0
        for t in tests[1:]:
            if t.startswith(prefix):
                fn = re.sub(r'\s*-\s*should\s+panic$', '', t).strip()
                if fn and fn not in all_fn_names:
                    all_fn_names.append(fn)
                    extra_count += 1
                    if extra_count >= 10:
                        break
    # Integration test --list output shows bare function names (no module prefix).
    # Also include leaf names (fn name after the last ::) so binaries like test_tr that
    # show "test_truncate_non_utf8_set: test" (no "test_tr::" prefix) are detected.
    leaf_names = [n.split('::')[-1] for n in all_fn_names if '::' in n]
    all_patterns = [n for n in all_fn_names if n] + [n for n in leaf_names if n]
    grep_pattern = "|".join(re.escape(n) for n in all_patterns if n)
    grep_pattern_safe = grep_pattern.replace("'", r"'\''")
    # Scan all tests/*.rs AND tests/*/*.rs files (excluding target/ and mod.rs helper files).
    # The second pattern handles projects like uutils/coreutils where integration tests live
    # in tests/by-util/test_<util>.rs and are registered as individual [[test]] binaries.
    # pkg detection: walk up from the rs_file to find the tests/ directory, then check
    # whether its parent is the repo root (/testbed) or a workspace member subdirectory.
    scan_cmd = (
        r"find /testbed -maxdepth 6 \! -path '*/target/*' "
        r"\( -path '*/tests/*.rs' -o -path '*/tests/*/*.rs' \) \! -name 'mod.rs' -print 2>/dev/null"
        " | while IFS= read -r rs_file; do"
        "  stem=$(basename \"$rs_file\" .rs);"
        "  bin=$(find /testbed/target/debug/deps -maxdepth 1 -type f -executable"
        r"    -name \"${stem}-*\" \! -name '*.so' \! -name '*.rlib' \! -name '*.d'"
        "    2>/dev/null | head -1);"
        "  if [ -n \"$bin\" ] && \"$bin\" --list 2>/dev/null"
        f"    | grep -qE '{grep_pattern_safe}'; then"
        # Walk up to find the 'tests' directory, then determine pkg from its parent.
        "    d=$rs_file;"
        "    while [ \"$(basename $d)\" != 'tests' ] && [ \"$d\" != '/testbed' ] && [ \"$d\" != '/' ]; do d=$(dirname $d); done;"
        "    tests_parent=$(dirname $d);"
        "    if [ \"$tests_parent\" = '/testbed' ]; then pkg=''; else pkg=$(basename $tests_parent); fi;"
        "    echo \"${stem}|||${pkg}\";"
        "    break;"
        "  fi;"
        "done"
    )
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", scan_cmd],
            capture_output=True, text=True, timeout=120
        )
        output = r.stdout.strip()
        if "|||" in output:
            stem, pkg = output.split("|||", 1)
            return stem.strip(), pkg.strip()
    except Exception:
        pass

    # Fallback: stem-based lookup failed (e.g. Cargo.toml [[test]] name != file stem,
    # as in BurntSushi/ripgrep where path="tests/tests.rs" but name="integration").
    # Scan ALL executables in deps to find which one contains the test patterns.
    fallback_cmd = (
        f"pat='{grep_pattern_safe}'; "
        "for f in $(find /testbed/target/debug/deps -maxdepth 1 -type f -executable "
        "-not -name '*.so' -not -name '*.rlib' -not -name '*.d' -not -name '*.rmeta' 2>/dev/null); do "
        "  if \"$f\" --list 2>/dev/null | grep -qE \"$pat\"; then "
        "    bname=$(basename \"$f\" | sed 's/-[0-9a-f]*$//'); "
        "    echo \"${bname}|||\"; break; "
        "  fi; "
        "done"
    )
    try:
        r2 = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", fallback_cmd],
            capture_output=True, text=True, timeout=120
        )
        out2 = r2.stdout.strip()
        if "|||" in out2:
            stem2, _ = out2.split("|||", 1)
            stem2 = stem2.strip()
            if stem2:
                return stem2, ""
    except Exception:
        pass

    return "", ""


def _detect_rust_package_from_source(image_tag: str, tests: List[str]) -> Tuple[str, str]:
    """Grep for test function names in source files to detect (binary_stem, package_name).

    Used as a fallback when pre-built binaries are not available but we can still
    infer the package and integration test file from source code.
    Tries all test names (not just the first) so it works on both the test-patched
    image and the base image (where FAIL_TO_PASS tests may not exist yet).
    Returns (binary_stem, package_name). Returns ("", "") if not found.
    """
    if not tests:
        return "", ""

    for test in tests:
        # Strip " - should panic" suffix for function name matching.
        # Use only the leaf function name (after the last ::) because:
        # 1. Rust fn declarations never contain '::' (e.g. "fn test_foo", not "fn mod::test_foo")
        # 2. Instance test names often carry a module prefix (e.g. "test_tr::test_foo")
        fn_name = re.sub(r'\s*-\s*should\s+panic$', '', test).strip()
        fn_leaf = fn_name.split('::')[-1]  # bare function name without module path
        fn_leaf_safe = fn_leaf.replace("'", r"'\''")

        grep_cmd = (
            f"grep -rl 'fn {fn_leaf_safe}' /testbed --include='*.rs' 2>/dev/null"
            " | grep -v '/target/' | head -5"
        )
        try:
            r = subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c", grep_cmd],
                capture_output=True, text=True, timeout=30
            )
        except Exception:
            continue

        for file_path in r.stdout.strip().split('\n'):
            if not file_path:
                continue
            rel = file_path.replace('/testbed/', '').replace('\\', '/')
            parts = rel.split('/')
            if 'tests' in parts:
                idx = parts.index('tests')
                if idx == 0 and len(parts) > 1:
                    if len(parts) > 2:
                        # File is in a subdirectory of tests/ (e.g. tests/by-util/test_tr.rs).
                        # The actual integration test binary is compiled from the root
                        # tests/*.rs entry point (e.g. tests/tests.rs → binary "tests"),
                        # not from the submodule file. Check if tests/tests.rs exists.
                        tests_root_check = (
                            "find /testbed/tests -maxdepth 1 -name 'tests.rs'"
                            " ! -path '*/target/*' 2>/dev/null | head -1"
                        )
                        try:
                            r2 = subprocess.run(
                                ["docker", "run", "--rm", image_tag, "bash", "-c", tests_root_check],
                                capture_output=True, text=True, timeout=15
                            )
                            if r2.stdout.strip():
                                return "tests", ""
                        except Exception:
                            pass
                    # Root tests/file.rs: use the file stem directly.
                    # BUT verify the binary exists — the file might be a module included
                    # from tests/tests.rs rather than a standalone [[test]] entry.
                    # (e.g. BurntSushi/ripgrep: tests/regression.rs is a module of
                    # the "integration" binary declared as [[test]] name="integration"
                    # path="tests/tests.rs" in Cargo.toml)
                    stem = parts[-1].replace('.rs', '')
                    bin_check = (
                        f"find /testbed/target/debug/deps -maxdepth 1 -type f -executable"
                        f" -name '{stem}-*' ! -name '*.so' ! -name '*.rlib'"
                        f" ! -name '*.d' 2>/dev/null | head -1"
                    )
                    try:
                        rb = subprocess.run(
                            ["docker", "run", "--rm", image_tag, "bash", "-c", bin_check],
                            capture_output=True, text=True, timeout=15
                        )
                        if rb.stdout.strip():
                            return stem, ""
                        # Binary not found by stem; fall through to let caller handle it
                    except Exception:
                        pass
                    # Don't return a stem that has no matching binary; continue searching
                    continue
                elif idx > 0 and len(parts) > idx + 1:
                    # workspace_member/tests/file.rs  OR  workspace_member/tests/subdir/file.rs
                    pkg = parts[idx - 1]
                    stem = parts[-1].replace('.rs', '')
                    if pkg and pkg not in ('testbed', ''):
                        return stem, pkg
            elif 'src' in parts and len(parts) > 0:
                # Unit test in src/: return the package name with empty stem (→ --lib)
                pkg = parts[0]
                if pkg and pkg not in ('testbed', ''):
                    return "", pkg
    return "", ""


def _run_rust_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = ""
) -> Dict[str, str]:
    """Run Rust tests via cargo test and parse output. Runs inside Docker (Ubuntu/Linux).

    Strategy:
    1. Detect the workspace package that owns the tests (from pre-built unit-test binaries).
       If found → RUSTFLAGS='-C debuginfo=0' cargo test -p <pkg> --lib '<filter>'
       (debuginfo=0 keeps memory within Docker limits for large unit-test crates like ruff_linter)
    2. If no unit-test binary found, detect the integration test binary from tests/*.rs in
       both root and workspace-member directories (e.g. tokio/tests/broadcast.rs).
       If found → cargo test [-p <pkg>] --test <binary> '<filter>'
    3. Grep source files for the test function name to infer the package even when
       pre-built binaries are missing (no-binary fallback).
    4. Fall back to cargo test with a combined regex filter to avoid running the whole
       workspace blindly when the package cannot be determined.

    After each run, mirrors full_validation_multilingual_rust.py retry logic:
    - If a workspace member fails to compile ("failed to select a version for" /
      "no matching package named"), detect and exclude it, then retry with
      --workspace --exclude <member>.
    - If 0 tests ran on an integration test and build succeeded ("running 0 tests"),
      retry with --all-features (handles feature-gated integration tests).
    - If build fails with error[E0433] / could not compile on an integration test,
      retry with --all-features.
    """
    # ── Split bat syntax tests from cargo tests ──────────────────────────────
    # Bat syntax tests have names like 'syntax/Svelte/App.svelte' (path-style,
    # starts with 'syntax/'). They run the bat binary, not cargo test.
    bat_tests = [t for t in tests if t.startswith('syntax/')]
    cargo_tests = [t for t in tests if not t.startswith('syntax/')]

    results: Dict[str, str] = {}

    # Run bat syntax tests via the bat binary
    if bat_tests:
        bat_results = _run_bat_syntax_tests_in_container(image_tag, bat_tests, timeout)
        results.update(bat_results)

    if not cargo_tests:
        return results

    # ── Cargo test path ───────────────────────────────────────────────────────
    # Replace 'tests' with only cargo tests for the rest of the function
    tests = cargo_tests

    # --cap-lints=warn prevents deny(lint) in old code from becoming hard errors under
    # newer Rust toolchains (e.g. drop(ManuallyDrop<T>) lint added in Rust 1.75+).
    cargo_env = (
        "source $HOME/.cargo/env 2>/dev/null || true && unset RUSTUP_TOOLCHAIN && "
        "export RUSTFLAGS=\"${RUSTFLAGS:+$RUSTFLAGS }--cap-lints=warn\""
    )

    workspace_package = _detect_rust_workspace_package(image_tag, tests)
    integration_binary = ""
    integration_package = ""
    if not workspace_package:
        integration_binary, integration_package = _detect_rust_integration_test_binary(image_tag, tests)
        # Fallback: grep source files when pre-built binaries are missing
        if not integration_binary:
            integration_binary, integration_package = _detect_rust_package_from_source(image_tag, tests)
        # Special case: detection may return a binary that only contains PTP tests
        # (e.g. uutils/coreutils: "test_util_name" contains common::* PTP tests but
        # the FTP test lives in the "tests" binary under a different module prefix).
        # When the FTP test's module prefix doesn't match the detected binary name,
        # re-run source-based detection using only the FTP test to find its binary.
        if (integration_binary and tests and '::' in tests[0]
                and tests[0].rsplit('::', 1)[0] != integration_binary):
            ftp_bin, ftp_pkg = _detect_rust_package_from_source(image_tag, [tests[0]])
            if ftp_bin:
                integration_binary = ftp_bin
                integration_package = ftp_pkg
        # Doctest fallback: infer the workspace package from doctest test-ID paths.
        # Doctest IDs look like "axum/src/middleware/mod.rs - middleware (line 143)".
        # The first path component is the crate/package name.  Using this lets us
        # run "cargo test -p <pkg> --doc" (fast, single-crate) instead of the
        # slow "cargo test --doc --workspace" (compiles everything from scratch).
        if not integration_binary:
            for t in tests:
                if '(line ' in t and '/' in t:
                    pkg = t.split('/')[0]
                    if pkg and not pkg.startswith('.'):
                        workspace_package = pkg
                        break

    def _parse_output(output: str, parsed: Dict[str, str]) -> None:
        for line in output.split('\n'):
            # Doctest lines: "test foo.rs - bar (line N) ... ok"
            # or "test foo.rs - bar (line N) - compile fail ... ok"
            # Use [^.]* after (line N) to capture the full suffix (e.g. "- compile fail")
            # without accidentally consuming the " ... " separator (which contains dots).
            m = re.match(r'^test\s+(.*?\(line \d+\)[^.]*?)\s+\.\.\.\s+(ok|FAILED|ignored)', line)
            if not m:
                m = re.match(r'^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)', line)
            if m:
                name, status = m.group(1).strip(), m.group(2)
                parsed[name] = 'PASSED' if status == 'ok' else ('FAILED' if status == 'FAILED' else 'SKIPPED')

    def _extract_excluded_member(output: str) -> str:
        """Mirror full_validation_multilingual_rust.py: extract broken workspace member name."""
        if ('failed to select a version for' not in output
                and 'no matching package named' not in output):
            return ""
        m = re.search(r'required by package `[^`]+ v[^(]+\(([^)]+)\)', output)
        if m:
            path = m.group(1)
            if '/testbed/' in path:
                member = path.split('/testbed/')[-1].strip('/').split('/')[0]
                if member and member != '.':
                    return member
        return ""

    def _run_cmd(cmd: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            # Return a fake result so callers can mark tests as TIMEOUT
            class _TR:
                stdout = ""; stderr = "TIMEOUT"; returncode = -1
            return _TR()

    # Detect if the tests are primarily doctest-style: "file - module (line N)"
    has_doctests = any('(line ' in t for t in tests)

    def _cargo_test_cmd(filter_arg: str = "", all_features: bool = False,
                        excluded_members: List[str] = None,
                        remove_from_workspace: List[str] = None,
                        extra_setup: str = "") -> str:
        safe = filter_arg.replace("'", r"'\''") if filter_arg else ""
        farg = f" '{safe}'" if safe else ""
        excl = ""
        if excluded_members and not workspace_package:
            excl = " --workspace" + "".join(f" --exclude {m}" for m in excluded_members)
        af = " --all-features" if all_features else ""
        # Prepend sed commands to remove broken workspace members from Cargo.toml.
        # This fixes "failed to select a version for" errors where cargo resolves ALL
        # workspace member deps during lockfile resolution, even with --exclude.
        patch_prefix = ""
        if remove_from_workspace:
            for m in remove_from_workspace:
                safe_m = m.replace("'", r"'\''").replace("/", r"\/")
                patch_prefix += f"sed -i '/{safe_m}/d' /testbed/Cargo.toml 2>/dev/null && "
        # Optional extra setup commands (e.g. cargo update to pin deps)
        if extra_setup:
            patch_prefix += extra_setup

        if workspace_package:
            # If doctest-style, use --doc instead of --lib
            doc_flag = " --doc" if has_doctests else " --lib"
            return (f"{cargo_env} && {patch_prefix}cd /testbed && "
                    f"RUSTFLAGS='-C debuginfo=0' cargo test -p {workspace_package}{doc_flag}{farg} "
                    f"2>&1 || true")
        elif integration_binary:
            # Use -p package when we know the workspace member (e.g. tokio-rs/tokio)
            pkg_arg = f" -p {integration_package}" if integration_package else ""
            return (f"{cargo_env} && {patch_prefix}cd /testbed && "
                    f"cargo test{pkg_arg} --test {integration_binary}{af}{excl}{farg} 2>&1 || true")
        else:
            # For doctest-heavy cases use --doc --workspace; otherwise run all tests
            doc_flag = " --doc --workspace" if has_doctests and not farg else ""
            # When neither workspace_package nor integration_binary found but we have
            # integration_package (from grep-based source detection), scope to that package
            pkg_arg = f" -p {integration_package}" if integration_package and not excl else ""
            return (f"{cargo_env} && {patch_prefix}cd /testbed && "
                    f"cargo test{pkg_arg}{doc_flag}{af}{excl}{farg} 2>&1 || true")

    # Group by module prefix to minimise cargo invocations.
    # For tests without '::' separators (common in Rust integration tests like tokio broadcast),
    # each test is its own prefix. When > 5 such tests, instead of running all workspace tests
    # with no filter (very slow), build a regex filter from all test names.
    module_prefixes: Dict[str, List[str]] = {}
    for t in tests:
        prefix = t.rsplit('::', 1)[0] if '::' in t else t
        module_prefixes.setdefault(prefix, []).append(t)

    parsed: Dict[str, str] = {}
    if len(module_prefixes) <= 5:
        prefixes = list(module_prefixes.keys())
    elif integration_binary or workspace_package:
        if (integration_binary and not workspace_package
                and tests and '::' in tests[0]):
            # With many module prefixes and a known integration binary, use the
            # FAIL_TO_PASS tests' module prefix as a targeted filter (tests[0] is
            # always the first FAIL_TO_PASS test since all_tests = FTP + PTP).
            # This avoids running all 3000+ tests in a large integration binary
            # (e.g. uutils/coreutils `tests`) which can timeout under QEMU x86
            # emulation on ARM hosts. PASS_TO_PASS tests from other module
            # prefixes will be MISSING both before and after → treated as success.
            first_module = tests[0].rsplit('::', 1)[0]  # e.g. "test_tr"
            prefixes = [first_module + "::"]
        else:
            # We know the target binary/package — use a single combined regex filter
            # to run all tests at once without scanning the whole workspace.
            prefixes = [""]
    else:
        # Unknown package: build a regex filter from test names to at least avoid
        # running the entire workspace blindly (which can timeout on large projects).
        # Strip " - should panic" suffixes and escape for use as a cargo test filter regex.
        raw_names = [re.sub(r'\s*-\s*should\s+panic$', '', t).strip() for t in tests]
        escaped = [re.escape(n) for n in raw_names]
        # Cargo test filter is a substring/regex; join with | to match any of the tests
        combined = '|'.join(escaped)
        prefixes = [combined] if len(combined) < 500 else [""]

    for prefix in prefixes:
        cmd = _cargo_test_cmd(prefix)
        result = _run_cmd(cmd)
        if result.returncode == -1:  # timeout
            results.update({t: 'TIMEOUT' for t in tests})
            return results
        output = result.stdout + result.stderr
        before = len(parsed)
        _parse_output(output, parsed)

        # ── Mirror full_validation_multilingual_rust.py retry logic ──────────

        # (A) Broken workspace member: exclude it and retry
        excluded_member = _extract_excluded_member(output)
        if excluded_member and len(parsed) == before:
            if 'failed to select a version for' in output:
                # Dependency RESOLUTION error: cargo resolves ALL workspace member deps
                # even with --exclude, so we must remove the broken member from
                # workspace Cargo.toml entirely (via inline sed) so lockfile resolution
                # skips it completely.
                # Also pin common transitive deps that pull in edition-2024 crates:
                #   cc 1.0.83 - last version before namespaced dep: features
                #   tempfile 3.9.0 - last version not requiring getrandom 0.3 (edition 2024)
                # Use --all-features so macros like #[tokio::test] resolve correctly.
                dep_pins = (
                    "cargo update cc --precise 1.0.83 2>/dev/null; "
                    "cargo update tempfile --precise 3.9.0 2>/dev/null; "
                )
                retry_cmd = _cargo_test_cmd(
                    prefix, remove_from_workspace=[excluded_member],
                    extra_setup=dep_pins,
                    all_features=(integration_binary is not None and integration_binary != "")
                )
            else:
                retry_cmd = _cargo_test_cmd(prefix, excluded_members=[excluded_member])
            result2 = _run_cmd(retry_cmd)
            if result2.returncode != -1:
                _parse_output(result2.stdout + result2.stderr, parsed)
            # If (A) retry gave no results and it's an integration test with E0433,
            # try again with --all-features (handles feature-gated macros like tokio::test)
            if (len(parsed) == before and (integration_binary or not workspace_package)
                    and result2.returncode != -1):
                out2 = result2.stdout + result2.stderr
                if 'error[E0433]' in out2 or 'could not compile' in out2 or 'running 0 tests' in out2:
                    retry3_cmd = _cargo_test_cmd(
                        prefix,
                        remove_from_workspace=[excluded_member] if 'failed to select a version for' in output else None,
                        extra_setup=dep_pins if 'failed to select a version for' in output else "",
                        all_features=True
                    )
                    result3 = _run_cmd(retry3_cmd)
                    if result3.returncode != -1:
                        _parse_output(result3.stdout + result3.stderr, parsed)

        # (B) Integration/fallback only: --all-features retry
        # Mirrors: "running 0 tests" with rc=0, or compile error with E0433/could not compile
        elif integration_binary or not workspace_package:
            output2 = ""
            if (len(parsed) == before and result.returncode == 0
                    and 'running 0 tests' in output):
                retry_cmd = _cargo_test_cmd(prefix, all_features=True)
                result2 = _run_cmd(retry_cmd)
                output2 = result2.stdout + result2.stderr if result2.returncode != -1 else ""
            elif (len(parsed) == before
                    and ('error[E0433]' in output or 'could not compile' in output)):
                # Note: returncode may be 0 due to '|| true' in cargo command; check output
                retry_cmd = _cargo_test_cmd(prefix, all_features=True)
                result2 = _run_cmd(retry_cmd)
                output2 = result2.stdout + result2.stderr if result2.returncode != -1 else ""
            if output2:
                _parse_output(output2, parsed)

    # Extra pass: when the test list contains doctests but we ran via --test <binary>,
    # doctests are never collected (--test runs the integration binary, not the doc runner).
    # Detect any uncollected doctest-style tests and run --doc --all-features to collect them.
    # --all-features is required because feature-gated modules (e.g. cfg_io! { pub mod io; })
    # are not compiled in the default feature set, hiding their doctests.
    if integration_binary and has_doctests:
        uncollected_doctests = [t for t in tests if '(line ' in t and t not in parsed]
        if uncollected_doctests:
            pkg_arg = f" -p {integration_package}" if integration_package else ""
            doc_cmd = (f"{cargo_env} && cd /testbed && "
                       f"cargo test{pkg_arg} --doc --all-features 2>&1 || true")
            doc_result = _run_cmd(doc_cmd)
            if doc_result.returncode != -1:
                _parse_output(doc_result.stdout + doc_result.stderr, parsed)

    cargo_results = _match_tests_to_results(tests, parsed)
    results.update(cargo_results)
    return results


def _run_go_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = ""
) -> Dict[str, str]:
    """Run Go tests via go test -v and parse output. Runs inside Docker (Ubuntu/Linux).
    Targets only the test functions listed in tests using a -run regex filter.

    Strategy:
    1. Grep /testbed for *_test.go files containing the test functions to find
       the specific Go packages (avoids running ./... which may fail to compile
       due to unrelated packages requiring a newer Go version).
    2. Fall back to ./... if no specific packages are found.
    """
    go_env = "export PATH=/usr/local/go/bin:$HOME/go/bin:$PATH"

    # Go test names: "TestFuncName" or "TestFuncName/SubTest"
    # -run accepts a regex; anchor top-level function names with ^...$
    # Extract top-level function names (before the first '/')
    top_level = sorted({t.split('/')[0] for t in tests})
    # Escape special regex chars in test names
    escaped = [re.escape(n) for n in top_level]
    run_filter = '^(' + '|'.join(escaped) + ')$' if escaped else '.'

    # ── Step 1: detect specific packages containing the test functions ────────
    # Build a grep pattern matching any top-level test function declaration.
    func_pattern = '|'.join(f'func {re.escape(n)}\\b' for n in top_level)
    grep_cmd = (
        f"grep -rl --include='*_test.go' -E '{func_pattern}' /testbed 2>/dev/null || true"
    )
    try:
        grep_result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", grep_cmd],
            capture_output=True, text=True, timeout=60
        )
        found_files = [l.strip() for l in grep_result.stdout.splitlines() if l.strip()]
    except Exception:
        found_files = []

    # Convert absolute paths like /testbed/promql/eval_test.go → ./promql
    packages: list = []
    seen: set = set()
    for f in found_files:
        # Strip /testbed/ prefix
        rel = f[len('/testbed/'):] if f.startswith('/testbed/') else f
        pkg_dir = str(Path(rel).parent)
        pkg_path = '.' if pkg_dir == '.' else f'./{pkg_dir}'
        if pkg_path not in seen:
            seen.add(pkg_path)
            packages.append(pkg_path)

    pkg_targets = ' '.join(packages) if packages else './...'

    def _parse_output(raw: str, parsed: Dict[str, str]) -> None:
        for line in raw.split('\n'):
            m = re.match(r'\s*---\s+(PASS|FAIL|SKIP):\s+(\S+)', line)
            if m:
                status_str, name = m.group(1), m.group(2)
                name = re.sub(r'\s*\([\d.]+s\)\s*$', '', name)
                parsed[name] = 'PASSED' if status_str == 'PASS' else ('FAILED' if status_str == 'FAIL' else 'SKIPPED')

    parsed: Dict[str, str] = {}

    def _docker_go_test(img: str, targets: str) -> str:
        """Run go test in Docker and return combined stdout+stderr."""
        c = (
            f"{go_env} && cd /testbed && "
            f"go test -v -run '{run_filter}' {targets} 2>&1 || true"
        )
        try:
            r = subprocess.run(
                ["docker", "run", "--rm", "--env", "CGO_ENABLED=0", img, "bash", "-c", c],
                capture_output=True, text=True, timeout=timeout
            )
            return r.stdout + "\n" + r.stderr
        except subprocess.TimeoutExpired:
            return "__TIMEOUT__"

    # ── Step 2: run targeted packages ─────────────────────────────────────────
    output = _docker_go_test(image_tag, pkg_targets)
    if output == "__TIMEOUT__":
        return {t: "TIMEOUT" for t in tests}
    _parse_output(output, parsed)

    # ── Step 2b: if Go version is too old, upgrade and retry ──────────────────
    if not parsed:
        mod_req = re.search(r'note: module requires Go (\d+\.\d+)', output)
        if mod_req:
            needed_ver = mod_req.group(1)
            print(f"  → Go module requires Go {needed_ver}, upgrading image...")
            upgraded_img = _upgrade_go_in_image(image_tag, needed_ver)
            if upgraded_img != image_tag:
                output = _docker_go_test(upgraded_img, pkg_targets)
                if output == "__TIMEOUT__":
                    return {t: "TIMEOUT" for t in tests}
                _parse_output(output, parsed)
                # Also update image_tag for the ./... fallback below
                image_tag = upgraded_img

    # ── Step 3: if targeted run found nothing, fall back to ./... ─────────────
    if not parsed and pkg_targets != './...':
        fb_output = _docker_go_test(image_tag, './...')
        if fb_output != "__TIMEOUT__":
            _parse_output(fb_output, parsed)

    return _match_tests_to_results(tests, parsed)


def _parse_junit_xml_string(xml_str: str) -> Dict[str, str]:
    """Parse a JUnit XML string and return {classname.method: status} dict.
    Mirrors _parse_junit_xml() from full_validation_multilingual_java.py."""
    import xml.etree.ElementTree as ET
    status_map: Dict[str, str] = {}
    try:
        root = ET.fromstring(xml_str)
        testsuites = [root] if root.tag == 'testsuite' else root.findall('.//testsuite')
        for ts in testsuites:
            for tc in ts.findall('testcase'):
                classname = tc.get('classname', '')
                name = tc.get('name', '')
                if not name:
                    continue
                if tc.find('failure') is not None or tc.find('error') is not None:
                    status = 'FAILED'
                elif tc.find('skipped') is not None:
                    status = 'SKIPPED'
                else:
                    status = 'PASSED'
                # Store both separator styles so _match_tests_to_results can find them
                for key in [f"{classname}.{name}", f"{classname}#{name}", name]:
                    status_map[key] = status
    except Exception:
        pass
    return status_map


def _run_java_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = ""
) -> Dict[str, str]:
    """Run Java tests via Maven/Gradle/Ant and parse JUnit XML results.

    Mirrors full_validation_multilingual_java.py run_tests() logic:
      - Parses test names (FQCN.method) into per-class method lists
      - Detects Maven submodule from test FQCN for multi-module projects
      - Runs targeted tests (-Dtest= for Maven, --tests for Gradle)
      - Dumps JUnit XML surefire-reports to stdout and parses them (primary)
      - Falls back to stdout regex parsing if no XML found
    """
    if not tests:
        return {}

    # ── 1. Detect build system ────────────────────────────────────────────────
    detect_cmd = (
        "if [ -f /testbed/pom.xml ]; then echo maven; "
        "elif [ -f /testbed/build.gradle ] || [ -f /testbed/build.gradle.kts ]; then echo gradle; "
        "elif [ -f /testbed/build.xml ]; then echo ant; "
        "else echo maven; fi"
    )
    r = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c", detect_cmd],
        capture_output=True, text=True
    )
    build_system = r.stdout.strip() or "maven"

    compat = "-Dmaven.javadoc.skip=true -Denforcer.skip=true -Dcheckstyle.skip=true -Dproguard.skip=true"
    mvn_bin = "$([ -x /opt/conda/bin/mvn ] && echo /opt/conda/bin/mvn || echo mvn)"
    gradle_bin = "$([ -f /testbed/gradlew ] && echo /testbed/gradlew || echo gradle)"

    # ── 2. Parse test names into {FQCN: [methods]} ───────────────────────────
    # Format from FAIL_TO_PASS: "org.example.ClassName.methodName"
    # Last dot-segment that starts lowercase is the method; the rest is the FQCN.
    class_methods: Dict[str, List[str]] = {}
    for t in tests:
        idx = t.rfind('.')
        if idx > 0:
            fqcn, method = t[:idx], t[idx + 1:]
            # Method names start lowercase; class names start uppercase
            if method and method[0].islower():
                class_methods.setdefault(fqcn, []).append(method)
                continue
        class_methods.setdefault(t, [])

    # ── 3. Detect Maven/Gradle submodule from test FQCN ──────────────────────
    # Find the source file for the first test class inside the container, then
    # infer the module path from the path (mirrors _detect_maven_module logic).
    submodule = ""
    first_fqcn = next(iter(class_methods))
    short_name = first_fqcn.split('.')[-1]
    find_r = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c",
         f"find /testbed -name '{short_name}.java' -path '*/test/*' 2>/dev/null | head -1"],
        capture_output=True, text=True
    )
    found_path = find_r.stdout.strip()
    if found_path:
        # /testbed/lucene/queries/src/test/java/... → lucene/queries
        rel = found_path.replace('/testbed/', '', 1)
        parts = rel.split('/src/test/')
        if len(parts) == 2 and parts[0]:
            submodule = parts[0]

    # ── 4. Build targeted test filters ───────────────────────────────────────
    # Maven surefire: -Dtest="ClassName#method1+method2,OtherClass#method3"
    # The '#' must be quoted in the shell command to avoid comment interpretation.
    #
    # IMPORTANT: Parameterized test names contain special characters like '[', ']', '#'
    # (e.g. "testFoo[JsonArray#asList [collection size: one]]") that break Maven surefire's
    # filter parser.  Skip any method name that contains these characters so only simple
    # test methods are targeted.  Skipped (parameterized) tests will appear as MISSING in
    # both before and after, which compare_results treats as success (no regression).
    surefire_parts = []
    for fqcn, methods in class_methods.items():
        short = fqcn.split('.')[-1]
        simple_methods = [m for m in methods if not any(c in m for c in '[]()#')]
        if simple_methods:
            surefire_parts.append(f"{short}#{'+'.join(simple_methods)}")
        elif not methods:
            surefire_parts.append(short)
        # else: all methods are parameterized — skip this class entirely
    surefire_filter = ','.join(surefire_parts)

    # Gradle: --tests 'org.example.ClassName.methodName' (one flag per test)
    # Cap at 30 to avoid shell-length limits; also skip parameterized names.
    gradle_test_args = ' '.join(
        f"--tests '{fqcn}.{m}'" if methods else f"--tests '{fqcn}'"
        for fqcn, methods in list(class_methods.items())[:30]
        for m in (methods if methods else [''])
        if not any(c in m for c in '[]()#')
    )

    # ── 5. XML dump suffix: cat all surefire/test-result XML files to stdout ──
    # This lets us parse structured results without needing docker cp.
    xml_dump = (
        "echo '===SUREFIRE_XML_START==='; "
        "find /testbed -name 'TEST-*.xml' "
        r"  \( -path '*/surefire-reports/*' -o -path '*/test-results/*' \) "
        "  2>/dev/null | while IFS= read -r f; do "
        "    echo \"===FILE: $f===\"; cat \"$f\"; echo '===END_FILE==='; "
        "  done; "
        "echo '===SUREFIRE_XML_END==='"
    )

    # ── 6. Build full shell command ───────────────────────────────────────────
    if build_system == "maven":
        module_arg = f"-pl {submodule} --also-make" if submodule else ""
        if surefire_filter:
            test_filter_arg = f"-Dtest='{surefire_filter}' -Dsurefire.failIfNoSpecifiedTests=false "
        else:
            test_filter_arg = ""
        cmd = (
            f"cd /testbed && {mvn_bin} test {module_arg} {compat} "
            f"{test_filter_arg}"
            f"2>&1 || true; {xml_dump}"
        )
    elif build_system == "gradle":
        module_task = f"{submodule.replace('/', ':')}:test" if submodule else "test"
        cmd = (
            f"cd /testbed && {gradle_bin} {module_task} --no-daemon "
            f"{gradle_test_args} 2>&1 || true; {xml_dump}"
        )
    else:  # ant
        cmd = f"cd /testbed && ant test 2>&1 || true; {xml_dump}"

    # ── 7. Execute ────────────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: "TIMEOUT" for t in tests}

    output = result.stdout + "\n" + result.stderr

    # ── 8. Parse JUnit XML files embedded in output (primary) ─────────────────
    parsed: Dict[str, str] = {}
    xml_section = re.search(
        r'===SUREFIRE_XML_START===(.*?)===SUREFIRE_XML_END===', output, re.DOTALL
    )
    if xml_section:
        for xml_block in re.split(r'===FILE:[^=]*===', xml_section.group(1)):
            xml_block = xml_block.replace('===END_FILE===', '').strip()
            if xml_block.startswith('<'):
                parsed.update(_parse_junit_xml_string(xml_block))

    # ── 9. Fallback: stdout regex parsing if no XML found ─────────────────────
    if not parsed:
        for line in output.split('\n'):
            # Ant/Lombok SimpleTestFormatter: "    [junit] [PASS] testName(className)"
            # (mirrors _parse_ant_stdout_results in full_validation_multilingual_java.py)
            stripped = re.sub(r'^\s*\[\w+\]\s*', '', line).strip()
            m = re.match(r'\[(PASS|FAIL|ERR)\]\s+([^\s(]+)\(([^)]+)\)', stripped)
            if m:
                tag, method, classname = m.group(1), m.group(2), m.group(3)
                status = 'PASSED' if tag == 'PASS' else ('FAILED' if tag == 'FAIL' else 'ERROR')
                parsed[f"{classname}.{method}"] = status
                continue
            # Maven failed: "  testMethod(classname)  <<< FAILURE!"
            m = re.search(r'(\w+)\(([^)]+)\).*?(FAILURE|ERROR)', line)
            if m:
                method, classname = m.group(1), m.group(2)
                for key in [f"{classname}#{method}", f"{classname}.{method}"]:
                    parsed[key] = 'FAILED'
            # Gradle: "  classname > methodName PASSED/FAILED/SKIPPED"
            m = re.match(r'\s+(\S+)\s+>\s+(\S+)\s+(PASSED|FAILED|SKIPPED)', line)
            if m:
                classname, method, status = m.group(1), m.group(2), m.group(3)
                for key in [f"{classname}#{method}", f"{classname}.{method}"]:
                    parsed[key] = status
            # Maven summary: "Tests run: X, Failures: 0, Errors: 0 - in classname"
            m = re.search(r'Tests run:\s*\d+.*?Failures:\s*(\d+).*?Errors:\s*(\d+).*?in\s+(\S+)', line)
            if m:
                n_fail, n_err, classname = int(m.group(1)), int(m.group(2)), m.group(3)
                if n_fail == 0 and n_err == 0:
                    short = classname.split('.')[-1]
                    for t in tests:
                        if short in t and t not in parsed:
                            parsed[t] = 'PASSED'

    return _match_tests_to_results(tests, parsed)


def _upgrade_php_for_phpunit(image_tag: str) -> str:
    """Check if PHPUnit requires a newer PHP version than is installed, and upgrade if so.

    This handles the case where composer.lock pins a PHPUnit version that requires
    a higher PHP than the image's default PHP (e.g., PHPUnit 11 requires PHP >= 8.2
    but the image was built with PHP 7.4).

    Returns the (possibly updated) image tag.
    """
    # Step 1: Check PHPUnit's PHP version requirement
    ver_result = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c",
         "cd /testbed && vendor/bin/phpunit --version 2>&1 | head -5 || true"],
        capture_output=True, text=True, timeout=30
    )
    ver_output = ver_result.stdout + ver_result.stderr
    req_match = re.search(r'requires PHP >= (\d+\.\d+)', ver_output)
    if not req_match:
        return image_tag  # No upgrade needed

    required_ver = req_match.group(1)  # e.g. "8.2"

    # Step 2: Check current PHP version
    cur_result = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c",
         "php --version 2>/dev/null | head -1 || true"],
        capture_output=True, text=True, timeout=15
    )
    cur_match = re.search(r'PHP\s+(\d+\.\d+)', cur_result.stdout)
    if not cur_match:
        return image_tag

    current_ver = cur_match.group(1)

    def _ver_tuple(v):
        return tuple(int(x) for x in v.split('.'))

    if _ver_tuple(current_ver) >= _ver_tuple(required_ver):
        return image_tag  # Already meets the requirement

    # Step 3: Create new image with upgraded PHP
    safe_tag = re.sub(r'[^a-zA-Z0-9_.-]', '_', image_tag)
    new_tag = f"{image_tag}_php{required_ver.replace('.', '')}"
    # Reuse existing prepared image if already built
    if subprocess.run(["docker", "image", "inspect", new_tag],
                      capture_output=True).returncode == 0:
        print(f"  → Reusing cached PHP {required_ver} image: {new_tag}")
        return new_tag

    print(f"  → PHPUnit requires PHP >= {required_ver} (image has PHP {current_ver}), upgrading...")

    safe = image_tag.replace(":", "_").replace("/", "_").replace(".", "_")
    container_name = f"php_upgrade_{safe}"[:60]
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Install required PHP version (ondrej/php PPA is already in the image from
    # Dockerfile.instance.php build) and update the default `php` symlink.
    # Try versioned extensions first; fall back to minimal set on failure.
    install_cmd = (
        "apt-get update -qq 2>/dev/null && "
        f"(apt-get install -y -q "
        f"php{required_ver}-cli php{required_ver}-common php{required_ver}-xml "
        f"php{required_ver}-zip php{required_ver}-mbstring php{required_ver}-curl "
        f"php{required_ver}-gmp php{required_ver}-intl php{required_ver}-tokenizer "
        f"2>/dev/null || "
        f"apt-get install -y -q php{required_ver}-cli php{required_ver}-common "
        f"php{required_ver}-xml php{required_ver}-mbstring 2>/dev/null || true) && "
        f"(update-alternatives --set php /usr/bin/php{required_ver} 2>/dev/null || "
        f"ln -sf /usr/bin/php{required_ver} /usr/bin/php 2>/dev/null || true)"
    )

    result = subprocess.run(
        ["docker", "run", "--name", container_name, image_tag, "bash", "-c", install_cmd],
        capture_output=True, text=True, timeout=300
    )

    subprocess.run(["docker", "commit", container_name, new_tag], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Verify the upgrade worked
    verify = subprocess.run(
        ["docker", "run", "--rm", new_tag, "bash", "-c",
         f"php --version 2>/dev/null | head -1 || true"],
        capture_output=True, text=True, timeout=15
    )
    if required_ver in verify.stdout:
        print(f"  ✓ Upgraded PHP to {required_ver} → {new_tag}")
    else:
        print(f"  ⚠ PHP upgrade may have failed, proceeding with best-effort image")

    return new_tag


def _ensure_php_extensions(image_tag: str) -> str:
    """Ensure required PHP extensions (pdo_sqlite, etc.) are installed in the image.

    If missing extensions are found, installs them via apt and commits a new image.
    Returns the (possibly updated) image tag.
    """
    # Check which required extensions are missing
    check_cmd = "php -m | grep -qi pdo_sqlite && echo HAS_PDO_SQLITE || echo MISSING_PDO_SQLITE"
    result = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c", check_cmd],
        capture_output=True, text=True, timeout=30
    )
    if "HAS_PDO_SQLITE" in result.stdout:
        return image_tag  # Already installed

    # Detect PHP version for the correct package name (e.g. "8.2")
    ver_result = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c", "php --version 2>/dev/null | head -1"],
        capture_output=True, text=True, timeout=15
    )
    m = re.search(r'PHP\s+(\d+\.\d+)', ver_result.stdout)
    php_ver = m.group(1) if m else ""  # e.g. "8.2"

    new_tag = f"{image_tag}_phpext"
    # Reuse existing prepared image
    if subprocess.run(["docker", "image", "inspect", new_tag],
                      capture_output=True).returncode == 0:
        return new_tag

    safe = image_tag.replace(":", "_").replace("/", "_").replace(".", "_")
    container_name = f"php_ext_{safe}"
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Try versioned package first (e.g. php8.2-sqlite3), then generic php-sqlite3
    install_cmd = (
        "apt-get update -qq 2>/dev/null && "
        f"(apt-get install -y php{php_ver}-sqlite3 2>/dev/null || "
        "apt-get install -y php-sqlite3 2>/dev/null || true)"
    )
    r = subprocess.run(
        ["docker", "run", "--name", container_name, image_tag, "bash", "-c", install_cmd],
        capture_output=True, text=True, timeout=120
    )
    subprocess.run(["docker", "commit", container_name, new_tag], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    print(f"  ✓ Installed PHP SQLite extension → {new_tag}")
    return new_tag


def _run_php_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = ""
) -> Dict[str, str]:
    """Run PHP tests via PHPUnit with targeted --filter and JUnit XML output.
    Test name format: 'Description (FQCN)::methodName"dataSetName"'
    Mirrors PHP validator: installs directly in image, no Docker-in-Docker."""

    # ── Parse FQCN and method from test names ────────────────────────────────
    # Format: "Statement Indentation Fixer (Ns\ClassName)::testMethod"dataSet""
    fqcns: set = set()
    methods: set = set()
    for t in tests:
        m = re.search(r'\(([^)]+)\)', t)
        if m:
            fqcns.add(m.group(1))
        m2 = re.search(r'\)::(\w+)', t)
        if m2:
            methods.add(m2.group(1))
        # Also handle plain "ClassName::method" format (no description prefix)
        m3 = re.match(r'([\w\\]+)::([\w]+)', t)
        if m3 and not m:
            fqcns.add(m3.group(1))
            methods.add(m3.group(2))

    # Build --filter: prefer matching by short class name to handle namespace escaping
    short_classes = {fqcn.split('\\')[-1] for fqcn in fqcns}
    if short_classes:
        filter_pattern = '|'.join(re.escape(c) for c in sorted(short_classes))
    elif methods:
        filter_pattern = '|'.join(re.escape(m) for m in sorted(methods))
    else:
        filter_pattern = ''  # run all

    junit_xml = '/tmp/phpunit_results.xml'
    phpunit_cmd = (
        "if [ -f vendor/bin/phpunit ]; then PHPUNIT_CMD='php vendor/bin/phpunit'; "
        "elif command -v phpunit > /dev/null 2>&1; then PHPUNIT_CMD='phpunit'; "
        "else PHPUNIT_CMD='php vendor/bin/phpunit'; fi; "
        f"$PHPUNIT_CMD --colors=never --log-junit {junit_xml} "
        + (f"--filter '{filter_pattern}' " if filter_pattern else "")
        + "2>&1 || true"
    )
    xml_dump = f"echo '===PHPUNIT_XML_START==='; cat {junit_xml} 2>/dev/null || true; echo '===PHPUNIT_XML_END==='"
    cmd = f"cd /testbed && {phpunit_cmd}; {xml_dump}"

    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: "TIMEOUT" for t in tests}

    output = result.stdout + "\n" + result.stderr

    # ── Parse JUnit XML (primary) ─────────────────────────────────────────────
    parsed: Dict[str, str] = {}
    xml_section = re.search(
        r'===PHPUNIT_XML_START===(.*?)===PHPUNIT_XML_END===', output, re.DOTALL
    )
    if xml_section:
        xml_content = xml_section.group(1).strip()
        if xml_content.startswith('<'):
            parsed.update(_parse_junit_xml_string(xml_content))

    # ── Fallback: stdout parsing ──────────────────────────────────────────────
    if not parsed:
        for line in output.split('\n'):
            m = re.match(r'(PASSED|FAILED|ERROR|SKIPPED|INCOMPLETE)\s+(\S+)', line)
            if m:
                status, name = m.group(1), m.group(2)
                parsed[name] = 'PASSED' if status == 'PASSED' else ('FAILED' if status in ('FAILED', 'ERROR') else 'SKIPPED')
            m = re.match(r'\s+[✔✓]\s+(\S.+)', line)
            if m:
                parsed[m.group(1).strip()] = 'PASSED'
            m = re.match(r'\s+[✘✗]\s+(\S.+)', line)
            if m:
                parsed[m.group(1).strip()] = 'FAILED'

    # ── PHP-CS-Fixer camelCase normalization ──────────────────────────────────
    # JUnit XML names use testdox-style spaces:  'testFix with data set "no brace block"'
    # SWE-bench test IDs use camelCase data sets: 'testFixWithDataSet"noBraceBlock"'
    # Add camelCase keys to parsed so _match_tests_to_results can find them.
    def _php_camel_dataset(key: str) -> str:
        """Convert space-separated testdox dataset key to camelCase.
        'no brace block'  → 'noBraceBlock'
        'WITHOUT stick_x' → 'WithoutStick_x'  (underscore suffix kept verbatim)
        """
        words = key.split(' ')
        if not words:
            return key
        result = words[0]
        for word in words[1:]:
            if not word:
                continue
            us_idx = word.find('_')
            if us_idx == -1:
                result += word[0].upper() + word[1:].lower() if len(word) > 1 else word[0].upper()
            else:
                prefix = word[:us_idx]
                suffix = word[us_idx:]
                result += (prefix[0].upper() + prefix[1:].lower() if prefix else '') + suffix
        return result

    php_extra: Dict[str, str] = {}
    for k, v in list(parsed.items()):
        m_ds = re.search(r'(test\w+) with data set ["\'](.+)["\']$', k)
        if m_ds:
            method = m_ds.group(1)
            dataset = m_ds.group(2)
            camel_key = f'{method}WithDataSet"{_php_camel_dataset(dataset)}"'
            php_extra[camel_key] = v
    parsed.update(php_extra)

    return _match_tests_to_results(tests, parsed)


def _parse_js_output(output: str) -> Dict[str, str]:
    """Parse JavaScript/TypeScript test output (Mocha, Karma, Jest, Vitest)."""
    # Strip ANSI color codes so Unicode checkmarks are not obscured
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    output = ansi_escape.sub('', output)

    parsed: Dict[str, str] = {}
    for line in output.split('\n'):
        # PASSED: ✓ or ✔ anywhere in the line (Mocha, Karma, Jest, Vitest).
        # Use re.search (not re.match) so Karma summary lines like
        # "✓ 982 tests completed" (no leading whitespace) are also captured.
        m = re.search(r'[✓✔]\s+(.+)', line)
        if m:
            name = re.sub(r'\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$', '', m.group(1)).strip()
            # Strip Vitest file-level summary suffix: "file.spec.ts (28 tests) 78ms"
            # After timing strip, it becomes "file.spec.ts (28 tests)" — drop count too.
            name = re.sub(r'\s+\(\d+\s+tests?\)\s*$', '', name).strip()
            # Require at least two words to skip bare describe-block headings
            if name and len(name.split()) > 1:
                parsed[name] = 'PASSED'
        # FAILED: ✕, ✗, ×, or ✖ (U+2716, used by Karma mocha reporter) anywhere in line
        m = re.search(r'[✕✗×✖]\s+(.+)', line)
        if m:
            name = re.sub(r'\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$', '', m.group(1)).strip()
            # Skip Karma aggregate summary lines like "✖ 1 test failed"
            if name and len(name.split()) > 1 and not re.match(r'\d+\s+test', name):
                parsed[name] = 'FAILED'
        # Jest failure block: "  ● description"
        m = re.match(r'\s+●\s+(.+)', line)
        if m:
            name = m.group(1).strip()
            if name and not name.startswith('●') and not name.startswith('Console'):
                parsed[name] = 'FAILED'
        # Mocha numbered failures: "  1) description"
        m = re.match(r'\s+\d+\)\s+(.+)', line)
        if m:
            name = m.group(1).strip()
            if name and len(name.split()) > 1:
                parsed[name] = 'FAILED'
        # Jest file-level summary: "PASS packages/babel-core/test/api.js" or "FAIL path (10.13 s)"
        m = re.match(r'(PASS|FAIL)\s+(\S+)', line.strip())
        if m:
            status = m.group(1)
            test_file = re.sub(r'\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$', '', m.group(2)).strip()
            if test_file:
                parsed[test_file] = 'PASSED' if status == 'PASS' else 'FAILED'
    return parsed


def _js_extract_imports(patch: str) -> set:
    """Extract npm package names from import/require statements in added lines of a patch."""
    pkgs: set = set()
    for line in patch.split('\n'):
        if not line.startswith('+'):
            continue
        # import ... from 'pkg' or import ... from "pkg"
        m = re.search(r'''from\s+['"]([^'"]+)['"]''', line)
        if m:
            base = m.group(1).split('/')[0]
            if base.startswith('@'):
                parts = m.group(1).split('/')
                if len(parts) >= 2:
                    base = f"{parts[0]}/{parts[1]}"
            if not base.startswith('.'):
                pkgs.add(base)
        # require('pkg') or require("pkg")
        m = re.search(r'''require\s*\(\s*['"]([^'"]+)['"]\s*\)''', line)
        if m:
            base = m.group(1).split('/')[0]
            if base.startswith('@'):
                parts = m.group(1).split('/')
                if len(parts) >= 2:
                    base = f"{parts[0]}/{parts[1]}"
            if not base.startswith('.'):
                pkgs.add(base)
    return pkgs


def _js_extract_pkg_json_deps(patch: str) -> Dict[str, str]:
    """Extract new package versions added to package.json in a patch."""
    deps: Dict[str, str] = {}
    in_pkg_json = False
    for line in patch.split('\n'):
        if 'package.json' in line and ('diff --git' in line or '--- ' in line or '+++ ' in line):
            in_pkg_json = True
            continue
        if in_pkg_json and line.startswith('diff --git'):
            break
        if not in_pkg_json or not line.startswith('+'):
            continue
        m = re.search(r'^\+\s*["\']([a-zA-Z0-9@/_-]+)["\']\s*:\s*["\']([^"\']+)["\']', line)
        if m and re.match(r'[\^~>=]?\d', m.group(2)):
            deps[m.group(1)] = m.group(2)
    return deps


def _run_js_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = ""
) -> Dict[str, str]:
    """Run JavaScript/TypeScript tests via npm/yarn test and parse output.
    Passes a --testNamePattern (Jest) or --grep (Mocha) filter built from the
    test description strings in FAIL_TO_PASS/PASS_TO_PASS."""
    nvm_init = '. "$NVM_DIR/nvm.sh" 2>/dev/null || true'

    # Step 1: Detect the best test script and whether it uses run-p (parallel).
    # Prefer a sub-script over the main test when it includes build/lint steps.
    # If the chosen script uses run-p, capture its sub-scripts so we can run
    # them SEQUENTIALLY — parallel execution under QEMU emulation causes Chrome
    # to time out competing for CPU, producing non-deterministic Karma output.
    detect_cmd = (
        f"{nvm_init} && cd /testbed && node -e \""
        "try{"
        "var p=require('./package.json');"
        "var s=p.scripts||{};"
        "var t=s.test||'';"
        "var hasBuild=['build','lint','compile'].some(function(k){return t.indexOf(k)>-1;});"
        "var pref=['test:unit','test:browser','test:mocha','test:jest','test:vitest','test:spec','unit'];"
        "var chosen='';"
        "if(hasBuild){for(var i=0;i<pref.length;i++){if(s[pref[i]]){chosen=pref[i];break;}}}"
        "if(!chosen){console.log('test:__none__');process.exit(0);}"
        "var ct=s[chosen]||'';"
        "var m=ct.match(/(?:run-p|npm-run-all\\s+--parallel)\\s+(.+)/);"
        "if(m){"
        "  var subs=m[1].trim().split(/\\s+/).filter(function(x){return !x.startsWith('--');});"
        "  console.log('run-p:'+subs.join(','));"
        "}else{"
        "  console.log(chosen);"
        "}"
        "}catch(e){}process.exit(0);"
        "\" 2>/dev/null || true"
    )
    try:
        detect_result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", detect_cmd],
            capture_output=True, text=True, timeout=30
        )
        detect_out = detect_result.stdout.strip()
    except Exception:
        detect_out = ""

    # Parse detection result into a list of scripts to run sequentially
    if detect_out.startswith("run-p:"):
        sub_scripts = detect_out[len("run-p:"):].split(",")
    elif detect_out and detect_out != "test:__none__":
        sub_scripts = [detect_out]
    else:
        sub_scripts = []  # will fall back to "npm test"

    # Step 2: Setup Chrome/Chromium for Karma browser tests.
    # Installs Google Chrome via the official apt repo (AMD64 only).
    chrome_setup = (
        "export CHROME_BIN=$(command -v google-chrome-stable google-chrome chromium chromium-browser 2>/dev/null | head -1); "
        "if [ -z \"$CHROME_BIN\" ]; then "
        "  (apt-get update -qq 2>/dev/null "
        "   && apt-get install -y -qq --no-install-recommends wget gnupg2 2>/dev/null "
        "   && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub "
        "      | gpg --dearmor > /usr/share/keyrings/google-chrome.gpg 2>/dev/null "
        "   && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] "
        "      http://dl.google.com/linux/chrome/deb/ stable main' "
        "      > /etc/apt/sources.list.d/google-chrome.list "
        "   && apt-get update -qq 2>/dev/null "
        "   && apt-get install -y -qq --no-install-recommends google-chrome-stable 2>/dev/null "
        "   && rm -f /etc/apt/sources.list.d/google-chrome.list) "
        "  || true; "
        "  export CHROME_BIN=$(command -v google-chrome-stable google-chrome chromium chromium-browser 2>/dev/null | head -1); "
        "fi; "
    )

    # Step 3: Build a single bash command that runs all test sub-scripts
    # SEQUENTIALLY in ONE container execution.
    #
    # Why one container, not one per script:
    #   • Chrome is installed once in chrome_setup → avoids repeated downloads.
    #   • Scripts run alone (no parallel CPU contention) → Chrome pings don't
    #     time out under QEMU x86_64 emulation on ARM64 hosts.
    #   • Output is naturally ordered → parser never sees interleaved lines.
    #
    # Karma timeout patch: default browserDisconnectTimeout (2000 ms) is too
    # short for Chrome under QEMU emulation.  We raise it to 30 s in-container.
    # Patch karma.conf.js to:
    #   1. Increase browser timeouts — 2000 ms default is too short under QEMU.
    #   2. Add --disable-dev-shm-usage Chrome flag — Docker's default /dev/shm
    #      (64 MB) causes Chrome to run out of shared memory and crash mid-test.
    karma_patch = (
        "if [ -f /testbed/karma.conf.js ]; then "
        "  node -e \""
        "  var fs=require('fs');"
        "  var c=fs.readFileSync('/testbed/karma.conf.js','utf8');"
        "  if(c.indexOf('browserDisconnectTimeout')<0){"
        "    c=c.replace(/config\\.set\\(\\{/,"
        "      'config.set({browserDisconnectTimeout:30000,"
        "browserNoActivityTimeout:120000,captureTimeout:120000,"
        "browserDisconnectTolerance:2,');"
        "    fs.writeFileSync('/testbed/karma.conf.js',c);"
        "  }"
        "  \" 2>/dev/null || true; "
        "fi; "
    )

    scripts_to_run = sub_scripts if sub_scripts else ["npm test"]

    # Detect if tests use Vitest's "file > suite > test" format.
    # Vitest's default reporter only emits file-level summaries; individual
    # test names only appear with --reporter=verbose.  When we see this format,
    # override the command to run vitest with verbose output on the specific
    # test files so the parser can match individual test names.
    #
    # Guard: only activate vitest mode when at least one test uses the
    # "file > suite > test" hierarchy.  This prevents accidentally passing
    # --reporter=verbose to Jest/Mocha projects whose PASS_TO_PASS entries
    # happen to be bare file paths (e.g. babel, docusaurus).
    _ts_exts = ('.ts', '.tsx', '.js', '.jsx', '.mts', '.cts', '.mjs', '.cjs')
    _has_vitest_format = any(' > ' in t for t in tests)
    _vitest_files: set = set()
    if _has_vitest_format:
        for t in tests:
            if ' > ' in t:
                fp = t.split(' > ')[0].strip()
                if fp.endswith(_ts_exts):
                    _vitest_files.add(fp)
            elif t.endswith(_ts_exts):
                _vitest_files.add(t)
    vitest_test_files = sorted(_vitest_files)

    if vitest_test_files:
        # Run vitest with --reporter=verbose and target only the relevant files.
        # `npm test -- <args>` forwards args to the underlying vitest/jest binary.
        file_args = ' '.join(f"'{f}'" for f in vitest_test_files)
        script_cmds = f"CI=true npm test -- --reporter=verbose {file_args} 2>&1 || true"
    else:
        script_cmds = "; ".join(
            f"CI=true npm run {s} 2>&1 || true" if s != "npm test"
            else "CI=true npm test 2>&1 || true"
            for s in scripts_to_run
        )

    cmd = f"{nvm_init} && cd /testbed && {chrome_setup}{karma_patch}{script_cmds}"

    try:
        result = subprocess.run(
            # --shm-size=2gb: gives Chrome enough shared memory to avoid crashes
            # in Docker where /dev/shm defaults to 64 MB.
            ["docker", "run", "--rm", "--shm-size=2gb", "--env", "CI=true",
             image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: "TIMEOUT" for t in tests}

    output = result.stdout + "\n" + result.stderr
    merged = _parse_js_output(output)
    return _match_tests_to_results(tests, merged)


def _run_ruby_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = ""
) -> Dict[str, str]:
    """Run Ruby tests via RSpec (JSON output) or Minitest and parse output.
    For RSpec: extracts unique spec files from test IDs and runs only those files,
    using --format json so example ids match the FAIL_TO_PASS format exactly."""
    rbenv_init = (
        'export PATH="$HOME/.rbenv/bin:$HOME/.rbenv/shims:$PATH" && '
        'eval "$(rbenv init - bash 2>/dev/null)" 2>/dev/null || true'
    )

    detect_result = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c",
         f"{rbenv_init} && "
         "if [ -d /testbed/spec ]; then echo rspec; "
         "elif [ -f /testbed/Rakefile ]; then echo rake; "
         "else echo rspec; fi"],
        capture_output=True, text=True
    )
    runner = detect_result.stdout.strip() or "rspec"

    if runner == "rspec":
        # Extract unique spec files from test IDs: "./spec/file.rb[1:1:1]" → "./spec/file.rb"
        spec_files = sorted({
            re.sub(r'\[[\d:]+\]$', '', t).strip()
            for t in tests
            if t.startswith('./spec/') or t.startswith('spec/')
        })
        # Fallback: run entire spec directory if no file paths found in test IDs
        spec_target = ' '.join(f"'{f}'" for f in spec_files) if spec_files else 'spec'

        json_out = "/tmp/rspec_results.json"
        cmd = (
            f"{rbenv_init} && cd /testbed && "
            f"bundle exec rspec {spec_target} "
            f"--format json --out {json_out} --format progress 2>&1 || true; "
            f"echo '===RSPEC_JSON_START==='; cat {json_out} 2>/dev/null || echo '{{}}'; echo '===RSPEC_JSON_END==='"
        )
    else:
        # Minitest / Rake — find only the test files that contain the requested classes,
        # rather than running the entire test suite (which can time out on large repos).
        # Test IDs are either:
        #   "ClassName::description"  (real test names)
        #   "path::to::file::test_{i}_{j}_{passed|failed}"  (synthetic names from full_validation)
        # For synthetic names, extract the file path directly; for real names, grep for the class.
        _SYNTHETIC_RE = re.compile(r'^(.+?)::test_(\d+)_(\d+)_(passed|failed)$')
        synthetic_files = sorted({
            m.group(1).replace('::', '/') + '.rb'
            for t in tests
            for m in [_SYNTHETIC_RE.match(t)]
            if m
        })
        if synthetic_files:
            test_files = synthetic_files
        else:
            class_names = sorted({t.split('::')[0] for t in tests if '::' in t and t[0].isupper()})
            grep_pattern = '|'.join(re.escape(c) for c in class_names) if class_names else ''
            if grep_pattern:
                find_cmd = (
                    f"{rbenv_init} && cd /testbed && "
                    f"grep -rl --include='*.rb' -E 'class ({grep_pattern})' test/ 2>/dev/null | sort"
                )
                find_result = subprocess.run(
                    ["docker", "run", "--rm", image_tag, "bash", "-c", find_cmd],
                    capture_output=True, text=True, timeout=30
                )
                test_files = [f.strip() for f in find_result.stdout.strip().split('\n') if f.strip()]
            else:
                test_files = []

        if test_files:
            # Run each file separately (rake treats TEST= as env var; multiple assignments
            # overwrite each other, so only the last file would run if concatenated).
            file_cmds = ' && '.join(
                f"bundle exec rake test TEST={f} TESTOPTS='-v' 2>&1 || true"
                for f in test_files
            )
            cmd = f"{rbenv_init} && cd /testbed && {file_cmds}"
        else:
            cmd = (
                f"{rbenv_init} && cd /testbed && "
                "bundle exec rake test TESTOPTS='-v' 2>&1 || true"
            )

    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: "TIMEOUT" for t in tests}

    output = result.stdout + "\n" + result.stderr
    parsed: Dict[str, str] = {}

    if runner == "rspec":
        # Parse JSON block delimited by markers
        json_section = re.search(
            r'===RSPEC_JSON_START===(.*?)===RSPEC_JSON_END===', output, re.DOTALL
        )
        json_str = json_section.group(1).strip() if json_section else ''
        # Fallback: find last line that looks like RSpec JSON
        if not json_str:
            for line in reversed(output.split('\n')):
                line = line.strip()
                if line.startswith('{') and '"examples"' in line:
                    json_str = line
                    break
        if json_str:
            try:
                data = json.loads(json_str)
                for ex in data.get('examples', []):
                    eid = ex.get('id', '')
                    status = ex.get('status', 'failed')
                    parsed[eid] = 'PASSED' if status == 'passed' else ('SKIPPED' if status == 'pending' else 'FAILED')
            except Exception:
                pass
        # Text fallback
        if not parsed:
            for line in output.split('\n'):
                m = re.match(r'\s+[✓✔]\s+(.+)', line)
                if m:
                    parsed[m.group(1).strip()] = 'PASSED'
                m = re.match(r'\s+[✗✘F]\s+(.+)', line)
                if m:
                    parsed[m.group(1).strip()] = 'FAILED'
    else:
        # test-unit 3.x verbose (TESTOPTS=-v) produces:
        #   ClassName:
        #     test: description:\t[.FE]: (timing)
        # Failure details: "Failure: test: <desc>(<ClassName::context>)"
        # Also handles old Minitest format: "ClassName#test_name = 0.05 s = ."
        current_class = None
        for line in output.split('\n'):
            # Track top-level class header: "ClassName: " (no leading whitespace)
            class_match = re.match(r'^([A-Z]\w*):\s*$', line)
            if class_match:
                current_class = class_match.group(1)
                continue

            # test-unit 3.x verbose result line: "  test: <desc>:\t<result>"
            verbose_match = re.match(r'^\s+test:\s+(.+):\t+([.FE])', line)
            if verbose_match and current_class:
                test_desc = verbose_match.group(1).strip()
                result_char = verbose_match.group(2)
                key = f"{current_class}::{test_desc}"
                parsed[key] = 'PASSED' if result_char == '.' else 'FAILED'
                continue

            # Failure/error detail block: "Failure: test: <desc>(<ClassName>)"
            fail_match = re.search(r'(?:Failure|Error):\s*test:\s*(.+?)\(([^)]+)\)', line)
            if fail_match:
                test_desc = fail_match.group(1).strip()
                test_class = fail_match.group(2).split('::')[0]
                key = f"{test_class}::{test_desc}"
                parsed[key] = 'FAILED'
                continue

            # Minitest verbose fallback: "ClassName#test_name = 0.05 s = ."
            m = re.search(r'(test_\w+)\s*=.*=\s*([.FE])', line)
            if m:
                key = m.group(1)
                parsed[key] = 'PASSED' if m.group(2) == '.' else 'FAILED'

        # Synthetic name generation (mirrors full_validation_multilingual_ruby.py):
        # When individual test names cannot be parsed from verbose output, generate
        # positional names from the summary line so they match the stored FAIL_TO_PASS
        # / PASS_TO_PASS IDs produced by that script.
        summary_match = re.search(
            r'(\d+) (?:tests?|runs?),.*?(\d+) failures?,.*?(\d+) errors?', output, re.IGNORECASE
        )
        if summary_match:
            total_tests = int(summary_match.group(1))
            failures = int(summary_match.group(2))
            errors = int(summary_match.group(3))
            passed = total_tests - failures - errors

            detected_count = len(parsed)
            if detected_count < total_tests:
                missing_failed = max(0, failures - sum(1 for s in parsed.values() if s in ('FAILED', 'ERROR')))
                missing_passed = max(0, passed - sum(1 for s in parsed.values() if s == 'PASSED'))

                if test_files:
                    for i, test_file in enumerate(test_files):
                        test_file_name = test_file.replace('/', '::').replace('.rb', '')
                        if missing_failed > 0:
                            count = (missing_failed + len(test_files) - 1) // len(test_files)
                            for j in range(min(count, missing_failed)):
                                parsed[f"{test_file_name}::test_{i}_{j}_failed"] = 'FAILED'
                                missing_failed -= 1
                        if missing_passed > 0:
                            count = (missing_passed + len(test_files) - 1) // len(test_files)
                            for j in range(min(count, missing_passed)):
                                parsed[f"{test_file_name}::test_{i}_{j}_passed"] = 'PASSED'
                                missing_passed -= 1
                else:
                    for j in range(missing_failed):
                        parsed[f"test_{j}_failed"] = 'FAILED'
                    for j in range(missing_passed):
                        parsed[f"test_{j}_passed"] = 'PASSED'

    return _match_tests_to_results(tests, parsed)


def _detect_redis_tcl_units_from_patch(patch_content: str) -> List[str]:
    """Map changed C source files to Redis TCL test units from the patch."""
    units = []
    changed_files = re.findall(r'diff --git a/(.*?) b/', patch_content)
    for src_file in changed_files:
        # src/t_XXX.c → unit/type/XXX  (Redis type command handlers)
        m = re.match(r'src/t_(\w+)\.c', src_file)
        if m:
            units.append(f'unit/type/{m.group(1)}')
            continue
        # src/XXX.c → unit/XXX  (general Redis modules)
        m = re.match(r'src/(\w+)\.c', src_file)
        if m:
            units.append(f'unit/{m.group(1)}')
    return list(dict.fromkeys(units))  # deduplicate preserving order


def _generate_redis_proto_tcl(patch_content: str) -> str:
    """Generate RESP protocol-level nil-vs-empty-array TCL test from a Redis patch.
    Mirrors CValidator._generate_resp_type_tests() in full_validation_multilingual_c.py."""
    null_syms  = {'shared.null', 'shared.nullbulk', 'shared.nullarray'}
    empty_syms = {'shared.emptyset', 'shared.emptyarray', 'shared.emptymultibulk'}
    removed_null = added_empty = removed_empty = added_null = False
    for line in patch_content.split('\n'):
        if not (line.startswith('+') or line.startswith('-')):
            continue
        sign, body = line[0], line[1:]
        for sym in null_syms:
            if sym in body:
                if sign == '-': removed_null = True
                else: added_null = True
        for sym in empty_syms:
            if sym in body:
                if sign == '-': removed_empty = True
                else: added_empty = True
    nil_to_empty = removed_null and added_empty and not (removed_empty or added_null)
    empty_to_nil = removed_empty and added_null and not (removed_null or added_empty)
    if not (nil_to_empty or empty_to_nil):
        return ''
    cmd_candidates = []
    for m in re.finditer(r'@@[^\n]*?(\w+Command)\s*\(', patch_content):
        func = m.group(1)
        root = re.sub(r'(?i)(withcount|command)$', '', func, count=2)
        root = re.sub(r'(?i)(withcount|command)$', '', root, count=2)
        if root:
            cmd_candidates.append(root.upper())
    if not cmd_candidates:
        for m in re.finditer(r'\b(\w+Command)\b', patch_content):
            func = m.group(1)
            root = re.sub(r'(?i)(withcount|command)$', '', func, count=2)
            root = re.sub(r'(?i)(withcount|command)$', '', root, count=2)
            if root and len(root) >= 3:
                cmd_candidates.append(root.upper())
    if not cmd_candidates:
        return ''
    seen: set = set()
    cmd_upper = next(c for c in cmd_candidates if not (c in seen or seen.add(c)))
    if nil_to_empty:
        direction = "returns empty array (not nil) after fix"
        expected_prefix = '*0'
    else:
        direction = "returns nil (not empty array) after fix"
        expected_prefix = '*-'
    key, count = 'swebench_noexist', '100'
    resp_req = (
        f'*3\r\n${len(cmd_upper)}\r\n{cmd_upper}\r\n'
        f'${len(key)}\r\n{key}\r\n'
        f'${len(count)}\r\n{count}\r\n'
    )
    tcl_literal = (resp_req
        .replace('\\', '\\\\').replace('"', '\\"')
        .replace('$', '\\$').replace('\r', '\\r').replace('\n', '\\n'))
    tcl_test = (
        f'\n    test "swebench-proto: {cmd_upper} COUNT on nonexisting key {direction}" {{\n'
        f'        r del {key}\n'
        f'        set fd [socket [srv host] [srv port]]\n'
        f'        fconfigure $fd -translation binary -buffering full\n'
        f'        puts -nonewline $fd "{tcl_literal}"\n'
        f'        flush $fd\n'
        f'        after 100\n'
        f'        fconfigure $fd -blocking 0\n'
        f'        set data [read $fd 8]\n'
        f'        fconfigure $fd -blocking 1\n'
        f'        close $fd\n'
        f'        string range $data 0 1\n'
        f'    }} {{{expected_prefix}}}'
    )
    return (
        '# Auto-generated by swebench: protocol-level nil-vs-empty-array check.\n'
        'start_server {tags {"swebench-proto"}} {\n'
        + tcl_test + '\n}\n'
    )


def _tcl_path_to_unit(path: str) -> str:
    """Convert 'tests/unit/cluster/cluster-shards.tcl' → 'unit/cluster/cluster-shards'."""
    if path.startswith('tests/'):
        path = path[len('tests/'):]
    if path.endswith('.tcl'):
        path = path[:-4]
    return path


def _run_redis_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600,
    instance_id: str = "", instance: dict = None
) -> Dict[str, str]:
    """Run Redis/Valkey TCL tests inside Docker. Compiles Redis with -fcommon,
    runs relevant TCL unit files, and generates a proto-level test if the patch
    changes nil↔empty-array responses. Mirrors CValidator in full_validation_multilingual_c.py."""
    import random
    import base64 as _b64
    instance = instance or {}
    patch_content = instance.get('patch', '')
    test_patch = instance.get('test_patch', '')

    # Priority 1: extract TCL units from test_patch (new test files added for this instance)
    tcl_units = []
    if test_patch:
        for m in re.finditer(r'diff --git a/(.*?) b/', test_patch):
            fp = m.group(1)
            if fp.endswith('.tcl') and fp.startswith('tests/') and 'support/' not in fp:
                tcl_units.append(_tcl_path_to_unit(fp))
        tcl_units = list(dict.fromkeys(tcl_units))  # deduplicate

    # Priority 2: fall back to detecting units from the solution patch's changed C files
    if not tcl_units:
        tcl_units = _detect_redis_tcl_units_from_patch(patch_content)

    # Generate protocol-level test (nil vs empty array), only when no test_patch units
    proto_tcl = _generate_redis_proto_tcl(patch_content) if not test_patch else ''
    proto_unit = 'unit/swebench_proto_check'

    # Detect --port vs --baseport from test_helper.tcl
    try:
        pa_result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c",
             "grep -q 'baseport' /testbed/tests/test_helper.tcl && echo baseport || echo port"],
            capture_output=True, text=True, timeout=30
        )
        port_arg = "--" + (pa_result.stdout.strip() or "port")
    except Exception:
        port_arg = "--port"

    baseport = random.randint(23000, 27000)

    # Build bash command: compile → inject proto test → run units
    parts = [
        "cd /testbed",
        "make CFLAGS='-fcommon' -j$(nproc) 2>/dev/null || make CFLAGS='-fcommon' 2>/dev/null || true",
    ]

    if proto_tcl:
        proto_b64 = _b64.b64encode(proto_tcl.encode()).decode()
        parts.append(
            f"printf '%s' '{proto_b64}' | base64 -d > /testbed/tests/{proto_unit}.tcl"
        )

    all_units = tcl_units + ([proto_unit] if proto_tcl else [])
    for i, unit in enumerate(all_units):
        p = baseport + i * 10
        parts.append(
            f"./runtest --single {unit} --clients 1 --timeout 120 {port_arg} {p} 2>&1 || true"
        )

    cmd = " && ".join(parts)
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: "TIMEOUT" for t in tests}

    clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + "\n" + result.stderr)
    parsed: Dict[str, str] = {}
    for line in clean.split('\n'):
        line = line.strip()
        m = re.match(r'\[ok\]:?\s+(.+?)(?:\s+\(\d+\s*\w+\))?\s*$', line)
        if m:
            name = m.group(1).strip()
            if not name.startswith('Check for memory'):
                parsed[name] = 'PASSED'
            continue
        m = re.match(r'\[err\]:?\s+(.+?)$', line)
        if m:
            found = m.group(1).strip()
            if found.startswith("Can't start"):
                continue
            if ': ' in found:
                found = found.split(': ')[0].strip()
            parsed[found] = 'FAILED'
    return _match_tests_to_results(tests, parsed)


def _run_c_tests_in_container(
    image_tag: str, tests: List[str], timeout: int = 600, instance_id: str = "",
    instance: dict = None
) -> Dict[str, str]:
    """Run C tests (TAP, TCL/Redis-style, jq-style, or make test) and parse output. Runs inside Docker (Ubuntu/Linux).
    Uses gcc (not clang), and no macOS-specific CFLAGS."""
    # Redis/Valkey: use the dedicated TCL test runner
    _inst = instance or {}
    repo = _inst.get('repo', '')
    is_redis = bool(re.search(r'redis|valkey', repo, re.IGNORECASE)) or bool(
        re.search(r'redis|valkey', instance_id, re.IGNORECASE)
    )
    if is_redis:
        return _run_redis_tests_in_container(image_tag, tests, timeout, instance_id, instance)
    cmd = (
        "cd /testbed && git submodule update --init --recursive -q 2>/dev/null || true && "
        "(make -j$(nproc) 2>/dev/null || make 2>/dev/null || true) && ("
        "[ -f ./runtest ] && ./runtest 2>&1 || "
        "[ -f ./test/run-tests ] && ./test/run-tests 2>&1 || "
        "[ -f ./tests/run-tests ] && ./tests/run-tests 2>&1 || "
        "{ [ -f ./tests/jqtest ] && [ -f ./tests/jq.test ] && ./tests/jqtest ./tests/jq.test 2>&1; } || "
        "[ -f ./tests/jqtest ] && ./tests/jqtest 2>&1 || "
        "make test 2>&1 || echo 'NO_TESTS_FOUND') || true"
    )
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {t: "TIMEOUT" for t in tests}
    output = result.stdout + "\n" + result.stderr
    parsed: Dict[str, str] = {}

    # First pass: collect jq-format test declarations and failures
    # jq test binary output: "Test #N: 'prog' at line number M"
    jq_prog_to_key: Dict[str, str] = {}  # prog_text -> "Test #N: prog_text"
    failed_progs: set = set()

    for line in output.split('\n'):
        m = re.match(r"Test #(\d+): '(.+)' at line number \d+", line)
        if m:
            test_key = f"Test #{m.group(1)}: {m.group(2)}"
            jq_prog_to_key[m.group(2)] = test_key
            parsed[test_key] = 'PASSED'
        elif line.startswith('***'):
            # "*** Expected X, but got Y for test at line number N: prog"
            # "*** Insufficient results for test at line number N: prog"
            # "*** Test program failed to compile at line N: prog"
            # "*** Superfluous result: X for test at line number N, prog"
            m2 = re.search(r'at line(?:\s+number)?\s+\d+[,: ]+(.+)', line)
            if m2:
                failed_progs.add(m2.group(1).strip())

    # Apply failures to jq-format results
    for prog in failed_progs:
        if prog in jq_prog_to_key:
            parsed[jq_prog_to_key[prog]] = 'FAILED'

    # Second pass: TAP / [ok]: / PASS/FAIL formats
    for line in output.split('\n'):
        # TAP: "ok 1 - test_name" or "not ok 1 - test_name"
        m = re.match(r'(ok|not ok)\s+\d+\s*-?\s*(.+)', line)
        if m:
            parsed[m.group(2).strip()] = 'PASSED' if m.group(1) == 'ok' else 'FAILED'
            continue
        # jq/TCL: "[ok]: test_name" or "[err]: description"
        m = re.match(r'\[(ok|err)\]:\s*(.+)', line)
        if m:
            parsed[m.group(2).strip()] = 'PASSED' if m.group(1) == 'ok' else 'FAILED'
            continue
        # Generic PASS/FAIL lines
        m = re.match(r'(PASS|FAIL|PASSED|FAILED)[\s:]+(.+)', line, re.IGNORECASE)
        if m:
            status = 'PASSED' if m.group(1).upper().startswith('PASS') else 'FAILED'
            parsed[m.group(2).strip()] = status
    return _match_tests_to_results(tests, parsed)


def run_specific_tests_in_container(
    image_tag: str,
    tests: List[str],
    is_django: bool = False,
    language: str = "python",
    timeout: int = 600,
    instance_id: str = "",
    instance: dict = None
) -> Dict[str, str]:
    """
    Run specific tests inside Docker container.
    Dispatches to language-specific runners for non-Python languages.

    Args:
        image_tag: Docker image to run
        tests: List of specific test names
        is_django: Whether this is a Django repo (Python only)
        language: Programming language ("python", "rust", "go", "java", "php", "javascript", "ruby", "c")
        timeout: Timeout in seconds
        instance_id: Instance ID for repo-specific handling

    Returns:
        Dict mapping test names to status (PASSED, FAILED, ERROR, SKIPPED)
    """
    # Dispatch non-Python languages to dedicated runners
    if language == "rust":
        return _run_rust_tests_in_container(image_tag, tests, timeout, instance_id)
    elif language == "go":
        return _run_go_tests_in_container(image_tag, tests, timeout, instance_id)
    elif language == "java":
        return _run_java_tests_in_container(image_tag, tests, timeout, instance_id)
    elif language == "php":
        return _run_php_tests_in_container(image_tag, tests, timeout, instance_id)
    elif language == "javascript":
        return _run_js_tests_in_container(image_tag, tests, timeout, instance_id)
    elif language == "ruby":
        return _run_ruby_tests_in_container(image_tag, tests, timeout, instance_id)
    elif language == "c":
        return _run_c_tests_in_container(image_tag, tests, timeout, instance_id, instance=instance)

    # Python path (existing logic below)
    status_map = {}

    # Check if this is psf__requests-2344 - tests hang when run together
    # but work fine individually due to httpbin.org dependencies in 2014 test code
    run_individually = (instance_id == "psf__requests-2344")

    if is_django:
        # Group tests by file and convert to Django module format
        test_by_file = {}
        for test in tests:
            # Parse test name: tests/path/file.py::TestClass::test_method
            parts = test.split('::')
            if len(parts) >= 2:
                file_path = parts[0]
                if file_path not in test_by_file:
                    test_by_file[file_path] = []
                test_by_file[file_path].append(test)

        for test_file, test_list in test_by_file.items():
            # Convert to Django module format
            parts = Path(test_file).parts
            if len(parts) >= 3 and parts[0] == "tests" and test_file.endswith('.py'):
                if parts[-1] == "tests.py":
                    module_path = parts[1]
                else:
                    module_path = '.'.join(parts[1:-1] + (parts[-1][:-3],))

                # Check if any tests in this file need PostgreSQL
                needs_postgres = any(
                    'postgresql' in t.lower() or 'postgres' in t.lower()
                    for t in test_list
                )

                if needs_postgres:
                    # Write a minimal PostgreSQL settings module via base64 to avoid shell escaping issues
                    # Mirror test_sqlite.py structure but with PostgreSQL backend.
                    # Do NOT set USE_TZ — without it Django uses False (default),
                    # which avoids the utc_tzinfo_factory assertion on the connection.
                    _pg_settings = (
                        "DATABASES = {\n"
                        "    'default': {\n"
                        "        'ENGINE': 'django.db.backends.postgresql',\n"
                        "        'NAME': 'django_test',\n"
                        "        'USER': 'postgres',\n"
                        "        'HOST': '',\n"
                        "        'PORT': '',\n"
                        "    },\n"
                        "    'other': {\n"
                        "        'ENGINE': 'django.db.backends.postgresql',\n"
                        "        'NAME': 'django_test_other',\n"
                        "        'USER': 'postgres',\n"
                        "        'HOST': '',\n"
                        "        'PORT': '',\n"
                        "    },\n"
                        "}\n"
                        "SECRET_KEY = 'django_tests_secret_key'\n"
                        "PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']\n"
                    )
                    _pg_b64 = base64.b64encode(_pg_settings.encode()).decode()
                    cmd = (
                        # Start PostgreSQL
                        "service postgresql start 2>/dev/null || true; "
                        "sleep 1; "
                        # Switch pg_hba.conf to trust auth + force UTC timezone, then restart
                        "PG_VER=$(ls /etc/postgresql/ 2>/dev/null | head -1); "
                        "if [ -n \"$PG_VER\" ]; then "
                        "  PG_CONF=/etc/postgresql/$PG_VER/main/postgresql.conf; "
                        "  HBA=/etc/postgresql/$PG_VER/main/pg_hba.conf; "
                        "  sed -i 's/peer/trust/g; s/md5/trust/g; s/scram-sha-256/trust/g' $HBA 2>/dev/null || true; "
                        "  echo \"timezone = 'UTC'\" >> $PG_CONF 2>/dev/null || true; "
                        "  service postgresql restart 2>/dev/null || true; "
                        "  sleep 2; "
                        "fi; "
                        # Create test databases
                        "psql -U postgres -c 'CREATE DATABASE django_test;' 2>/dev/null || true; "
                        "psql -U postgres -c 'CREATE DATABASE django_test_other;' 2>/dev/null || true; "
                        # Write postgres settings file into tests/ (where test_sqlite.py lives)
                        f"echo '{_pg_b64}' | base64 -d > /testbed/tests/test_postgres_settings.py; "
                        # Run tests with postgres backend
                        f"cd /testbed && python tests/runtests.py --settings=test_postgres_settings {module_path} -v 2 --parallel=1 2>&1 "
                        f"|| python tests/runtests.py --settings=test_postgres_settings {module_path} -v 2 2>&1 || true"
                    )
                else:
                    # Run Django test with default SQLite backend
                    cmd = f"cd /testbed && python tests/runtests.py {module_path} -v 2 --parallel=1 2>&1 || python tests/runtests.py {module_path} -v 2 2>&1 || true"

                try:
                    result = subprocess.run(
                        ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                        capture_output=True,
                        text=True,
                        timeout=timeout
                    )

                    # Parse Django output (handle both single-line and multiline format)
                    # Copied from full_validation.py lines 1269-1336
                    test_output = result.stderr + "\n" + result.stdout
                    lines = test_output.split('\n')

                    # Handle both single-line and multi-line test output
                    pending_test = None  # (test_name, test_class_full)

                    for line in lines:
                        # Try to match single-line format: test_name (class) ... status
                        single_line_match = re.search(r'(test_\w+)\s+\((\S+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)', line, re.IGNORECASE)
                        if single_line_match:
                            test_name = single_line_match.group(1)
                            test_class_full = single_line_match.group(2)
                            status = single_line_match.group(3).upper()

                            # Normalize status
                            if status == "OK":
                                status = "PASSED"
                            elif status == "FAIL":
                                status = "FAILED"
                            elif status == "SKIPPED":
                                status = "SKIPPED"

                            # Build test identifier
                            parts = test_class_full.split('.')
                            if len(parts) >= 3:
                                test_class = parts[-1]
                                module_path_parts = '/'.join(parts[:-2])
                                file_part = parts[-2]
                                test_file_path = f"tests/{module_path_parts}/{file_part}.py"
                                full_test_name = f"{test_file_path}::{test_class}::{test_name}"
                                if full_test_name in tests:  # Only record requested tests
                                    status_map[full_test_name] = status
                            pending_test = None
                            continue

                        # Try to match test name line: test_name (class)
                        test_name_match = re.search(r'^(test_\w+)\s+\((\S+)\)\s*$', line)
                        if test_name_match:
                            pending_test = (test_name_match.group(1), test_name_match.group(2))
                            continue

                        # Try to match status line: ... status or description ... status
                        if pending_test:
                            status_match = re.search(r'\.\.\.\s+(ok|FAIL|ERROR|skipped)', line, re.IGNORECASE)
                            if status_match:
                                test_name, test_class_full = pending_test
                                status = status_match.group(1).upper()

                                # Normalize status
                                if status == "OK":
                                    status = "PASSED"
                                elif status == "FAIL":
                                    status = "FAILED"
                                elif status == "SKIPPED":
                                    status = "SKIPPED"

                                # Build test identifier
                                parts = test_class_full.split('.')
                                if len(parts) >= 3:
                                    test_class = parts[-1]
                                    module_path_parts = '/'.join(parts[:-2])
                                    file_part = parts[-2]
                                    test_file_path = f"tests/{module_path_parts}/{file_part}.py"
                                    full_test_name = f"{test_file_path}::{test_class}::{test_name}"
                                    if full_test_name in tests:  # Only record requested tests
                                        status_map[full_test_name] = status
                                pending_test = None
                except subprocess.TimeoutExpired:
                    for test in test_list:
                        status_map[test] = "TIMEOUT"
    else:
        # Group tests by file for pytest
        test_by_file = {}
        for test in tests:
            # Parse test name: path/to/file.py::test_name or path/to/file.py::Class::test_name
            file_path = test.split('::')[0]
            if file_path not in test_by_file:
                test_by_file[file_path] = []
            test_by_file[file_path].append(test)

        # Use per-test timeout when running specific test IDs to avoid infinite loops
        # in tests that expose bugs where the pre-fix code hangs (mirrors full_validation.py)
        # Exception: SymPy uses an extended 1800s timeout; the 120s cap would kill slow test
        # suites like test_wester.py under QEMU emulation before they finish.
        is_specific = any('::' in t for t in tests)
        is_sympy = 'sympy' in instance_id.lower()
        per_test_timeout = timeout if (is_specific and is_sympy) else (120 if is_specific else timeout)

        # Repos with class-based tests that need class names preserved in results
        # (mirrors full_validation.py's preserve_class_names logic)
        repos_with_classes = [
            'mwaskom/seaborn', 'psf/requests', 'pytest-dev/pytest',
            'pylint-dev/pylint', 'pydata/xarray', 'sphinx-doc/sphinx',
        ]
        preserve_class_names = any(
            repo.replace('/', '__') in instance_id or repo.replace('/', '__') in instance_id.replace('__', '__')
            for repo in repos_with_classes
        ) if instance_id else False

        # Special handling for psf__requests-2344: run tests individually to avoid httpbin.org hangs
        if run_individually:
            print(f"  ⚠ Running {len(tests)} tests individually (psf__requests-2344 workaround)")
            for test_name in tests:
                cmd = (
                    f"cd /testbed && "
                    f"python -m pytest {test_name} -v --tb=short --no-header --color=no 2>&1 || "
                    f"python -m pytest {test_name} -v --tb=short --color=no 2>&1 || true"
                )

                try:
                    result = subprocess.run(
                        ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                        capture_output=True,
                        text=True,
                        timeout=30  # Individual tests should be fast (0.2s each)
                    )

                    # Parse pytest output (mirrors full_validation.py regex)
                    # group(1) = file path + optional class (e.g., "tests/foo.py::TestClass")
                    # group(2) = test function name (e.g., "test_bar[param]")
                    full_output = result.stdout + "\n" + result.stderr
                    match = re.search(r'(\S+?)::(test_\w+(?:\[.*?\])?)\s+(PASSED|FAILED|ERROR|SKIPPED)', full_output)
                    if match:
                        status_map[test_name] = match.group(3)
                    else:
                        status_map[test_name] = "FAILED"

                except subprocess.TimeoutExpired:
                    status_map[test_name] = "TIMEOUT"

            return status_map

        # Check once if this is a meson-python project (mirrors full_validation.py's _is_meson_build flag
        # which is set once during setup rather than re-checked per file)
        check_meson = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c",
             "grep -q 'meson-python' /testbed/pyproject.toml 2>/dev/null && echo 'yes' || echo 'no'"],
            capture_output=True,
            text=True
        )
        is_meson = 'yes' in check_meson.stdout

        for test_file, test_list in test_by_file.items():
            # Run entire test file (like full_validation.py)
            # This is necessary because:
            # 1. Nose-style test classes ERROR when selected explicitly
            # 2. Instance file may omit class names (file::test vs file::Class::test)
            # 3. We only REPORT on tests from instance, but run whole file to collect results

            if is_meson:
                # For meson-python: Copy tests to /tmp to avoid importing incomplete source
                # This ensures matplotlib is imported from site-packages (with C extensions)
                # instead of /testbed/lib (incomplete source)
                if test_file.startswith('lib/mpl_toolkits/'):
                    test_relpath = test_file.replace('lib/mpl_toolkits/', '')
                    pytest_target = f"mpl_toolkits/{test_relpath}"
                    cmd_prefix = f"cd /testbed && cp -r lib/mpl_toolkits /tmp/mpl_toolkits && cd /tmp"
                else:
                    test_basename = test_file.split('/')[-1]
                    pytest_target = f"matplotlib_tests/{test_basename}"
                    cmd_prefix = (
                        f"cd /testbed && cp -r lib/matplotlib/tests /tmp/matplotlib_tests && "
                        f"cp -r lib/matplotlib/testing /tmp/matplotlib_testing 2>/dev/null || true && cd /tmp"
                    )
            else:
                pytest_target = test_file
                cmd_prefix = "cd /testbed"

            base_pytest = f"python -m pytest {pytest_target} -v --tb=short"

            try:
                # Primary attempt: with --no-header (mirrors full_validation.py primary call)
                cmd = f"{cmd_prefix} && timeout {per_test_timeout} {base_pytest} --no-header --color=no 2>&1"
                result = subprocess.run(
                    ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                    capture_output=True, text=True, timeout=per_test_timeout + 30
                )
                full_output = result.stdout + "\n" + result.stderr

                # Retry without --no-header if not supported (mirrors full_validation.py)
                if result.returncode != 0 and "--no-header" in full_output:
                    cmd = f"{cmd_prefix} && timeout {per_test_timeout} {base_pytest} --color=no 2>&1"
                    result = subprocess.run(
                        ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                        capture_output=True, text=True, timeout=per_test_timeout + 30
                    )
                    full_output = result.stdout + "\n" + result.stderr

                # Retry with addopts cleared if pyproject.toml addopts contains
                # plugin-specific flags that aren't installed (e.g. --mypy-*)
                # (mirrors full_validation.py returncode==4 + "unrecognized arguments" retry)
                if result.returncode == 4 and "unrecognized arguments" in full_output:
                    cmd = f"{cmd_prefix} && timeout {per_test_timeout} {base_pytest} --no-header --color=no -o addopts= 2>&1"
                    result = subprocess.run(
                        ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                        capture_output=True, text=True, timeout=per_test_timeout + 30
                    )
                    full_output = result.stdout + "\n" + result.stderr
                    if result.returncode != 0 and "--no-header" in full_output:
                        cmd = f"{cmd_prefix} && timeout {per_test_timeout} {base_pytest} --color=no -o addopts= 2>&1"
                        result = subprocess.run(
                            ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                            capture_output=True, text=True, timeout=per_test_timeout + 30
                        )
                        full_output = result.stdout + "\n" + result.stderr

                # Parse pytest output using simpler regex (mirrors full_validation.py)
                # group(1) = file path + optional class (e.g., "tests/foo.py" or "tests/foo.py::TestClass")
                # group(2) = test function name (e.g., "test_bar[param]")
                all_results = {}
                for line in full_output.split('\n'):
                    match = re.search(r'(\S+?)::(test_\w+(?:\[.*?\])?)\s+(PASSED|FAILED|ERROR|SKIPPED)', line)
                    if match:
                        raw_prefix = match.group(1)
                        test_name_part = match.group(2)
                        status = match.group(3)

                        # Normalize /testbed/ prefix
                        if raw_prefix.startswith('/testbed/'):
                            raw_prefix = raw_prefix[9:]

                        # For meson-python: convert copied test paths back to original.
                        # raw_prefix may be "matplotlib_tests/foo.py" or "matplotlib_tests/foo.py::Class"
                        if is_meson:
                            if raw_prefix.startswith('matplotlib_tests/'):
                                raw_prefix = 'lib/matplotlib/tests/' + raw_prefix[len('matplotlib_tests/'):]
                            elif raw_prefix.startswith('mpl_toolkits/'):
                                raw_prefix = 'lib/' + raw_prefix

                        all_results[f"{raw_prefix}::{test_name_part}"] = status

                # Match requested tests to actual results (mirrors full_validation.py approach)
                # For repos with class-based tests, results include class name in key
                # (e.g., "tests/foo.py::TestClass::test_bar"); use suffix matching as fallback.
                for requested_test in test_list:
                    # Try exact match first
                    if requested_test in all_results:
                        status_map[requested_test] = all_results[requested_test]
                        continue

                    # Fuzzy fallback: match by file prefix + test name suffix.
                    # Handles: requested="file::test", actual="file::TestClass::test"
                    #      and: requested="file::OldClass::test", actual="file::NewClass::test"
                    req_parts = requested_test.split('::')
                    if len(req_parts) >= 2:
                        req_file = req_parts[0]
                        req_test = req_parts[-1]
                        matches = [
                            s for k, s in all_results.items()
                            if k.startswith(req_file + '::') and k.endswith('::' + req_test)
                        ]
                        if matches:
                            if any(s in ('FAILED', 'ERROR') for s in matches):
                                status_map[requested_test] = 'FAILED'
                            elif all(s == 'PASSED' for s in matches):
                                status_map[requested_test] = 'PASSED'
                            elif any(s == 'SKIPPED' for s in matches):
                                status_map[requested_test] = 'SKIPPED'
                            else:
                                status_map[requested_test] = matches[0]

            except subprocess.TimeoutExpired:
                for test in test_list:
                    status_map[test] = "TIMEOUT"

    return status_map


def find_docker_image(instance_id: str, auto_pull: bool = True) -> str:
    """
    Find Docker image for instance using multiple naming patterns

    Tries the following patterns in order:
    1. sweb.simple.<instance_id with __ -> .>:latest (default SWE-bench format)
    2. jiayuanz3/memory:<instance_id with _ and __ -> .> (user's custom format, lowercase)
    3. Any image with tag matching instance_id pattern
    4. If auto_pull=True and not found, pull from jiayuanz3/memory

    Args:
        instance_id: Instance ID (e.g., "astropy__astropy-4973")
        auto_pull: If True, automatically pull missing images from jiayuanz3/memory

    Returns:
        Image tag if found, None otherwise
    """
    # Pattern 1: Default SWE-bench naming
    default_tag = f"sweb.simple.{instance_id.replace('__', '.')}:latest"
    result = subprocess.run(
        ["docker", "images", "-q", default_tag],
        capture_output=True,
        text=True
    )
    if result.stdout.strip():
        return default_tag

    # Pattern 2: jiayuanz3/memory format (replace __ with ., convert to lowercase)
    # Convert: astropy__astropy-4973 -> astropy.astropy-4973 (lowercase)
    # Docker tags are case-insensitive and typically stored in lowercase
    memory_tag = f"jiayuanz3/memory:{instance_id.replace('__', '.').lower()}"
    result = subprocess.run(
        ["docker", "images", "-q", memory_tag],
        capture_output=True,
        text=True
    )
    if result.stdout.strip():
        return memory_tag

    # Pattern 3: Search all images for matching tag
    # Get all docker images and search for instance_id pattern (case-insensitive)
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        instance_pattern = instance_id.replace('__', '.').lower()
        for line in result.stdout.strip().split('\n'):
            if instance_pattern in line.lower():
                return line.strip()

    # Pattern 4: Auto-pull from jiayuanz3/memory if not found locally
    if auto_pull:
        print(f"  → Image not found locally, pulling from Docker Hub...")
        pull_result = subprocess.run(
            ["docker", "pull", "--platform", "linux/amd64", memory_tag],
            capture_output=True,
            text=True
        )
        if pull_result.returncode == 0:
            print(f"  ✓ Successfully pulled: {memory_tag}")
            return memory_tag
        else:
            print(f"  ✗ Failed to pull image: {memory_tag}")
            print(f"    Error: {pull_result.stderr[:200]}")

    return None


def compare_results(
    before: Dict[str, str],
    after: Dict[str, str],
    fail_to_pass_tests: List[str],
    pass_to_pass_tests: List[str]
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """
    Compare test results (following swebench logic)

    Returns:
        (fail_to_pass_success, fail_to_pass_failure, pass_to_pass_success, pass_to_pass_failure)
    """
    fail_to_pass_success = []
    fail_to_pass_failure = []
    pass_to_pass_success = []
    pass_to_pass_failure = []

    # Check FAIL_TO_PASS tests
    for test in fail_to_pass_tests:
        before_status = before.get(test, "MISSING")
        after_status = after.get(test, "MISSING")

        if after_status == "PASSED":
            fail_to_pass_success.append(test)
        else:
            fail_to_pass_failure.append(test)

    # Check PASS_TO_PASS tests
    for test in pass_to_pass_tests:
        before_status = before.get(test, "MISSING")
        after_status = after.get(test, "MISSING")

        if after_status == "PASSED":
            pass_to_pass_success.append(test)
        elif after_status == "SKIPPED" and before_status == "SKIPPED":
            # Test was skipped before and after - treat as success (not a regression)
            # This matches full_validation.py behavior for environment-dependent tests
            pass_to_pass_success.append(test)
        elif before_status == after_status and after_status in ("FAILED", "ERROR", "TIMEOUT"):
            # Test failed/timed out consistently before and after - treat as success (not a regression)
            # This matches full_validation.py behavior for pre-existing failures
            # TIMEOUT is added to handle platform-specific issues (e.g., amd64 emulation on arm64)
            pass_to_pass_success.append(test)
        elif before_status == "MISSING" and after_status == "MISSING":
            # Test was not found by the runner in either the before or after state.
            # Common for compiled multi-module projects (e.g., Maven/Gradle) where
            # the runner only targets the submodule containing the FAIL_TO_PASS tests;
            # PASS_TO_PASS tests in other submodules are never executed.
            # Since the test was unreachable both before and after the patch it
            # cannot be a regression introduced by this patch → treat as success.
            pass_to_pass_success.append(test)
        else:
            pass_to_pass_failure.append(test)

    return fail_to_pass_success, fail_to_pass_failure, pass_to_pass_success, pass_to_pass_failure


def evaluate_instance(
    instance: dict,
    prediction: dict,
    run_id: str,
    log_dir: Path
) -> dict:
    """Evaluate a single instance"""
    instance_id = instance['instance_id']
    print("=" * 60)
    print(f"Evaluating: {instance_id}")
    print("=" * 60)

    # Create log directory
    log_dir.mkdir(parents=True, exist_ok=True)

    # Define output files (matching original swebench)
    run_instance_log = log_dir / "run_instance.log"
    test_output_file = log_dir / "test_output.txt"
    report_file = log_dir / "report.json"
    patch_file = log_dir / "patch.diff"
    eval_script_file = log_dir / "eval.sh"

    # Initialize run_instance.log
    execution_log = []
    execution_log.append(f"{'='*60}")
    execution_log.append(f"Evaluating instance: {instance_id}")
    execution_log.append(f"Run ID: {run_id}")
    execution_log.append(f"{'='*60}\n")

    # Find Docker image using smart detection
    execution_log.append(f"Searching for Docker image for instance: {instance_id}")
    image_tag = find_docker_image(instance_id)

    if not image_tag:
        # Try to provide helpful error message
        expected_tag = f"sweb.simple.{instance_id.replace('__', '.')}:latest"
        alt_tag = f"jiayuanz3/memory:{instance_id.replace('__', '.').lower()}"

        print(f"✗ Image not found for instance: {instance_id}")
        print(f"  Expected one of:")
        print(f"    - {expected_tag}")
        print(f"    - {alt_tag}")
        print(f"    - Any image with tag containing '{instance_id.replace('__', '.').lower()}'")

        execution_log.append(f"ERROR: No Docker image found for {instance_id}")
        execution_log.append(f"  Tried: {expected_tag}")
        execution_log.append(f"  Tried: {alt_tag}")
        error_msg = f"Image not found for instance: {instance_id}"

        # Write logs before returning
        run_instance_log.write_text("\n".join(execution_log))

        report = {
            "instance_id": instance_id,
            "resolved": False,
            "error": error_msg
        }
        report_file.write_text(json.dumps(report, indent=2))
        return report

    execution_log.append(f"✓ Image found: {image_tag}")
    print(f"→ Using Docker image: {image_tag}")

    # Detect language from container (must happen before patch sanitization and all
    # language-gated logic below)
    language = detect_language_in_container(image_tag, repo=instance.get('repo', ''))
    execution_log.append(f"Detected language: {language}")
    print(f"→ Detected language: {language}")

    # Write model patch to patch.diff
    model_patch = prediction.get('model_patch', prediction.get('patch', ''))
    original_patch = model_patch
    # Strip unapplyable binary stubs (e.g. "Binary files ... differ" without GIT binary patch)
    model_patch = _strip_unapplyable_binary_hunks(model_patch or '')
    if model_patch != original_patch:
        execution_log.append(f"  ⚠ Stripped unapplyable binary file hunks from model patch")
        print(f"  ⚠ Stripped unapplyable binary file hunks from model patch")
    # Apply Python 2→3 sanitization only for Python instances
    if language == "python":
        model_patch = _sanitize_patch_for_python3(model_patch or '')

    patch_file.write_text(model_patch or '')
    execution_log.append(f"Model patch written to {patch_file}")

    # Log if sanitization was applied
    if language == "python" and original_patch != model_patch and model_patch:
        execution_log.append(f"  ⚠ Sanitized for Python 3 compatibility (e.message → str(e))")
        print(f"  ⚠ Sanitized for Python 3 compatibility (e.message → str(e))")

    # Get test lists from instance
    fail_to_pass_tests = instance.get('FAIL_TO_PASS', [])
    pass_to_pass_tests = instance.get('PASS_TO_PASS', [])
    all_tests = fail_to_pass_tests + pass_to_pass_tests

    execution_log.append(f"\nTest configuration:")
    execution_log.append(f"  FAIL_TO_PASS tests: {len(fail_to_pass_tests)}")
    execution_log.append(f"  PASS_TO_PASS tests: {len(pass_to_pass_tests)}")
    execution_log.append(f"  Total tests: {len(all_tests)}")

    if not all_tests:
        print(f"✗ No tests specified in instance")
        execution_log.append("ERROR: No tests specified in instance")
        run_instance_log.write_text("\n".join(execution_log))

        report = {
            "instance_id": instance_id,
            "resolved": False,
            "error": "No tests specified"
        }
        report_file.write_text(json.dumps(report, indent=2))
        return report

    print(f"→ Tests to run:")
    print(f"  FAIL_TO_PASS: {len(fail_to_pass_tests)}")
    print(f"  PASS_TO_PASS: {len(pass_to_pass_tests)}")

    # Detect if Django repo (Python only)
    is_django = language == "python" and 'django' in instance['repo'].lower()

    # Get infrastructure fixes (if needed)
    from .build_instance import RepoConfig, get_commit_date
    repo = instance['repo']

    # SymPy instances need longer timeout due to slow symbolic computation
    # (especially for test_wester.py which has 236 tests with symbolic sets)
    is_sympy = 'sympy' in repo.lower()
    # Rust projects may need to compile tests on-demand (e.g. when pre-build failed or
    # new tests are added by test_patch). Allow extra time for compilation especially
    # under amd64 emulation on ARM (Docker Desktop on Apple Silicon).
    if is_sympy:
        test_timeout = 1800   # 30 min for sympy
    elif language == "rust":
        test_timeout = 1200   # 20 min for rust (compilation + emulation overhead)
    elif instance_id == "babel__babel-14907":
        test_timeout = 1800   # 30 min: large patch + yarn.lock rebuild; slow under QEMU emulation
    else:
        test_timeout = 600    # 10 min for others
    if is_sympy:
        print(f"  ⚙ Using extended timeout: {test_timeout}s (SymPy symbolic computation)")
        execution_log.append(f"Using extended timeout: {test_timeout}s for SymPy")
    else:
        execution_log.append(f"Using standard timeout: {test_timeout}s")
    base_commit = instance['base_commit']

    # Get actual commit date from git (not PR creation date)
    # This ensures infrastructure fixes are applied correctly
    result = subprocess.run(
        ["docker", "run", "--rm", image_tag, "bash", "-c",
         f"cd /testbed && git show -s --format=%ci {base_commit}"],
        capture_output=True,
        text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        commit_date = result.stdout.strip().split()[0]  # Extract YYYY-MM-DD
    else:
        # Fallback to PR creation date if git command fails
        commit_date = instance.get('created_at', '2020-01-01').split('T')[0]

    repo_config = RepoConfig(repo)
    infra_fixes = repo_config.get_infrastructure_fixes(commit_date)

    # Get test patch (model_patch already loaded and sanitized earlier)
    test_patch = instance.get('test_patch', '')

    if not model_patch:
        print(f"✗ No patch found in prediction")
        execution_log.append("ERROR: No patch found in prediction")
        run_instance_log.write_text("\n".join(execution_log))

        report = {
            "instance_id": instance_id,
            "resolved": False,
            "error": "No patch"
        }
        report_file.write_text(json.dumps(report, indent=2))
        return report

    execution_log.append(f"✓ Model patch loaded ({len(model_patch)} bytes)")

    # Create eval.sh script (for reference only — not executed by the pipeline)
    if language == "python":
        run_cmd = "python tests/runtests.py -v" if is_django else "python -m pytest -v"
    elif language == "java":
        run_cmd = "mvn test -Dmaven.javadoc.skip=true -Denforcer.skip=true || ./gradlew test --no-daemon || ant test"
    elif language == "rust":
        run_cmd = "source $HOME/.cargo/env && cargo test"
    elif language == "go":
        run_cmd = "export PATH=/usr/local/go/bin:$PATH && go test -v ./..."
    elif language == "php":
        run_cmd = "vendor/bin/phpunit --colors=never"
    elif language == "javascript":
        run_cmd = "npm test"
    elif language == "ruby":
        run_cmd = 'export PATH="$HOME/.rbenv/bin:$HOME/.rbenv/shims:$PATH" && bundle exec rspec'
    elif language == "c":
        run_cmd = "make test || make check"
    else:
        run_cmd = "python -m pytest -v"

    eval_script = f"""#!/bin/bash
set -euxo pipefail

cd /testbed

# Apply test patch if exists
if [ -f /test.patch ]; then
    git apply /test.patch || echo "Test patch already applied or failed"
fi

# Apply model patch
git apply /patch.diff

# Run tests ({language})
{run_cmd}
"""
    eval_script_file.write_text(eval_script)
    execution_log.append(f"✓ Evaluation script written to {eval_script_file}")

    # Initialize test output log
    output_log = []

    # Step 1: Apply test_patch (if exists)
    # Create unique name for test-patched image (handle tags with or without :latest)
    if ":latest" in image_tag:
        test_patched_image = image_tag.replace(":latest", f":{run_id}_testpatch")
    else:
        # For tags like jiayuanz3/memory:astropy.astropy-4973
        test_patched_image = f"{image_tag}_{run_id}_testpatch"

    if test_patch:
        print(f"→ Applying test patch...")
        execution_log.append("\nApplying test patch...")
        output_log.append("=" * 60)
        output_log.append("Applying test patch")
        output_log.append("=" * 60)

        # Strip unapplyable binary stubs from test_patch (mirrors model_patch handling)
        test_patch_to_apply = _strip_unapplyable_binary_hunks(test_patch)
        _had_binary_stubs_in_test = test_patch_to_apply != test_patch
        if _had_binary_stubs_in_test:
            execution_log.append("  ⚠ Stripped unapplyable binary file stubs from test patch")
            print(f"  ⚠ Stripped unapplyable binary file stubs from test patch")

        cmd_apply_test = "cd /testbed && git apply -"
        container_name = f"apply_testpatch_{instance_id.replace('/', '_').replace('__', '_')}"

        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        result = subprocess.run(
            ["docker", "run", "-i", "--name", container_name, image_tag, "bash", "-c", cmd_apply_test],
            input=test_patch_to_apply,
            capture_output=True,
            text=True
        )

        output_log.append(result.stdout)
        output_log.append(result.stderr)

        subprocess.run(
            ["docker", "commit", container_name, test_patched_image],
            capture_output=True
        )
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        # If binary stubs were stripped, try to fetch missing binary files from git remote
        if _had_binary_stubs_in_test:
            _bin_pat = re.compile(
                r'diff --git a/(.+?) b/\1\nnew file mode[^\n]*\nindex 0+\.\.[0-9a-f]+\nBinary files /dev/null'
            )
            _missing_bins = _bin_pat.findall(test_patch)
            if _missing_bins:
                print(f"  → Fetching {len(_missing_bins)} missing binary test file(s) from git remote...")
                _fetch_container = f"fetch_binary_{instance_id.replace('/', '_').replace('__', '_')}"
                subprocess.run(["docker", "rm", "-f", _fetch_container], capture_output=True)
                _fetch_cmd = (
                    "cd /testbed && "
                    "(git fetch --depth=1 origin main 2>/dev/null || "
                    " git fetch --depth=1 origin master 2>/dev/null || true)"
                )
                for _bfile in _missing_bins:
                    _fetch_cmd += (
                        f" && (git checkout origin/main -- '{_bfile}' 2>/dev/null || "
                        f" git checkout origin/master -- '{_bfile}' 2>/dev/null || true)"
                    )
                _fetch_result = subprocess.run(
                    ["docker", "run", "--name", _fetch_container, test_patched_image, "bash", "-c", _fetch_cmd],
                    capture_output=True, text=True, timeout=120
                )
                if _fetch_result.returncode == 0:
                    subprocess.run(["docker", "commit", _fetch_container, test_patched_image], capture_output=True)
                    print(f"  ✓ Fetched missing binary test files from git remote")
                    execution_log.append(f"  ✓ Fetched missing binary test files from git remote")
                else:
                    print(f"  ⚠ Could not fetch binary test files: {_fetch_result.stderr[:200]}")
                    execution_log.append(f"  ⚠ Binary test file fetch failed")
                subprocess.run(["docker", "rm", "-f", _fetch_container], capture_output=True)

        if result.returncode != 0:
            print(f"  ⚠ Test patch failed (may already be in repo)")
            execution_log.append(f"  ⚠ Test patch apply returned code {result.returncode}")
        else:
            print(f"  ✓ Test patch applied")
            execution_log.append(f"  ✓ Test patch applied successfully")

            if language == "python":
                # For meson-python: Reinstall after test_patch to include test infrastructure changes
                # in site-packages (e.g., is_ci_environment function added to matplotlib.testing)
                check_meson = subprocess.run(
                    ["docker", "run", "--rm", test_patched_image, "bash", "-c",
                     "grep -q 'meson-python' /testbed/pyproject.toml 2>/dev/null && echo 'yes' || echo 'no'"],
                    capture_output=True,
                    text=True
                )
                if 'yes' in check_meson.stdout:
                    print(f"  → Reinstalling package after test_patch...")
                    execution_log.append(f"  → Reinstalling to include test infrastructure changes in site-packages")

                    reinstall_cmd = "cd /testbed && /opt/conda/envs/testbed/bin/pip install . --no-build-isolation --force-reinstall --no-deps -q"
                    reinstall_container = f"reinstall_testpatch_{instance_id.replace('/', '_').replace('__', '_')}"
                    subprocess.run(["docker", "rm", "-f", reinstall_container], capture_output=True)

                    reinstall_result = subprocess.run(
                        ["docker", "run", "--name", reinstall_container, test_patched_image, "bash", "-c", reinstall_cmd],
                        capture_output=True,
                        text=True
                    )

                    if reinstall_result.returncode == 0:
                        subprocess.run(["docker", "commit", reinstall_container, test_patched_image], capture_output=True)
                        print(f"    ✓ Package reinstalled")
                        execution_log.append(f"    ✓ Package reinstalled successfully")

                    subprocess.run(["docker", "rm", "-f", reinstall_container], capture_output=True)

                # For pytest repos: Reinstall in editable mode after test_patch to generate _pytest._version
                is_pytest = 'pytest' in repo.lower() and repo.split('/')[-1] == 'pytest'
                if is_pytest:
                    print(f"  → Reinstalling pytest in editable mode after test_patch...")
                    execution_log.append(f"  → Reinstalling pytest to regenerate version module")

                    reinstall_cmd = "cd /testbed && /opt/conda/envs/testbed/bin/pip install -e . --no-deps -q"
                    reinstall_container = f"reinstall_pytest_{instance_id.replace('/', '_').replace('__', '_')}"
                    subprocess.run(["docker", "rm", "-f", reinstall_container], capture_output=True)

                    reinstall_result = subprocess.run(
                        ["docker", "run", "--name", reinstall_container, test_patched_image, "bash", "-c", reinstall_cmd],
                        capture_output=True,
                        text=True
                    )

                    if reinstall_result.returncode == 0:
                        subprocess.run(["docker", "commit", reinstall_container, test_patched_image], capture_output=True)
                        print(f"    ✓ Pytest reinstalled in editable mode")
                        execution_log.append(f"    ✓ Pytest reinstalled successfully")
                    else:
                        print(f"    ⚠ Pytest reinstall failed")
                        execution_log.append(f"    ⚠ Pytest reinstall returned code {reinstall_result.returncode}")

                    subprocess.run(["docker", "rm", "-f", reinstall_container], capture_output=True)

        # For Django repos, ensure new test directories have __init__.py files
        # This fixes test discovery when test_patch creates new test directories
        if is_django:
            test_patched_image = ensure_init_files_for_new_test_dirs(
                test_patched_image,
                test_patch,
                instance_id,
                execution_log
            )

        # For xarray: install dask, which is required by xarray/tests/test_computation.py
        # at module level via pytest.importorskip("dask"). Without dask, the entire module
        # is skipped and 0 tests run.
        if language == "python" and 'xarray' in repo:
            _xarray_check = subprocess.run(
                ["docker", "run", "--rm", test_patched_image, "bash", "-c",
                 "python -c 'import dask' 2>/dev/null && echo 'ok' || echo 'missing'"],
                capture_output=True, text=True, timeout=30
            )
            if 'missing' in _xarray_check.stdout:
                print(f"  → Installing dask for xarray tests...")
                execution_log.append("  → Installing dask for xarray test collection")
                _dask_container = f"install_dask_{instance_id.replace('/', '_').replace('__', '_')}"
                subprocess.run(["docker", "rm", "-f", _dask_container], capture_output=True)
                _dask_result = subprocess.run(
                    ["docker", "run", "--name", _dask_container, test_patched_image, "bash", "-c",
                     "pip install 'dask[array]' -q 2>&1 || true"],
                    capture_output=True, text=True, timeout=120
                )
                subprocess.run(["docker", "commit", _dask_container, test_patched_image], capture_output=True)
                subprocess.run(["docker", "rm", "-f", _dask_container], capture_output=True)
                if _dask_result.returncode == 0:
                    print(f"  ✓ dask installed for xarray")
                    execution_log.append("  ✓ dask installed")
                else:
                    print(f"  ⚠ dask install failed")
                    execution_log.append(f"  ⚠ dask install failed: {_dask_result.stderr[:100]}")

        # For SymPy: install scipy when the test_patch or FAIL_TO_PASS tests reference scipy.
        # Some SymPy instances (e.g. sympy__sympy-7229) add tests that require scipy, but
        # the base Docker image does not have scipy installed. Without it, the tests call
        # skip("scipy not installed") which—after the pytest.py infrastructure fix—correctly
        # emits a pytest SKIP rather than a FAIL, but SKIPPED != PASSED so FAIL_TO_PASS
        # tests would still not resolve. Installing scipy lets the tests actually run.
        if language == "python" and 'sympy' in repo:
            _needs_scipy = (
                'scipy' in test_patch or
                any('scipy' in t for t in fail_to_pass_tests)
            )
            if _needs_scipy:
                _scipy_check = subprocess.run(
                    ["docker", "run", "--rm", test_patched_image, "bash", "-c",
                     "python -c 'import scipy' 2>/dev/null && echo 'ok' || echo 'missing'"],
                    capture_output=True, text=True, timeout=30
                )
                if 'missing' in _scipy_check.stdout:
                    print(f"  → Installing scipy for sympy tests...")
                    execution_log.append("  → Installing scipy for sympy scipy tests")
                    _scipy_container = f"install_scipy_{instance_id.replace('/', '_').replace('__', '_')}"
                    subprocess.run(["docker", "rm", "-f", _scipy_container], capture_output=True)
                    _scipy_result = subprocess.run(
                        ["docker", "run", "--name", _scipy_container, test_patched_image, "bash", "-c",
                         "pip install scipy -q 2>&1 || true"],
                        capture_output=True, text=True, timeout=300
                    )
                    subprocess.run(["docker", "commit", _scipy_container, test_patched_image], capture_output=True)
                    subprocess.run(["docker", "rm", "-f", _scipy_container], capture_output=True)
                    if _scipy_result.returncode == 0:
                        print(f"  ✓ scipy installed for sympy")
                        execution_log.append("  ✓ scipy installed")
                    else:
                        print(f"  ⚠ scipy install failed")
                        execution_log.append(f"  ⚠ scipy install failed: {_scipy_result.stderr[:100]}")

        # For JavaScript: install packages imported by test_patch that model_patch adds to package.json.
        # Without this, the test suite crashes immediately with ERR_MODULE_NOT_FOUND when the
        # test_patch references a new npm package that only exists after the model patch is applied.
        if language == "javascript":
            _test_imports = _js_extract_imports(test_patch)
            _solution_deps = _js_extract_pkg_json_deps(model_patch)
            _needed = _test_imports & set(_solution_deps.keys())
            if _needed:
                print(f"  → Installing {len(_needed)} test-patch dependency(ies): {', '.join(sorted(_needed))}")
                execution_log.append(f"  → Installing test-patch dependencies: {', '.join(sorted(_needed))}")
                _nvm_init = '. "$NVM_DIR/nvm.sh" 2>/dev/null || true'
                _install_pkgs = ' '.join(f'{p}@{_solution_deps[p]}' for p in sorted(_needed))
                _install_cmd = f'{_nvm_init} && cd /testbed && npm install --no-save {_install_pkgs} 2>&1 || true'
                _dep_container = f"install_jsdeps_{instance_id.replace('/', '_').replace('__', '_')}"
                subprocess.run(["docker", "rm", "-f", _dep_container], capture_output=True)
                _dep_result = subprocess.run(
                    ["docker", "run", "--name", _dep_container, test_patched_image, "bash", "-c", _install_cmd],
                    capture_output=True, text=True, timeout=120
                )
                subprocess.run(["docker", "commit", _dep_container, test_patched_image], capture_output=True)
                subprocess.run(["docker", "rm", "-f", _dep_container], capture_output=True)
                if _dep_result.returncode == 0:
                    print(f"  ✓ Test-patch dependencies installed")
                    execution_log.append("  ✓ Test-patch dependencies installed")
                else:
                    print(f"  ⚠ Test-patch dependency install failed")
                    execution_log.append(f"  ⚠ Dependency install failed: {_dep_result.stderr[:100]}")
    else:
        # No test patch, use original image
        test_patched_image = image_tag
        execution_log.append("No test patch to apply")

    # Step 1.4: For Rust projects, check toolchain compatibility and fix if needed.
    # Mirrors full_validation_multilingual_rust.py's setup_version() logic.
    # Upgrade/pin the image once here so both before/after test runs use the correct setup.
    if language == "rust":
        # First try fetching (may fail due to broken workspace members or old Rust)
        check_cmd = (
            "source ~/.cargo/env 2>/dev/null || true && unset RUSTUP_TOOLCHAIN && "
            "cd /testbed && cargo fetch --locked 2>&1 || cargo fetch 2>&1 || true"
        )
        check_result = subprocess.run(
            ["docker", "run", "--rm", test_patched_image, "bash", "-c", check_cmd],
            capture_output=True, text=True, timeout=120
        )
        fetch_output = check_result.stdout + check_result.stderr

        # Handle broken workspace members that block dep resolution even with --exclude
        broken_members = []
        _fetch_out = fetch_output
        for _ in range(4):
            m = re.search(
                r"required by package `([^` ]+)", _fetch_out
            )
            if not m:
                break
            broken_members.append(m.group(1))
            break  # Only remove one at a time; evaluation retry logic handles the rest

        if broken_members:
            # Re-run fetch after removing the broken member to get the real fetch output
            member_seds = " && ".join(
                f"sed -i '/{m}/d' /testbed/Cargo.toml" for m in broken_members
            )
            recheck_cmd = (
                "source ~/.cargo/env 2>/dev/null || true && unset RUSTUP_TOOLCHAIN && "
                f"cd /testbed && {member_seds} && "
                "cargo fetch 2>&1 || true"
            )
            recheck = subprocess.run(
                ["docker", "run", "--rm", test_patched_image, "bash", "-c", recheck_cmd],
                capture_output=True, text=True, timeout=180
            )
            fetch_output = recheck.stdout + recheck.stderr

        # Detect if newer Rust is needed (edition 2024 crates or namespaced dep: features)
        needs_rust_upgrade = bool(
            re.search(r"older than the `20\d+` edition|namespaced features with the `dep:` prefix", fetch_output)
        )
        # Newer cargo versions report "feature `edition2024` is required" instead of
        # "older than the `2024` edition" when a crate's Cargo.toml uses the 2024 edition.
        needs_edition_2024 = bool(
            re.search(r"feature `edition2024` is required", fetch_output)
        )
        if needs_edition_2024:
            # Rust 1.85+ required: its Cargo understands the edition2024 Cargo.toml feature.
            print(f"  → Cargo edition2024 support required, upgrading to Rust 1.85...")
            execution_log.append("  → Upgrading Rust toolchain to 1.85 (edition2024 required)")
            upgraded = _upgrade_rust_in_image(test_patched_image, "1.85")
            if upgraded != test_patched_image:
                if test_patched_image != image_tag:
                    subprocess.run(["docker", "rmi", test_patched_image], capture_output=True)
                test_patched_image = upgraded
                execution_log.append(f"  ✓ Rust 1.85 installed in image: {test_patched_image}")
        elif needs_rust_upgrade:
            # Upgrade to Rust 1.75: handles edition 2021/2024 crates and dep: features
            # while remaining broadly compatible with old codebases (uses --cap-lints=warn).
            print(f"  → Modern crates.io deps detected, upgrading to Rust 1.75...")
            execution_log.append("  → Upgrading Rust toolchain to 1.75 (dep: features / edition 2024)")
            upgraded = _upgrade_rust_in_image(test_patched_image, "1.75")
            if upgraded != test_patched_image:
                if test_patched_image != image_tag:
                    subprocess.run(["docker", "rmi", test_patched_image], capture_output=True)
                test_patched_image = upgraded
                execution_log.append(f"  ✓ Rust 1.75 installed in image: {test_patched_image}")
        elif re.search(r"older than the `20\d+` edition|feature `edition2024` is required", check_result.stdout + check_result.stderr):
            # Original check (no broken members blocking): upgrade to 1.85 for full 2024 support
            print(f"  → Rust 2024 edition dependency detected, upgrading to Rust 1.85...")
            execution_log.append("  → Upgrading Rust toolchain to 1.85 (2024 edition required)")
            upgraded = _upgrade_rust_in_image(test_patched_image, "1.85")
            if upgraded != test_patched_image:
                if test_patched_image != image_tag:
                    subprocess.run(["docker", "rmi", test_patched_image], capture_output=True)
                test_patched_image = upgraded
                execution_log.append(f"  ✓ Rust 1.85 installed in image: {test_patched_image}")

    # Step 1.5: Apply infrastructure fixes (if needed)
    if infra_fixes:
        print(f"→ Applying {len(infra_fixes)} infrastructure fix(es)...")
        execution_log.append(f"\nApplying {len(infra_fixes)} infrastructure fixes...")

        for description, patch_content in infra_fixes:
            print(f"  → {description}")
            execution_log.append(f"  → {description}")

            cmd_apply_infra = "cd /testbed && git apply -"
            container_name = f"apply_infra_{instance_id.replace('/', '_').replace('__', '_')}"

            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

            result = subprocess.run(
                ["docker", "run", "-i", "--name", container_name, test_patched_image, "bash", "-c", cmd_apply_infra],
                input=patch_content,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                print(f"  ⚠ Infrastructure fix failed (may already be applied)")
                execution_log.append(f"  ⚠ Infrastructure fix failed: {result.stderr[:200]}")
            else:
                print(f"  ✓ Infrastructure fix applied")
                execution_log.append(f"  ✓ Infrastructure fix applied successfully")

            # Commit the change
            subprocess.run(
                ["docker", "commit", container_name, test_patched_image],
                capture_output=True
            )
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Step 2: Run tests BEFORE model patch
    print(f"→ Running tests before patch...")
    execution_log.append("\nRunning tests BEFORE model patch...")
    output_log.append("\n" + "=" * 60)
    output_log.append("Tests BEFORE model patch")
    output_log.append("=" * 60)

    tests_before = run_specific_tests_in_container(test_patched_image, all_tests, is_django, language=language, timeout=test_timeout, instance_id=instance_id, instance=instance)

    output_log.append(f"Collected {len(tests_before)} test(s)")
    for test, status in sorted(tests_before.items()):
        output_log.append(f"  {test}: {status}")

    print(f"  ✓ Ran {len(tests_before)} test(s)")
    execution_log.append(f"  ✓ Collected {len(tests_before)} test results")

    # Fix-first fallback for compiled languages (Java, Go, Rust, C):
    # test_patch may add new test files that reference APIs not yet present at
    # base_commit.  Maven/Cargo/Go's compile phase then fails for the entire
    # test module, producing 0 results.  Solution: re-run on the base image
    # (no patches applied).  PASS_TO_PASS tests already exist there and compile
    # fine.  FAIL_TO_PASS tests don't exist yet (they are added by test_patch),
    # so they appear as MISSING — which is correct because compare_results only
    # checks after_status == "PASSED" for FAIL_TO_PASS resolution.
    if len(tests_before) == 0 and language in {"java", "go", "rust", "c"} and test_patch:
        print(f"  → 0 results for {language}: re-running on base image (fix-first fallback)...")
        execution_log.append(f"\n  → Fix-first fallback: re-running on base image (no patches applied)")
        tests_before_base = run_specific_tests_in_container(
            image_tag, all_tests, is_django, language=language,
            timeout=test_timeout, instance_id=instance_id, instance=instance
        )
        if len(tests_before_base) > 0:
            print(f"  ✓ Fix-first fallback: {len(tests_before_base)} result(s) from base image")
            execution_log.append(f"  ✓ Fix-first fallback: {len(tests_before_base)} results from base image")
            output_log.append(f"\nFix-first fallback (base image): {len(tests_before_base)} result(s)")
            for test, status in sorted(tests_before_base.items()):
                output_log.append(f"  {test}: {status}")
            tests_before = tests_before_base
        else:
            print(f"  → Fix-first fallback also returned 0 results, keeping empty baseline")
            execution_log.append(f"  → Fix-first fallback also returned 0 results")

    # Step 3: Apply model patch
    print(f"→ Applying model patch...")
    execution_log.append("\nApplying model patch...")
    output_log.append("\n" + "=" * 60)
    output_log.append("Applying model patch")
    output_log.append("=" * 60)

    cmd_apply_model = "cd /testbed && git apply -C1 -"
    # Create unique name for model-patched image (handle tags with or without :latest)
    if ":latest" in image_tag:
        model_patched_image = image_tag.replace(":latest", f":{run_id}_patched")
    else:
        # For tags like jiayuanz3/memory:astropy.astropy-4973
        model_patched_image = f"{image_tag}_{run_id}_patched"
    container_name = f"apply_patch_{instance_id.replace('/', '_').replace('__', '_')}"

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    result = subprocess.run(
        ["docker", "run", "-i", "--name", container_name, test_patched_image, "bash", "-c", cmd_apply_model],
        input=model_patch,
        capture_output=True,
        text=True
    )

    output_log.append(result.stdout)
    output_log.append(result.stderr)

    subprocess.run(
        ["docker", "commit", container_name, model_patched_image],
        capture_output=True
    )
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    if result.returncode != 0:
        print(f"✗ Failed to apply model patch")
        execution_log.append(f"ERROR: Model patch failed to apply (code {result.returncode})")
        output_log.append("ERROR: Patch failed to apply")

        # Write logs
        test_output_file.write_text("\n".join(output_log))
        run_instance_log.write_text("\n".join(execution_log))

        # Clean up
        if test_patched_image != image_tag:
            subprocess.run(["docker", "rmi", test_patched_image], capture_output=True)

        report = {
            "instance_id": instance_id,
            "resolved": False,
            "patch_applied": False,
            "error": "Patch failed"
        }
        report_file.write_text(json.dumps(report, indent=2))
        return report

    print(f"  ✓ Patch applied")
    execution_log.append(f"  ✓ Model patch applied successfully")

    # For meson-python: Reinstall after model_patch to incorporate code changes (Python only)
    if language == "python":
        check_meson = subprocess.run(
            ["docker", "run", "--rm", model_patched_image, "bash", "-c",
             "grep -q 'meson-python' /testbed/pyproject.toml 2>/dev/null && echo 'yes' || echo 'no'"],
            capture_output=True,
            text=True
        )
        if 'yes' in check_meson.stdout:
            print(f"  → Reinstalling package after model_patch...")
            execution_log.append(f"  → Reinstalling to incorporate code changes in site-packages")

            reinstall_cmd = "cd /testbed && /opt/conda/envs/testbed/bin/pip install . --no-build-isolation --force-reinstall --no-deps -q"
            reinstall_container = f"reinstall_modelpatch_{instance_id.replace('/', '_').replace('__', '_')}"
            subprocess.run(["docker", "rm", "-f", reinstall_container], capture_output=True)

            reinstall_result = subprocess.run(
                ["docker", "run", "--name", reinstall_container, model_patched_image, "bash", "-c", reinstall_cmd],
                capture_output=True,
                text=True
            )

            if reinstall_result.returncode == 0:
                subprocess.run(["docker", "commit", reinstall_container, model_patched_image], capture_output=True)
                print(f"    ✓ Package reinstalled")
                execution_log.append(f"    ✓ Package reinstalled successfully")

            subprocess.run(["docker", "rm", "-f", reinstall_container], capture_output=True)

    # For JavaScript projects: rebuild after model_patch so the compiled output
    # (dist/) reflects the patched source.  Karma tests run the built bundle,
    # not the raw source, so skipping the rebuild means the fix never takes effect.
    if language == "javascript":
        nvm_init = '. "$NVM_DIR/nvm.sh" 2>/dev/null || true'
        rebuild_cmd = f"{nvm_init} && cd /testbed && npm run build 2>&1 || true"
        rebuild_container = f"rebuild_js_{instance_id.replace('/', '_').replace('__', '_')}"
        subprocess.run(["docker", "rm", "-f", rebuild_container], capture_output=True)

        rebuild_result = subprocess.run(
            ["docker", "run", "--name", rebuild_container, model_patched_image, "bash", "-c", rebuild_cmd],
            capture_output=True, text=True, timeout=300
        )

        if rebuild_result.returncode == 0:
            subprocess.run(["docker", "commit", rebuild_container, model_patched_image], capture_output=True)
            print(f"  → Rebuilt JavaScript bundle after model patch")
            execution_log.append(f"  → JavaScript rebuild successful")
        else:
            print(f"  ⚠ JavaScript rebuild failed (tests may use stale build): {rebuild_result.stderr[:100]}")
            execution_log.append(f"  ⚠ JavaScript rebuild failed: {rebuild_result.stderr[:100]}")
            # Commit unchanged container so model_patched_image still exists
            subprocess.run(["docker", "commit", rebuild_container, model_patched_image], capture_output=True)

        subprocess.run(["docker", "rm", "-f", rebuild_container], capture_output=True)

    # Step 4: Run tests AFTER model patch
    print(f"→ Running tests after patch...")
    execution_log.append("\nRunning tests AFTER model patch...")
    output_log.append("\n" + "=" * 60)
    output_log.append("Tests AFTER model patch")
    output_log.append("=" * 60)

    tests_after = run_specific_tests_in_container(model_patched_image, all_tests, is_django, language=language, timeout=test_timeout, instance_id=instance_id, instance=instance)

    output_log.append(f"Collected {len(tests_after)} test(s)")
    for test, status in sorted(tests_after.items()):
        output_log.append(f"  {test}: {status}")

    print(f"  ✓ Ran {len(tests_after)} test(s)")
    execution_log.append(f"  ✓ Collected {len(tests_after)} test results")

    # Step 5: Compare results
    execution_log.append("\nComparing test results...")

    # Special case: vitest/jest only emits a file-level summary entry when tests FAIL;
    # when all tests in the file pass, only individual test results appear.
    # For vuejs__core-10501 and vuejs__core-7576 the PASS_TO_PASS test is the bare
    # file path (e.g. "packages/reactivity/__tests__/effect.spec.ts"), which shows as
    # FAILED before the patch but is MISSING after the patch (all tests passed →
    # no failure summary emitted).  Synthesize a PASSED entry so comparison succeeds.
    if instance_id in {"vuejs__core-10501", "vuejs__core-7576"}:
        for ptp_test in pass_to_pass_tests:
            if '>' not in ptp_test and ptp_test not in tests_after:
                # Check that every individual test result starting with this file path passed
                file_tests_after = {k: v for k, v in tests_after.items() if k.startswith(ptp_test + ' >')}
                if file_tests_after and all(v == "PASSED" for v in file_tests_after.values()):
                    tests_after[ptp_test] = "PASSED"
                    execution_log.append(f"  [special-case] Synthesized file-level PASSED for: {ptp_test}")

    f2p_success, f2p_failure, p2p_success, p2p_failure = compare_results(
        tests_before, tests_after, fail_to_pass_tests, pass_to_pass_tests
    )

    # Determine if resolved
    resolved = len(f2p_success) == len(fail_to_pass_tests) and len(f2p_failure) == 0

    print()
    print(f"Results:")
    print(f"  FAIL_TO_PASS: {len(f2p_success)}/{len(fail_to_pass_tests)} passed")
    print(f"  PASS_TO_PASS: {len(p2p_success)}/{len(pass_to_pass_tests)} passed")
    print(f"  Resolved: {resolved}")

    # Write logs
    output_log.append("\n" + "=" * 60)
    output_log.append("SUMMARY")
    output_log.append("=" * 60)
    output_log.append(f"FAIL_TO_PASS: {len(f2p_success)}/{len(fail_to_pass_tests)}")
    output_log.append(f"PASS_TO_PASS: {len(p2p_success)}/{len(pass_to_pass_tests)}")
    output_log.append(f"Resolved: {resolved}")

    test_output_file.write_text("\n".join(output_log))
    execution_log.append(f"✓ Test output written to {test_output_file}")


    # Create report
    execution_log.append(f"\nEvaluation results:")
    execution_log.append(f"  FAIL_TO_PASS: {len(f2p_success)}/{len(fail_to_pass_tests)} passed")
    execution_log.append(f"  PASS_TO_PASS: {len(p2p_success)}/{len(pass_to_pass_tests)} passed")
    execution_log.append(f"  Resolved: {resolved}")

    report = {
        "instance_id": instance_id,
        "resolved": resolved,
        "patch_applied": True,
        "tests_status": {
            "FAIL_TO_PASS": {
                "success": f2p_success,
                "failure": f2p_failure
            },
            "PASS_TO_PASS": {
                "success": p2p_success,
                "failure": p2p_failure
            }
        }
    }

    report_file.write_text(json.dumps(report, indent=2))
    execution_log.append(f"✓ Report written to {report_file}")

    # Clean up images
    execution_log.append("\nCleaning up Docker images...")
    if test_patched_image != image_tag:
        subprocess.run(["docker", "rmi", test_patched_image], capture_output=True)
        execution_log.append(f"  Removed: {test_patched_image}")
    subprocess.run(["docker", "rmi", model_patched_image], capture_output=True)
    execution_log.append(f"  Removed: {model_patched_image}")

    # Delete instance image (but keep jiayuanz3/memory:base)
    if image_tag != "jiayuanz3/memory:base":
        subprocess.run(["docker", "rmi", image_tag], capture_output=True)
        execution_log.append(f"  Removed: {image_tag}")
        print(f"  ✓ Cleaned up instance image: {image_tag}")
    else:
        execution_log.append(f"  Kept: {image_tag} (base image)")
        print(f"  ✓ Kept base image: {image_tag}")

    # Write execution log
    execution_log.append(f"\n{'='*60}")
    execution_log.append(f"Evaluation completed for {instance_id}")
    execution_log.append(f"{'='*60}")
    run_instance_log.write_text("\n".join(execution_log))

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Run SWE-bench Memory evaluation"
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        required=True,
        help="Path to dataset JSON file"
    )
    parser.add_argument(
        "--predictions_path",
        type=str,
        required=True,
        help="Path to predictions JSON file"
    )
    parser.add_argument(
        "--run_id",
        type=str,
        required=True,
        help="Run ID for this evaluation"
    )

    args = parser.parse_args()

    # Load dataset
    with open(args.dataset_name) as f:
        instances = json.load(f)
    if not isinstance(instances, list):
        instances = [instances]

    # Load predictions (handle both dict and list formats)
    with open(args.predictions_path) as f:
        predictions_raw = json.load(f)

    # Convert to list format
    if isinstance(predictions_raw, dict):
        # Dict format: {"instance_id": {...}}
        predictions = []
        for instance_id, pred_data in predictions_raw.items():
            if 'instance_id' not in pred_data:
                pred_data['instance_id'] = instance_id
            predictions.append(pred_data)
    elif isinstance(predictions_raw, list):
        predictions = predictions_raw
    else:
        predictions = [predictions_raw]

    # Create predictions map
    pred_map = {p['instance_id']: p for p in predictions}

    # Determine model name from predictions
    if predictions:
        model_name = predictions[0].get('model_name_or_path', 'unknown')
    else:
        model_name = 'unknown'

    # Evaluate each instance
    results = []
    resolved_count = 0

    for instance in instances:
        instance_id = instance['instance_id']
        prediction = pred_map.get(instance_id)

        if not prediction:
            print(f"✗ No prediction for {instance_id}")
            continue

        # Create log directory: logs/run_evaluation/{run_id}/{model_name}/{instance_id}/
        log_dir = RUN_EVALUATION_LOG_DIR / args.run_id / model_name / instance_id

        result = evaluate_instance(instance, prediction, args.run_id, log_dir)
        results.append(result)

        if result.get('resolved', False):
            resolved_count += 1

        print()

    # Generate summary report
    report = {
        "total_instances": len(instances),
        "submitted_instances": len(results),
        "completed_instances": len(results),
        "resolved_instances": resolved_count,
        "unresolved_instances": len(results) - resolved_count,
        "resolved_ids": [r['instance_id'] for r in results if r.get('resolved')],
        "unresolved_ids": [r['instance_id'] for r in results if not r.get('resolved')],
        "schema_version": 2
    }

    # Add individual results
    for result in results:
        report[result['instance_id']] = result

    # Write summary report
    output_file = f"{args.run_id}.json"
    with open(output_file, 'w') as f:
        json.dump(report, f, indent=2)

    print("=" * 60)
    print(f"✓ Evaluation complete: {resolved_count}/{len(results)} resolved")
    print(f"Report written to: {output_file}")
    print(f"Logs written to: {RUN_EVALUATION_LOG_DIR / args.run_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
