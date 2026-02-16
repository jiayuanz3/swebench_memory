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
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


# Log directory structure (same as swebench)
RUN_EVALUATION_LOG_DIR = Path("logs/run_evaluation")


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


def run_specific_tests_in_container(
    image_tag: str,
    tests: List[str],
    is_django: bool = False,
    timeout: int = 600,
    instance_id: str = ""
) -> Dict[str, str]:
    """
    Run specific tests inside Docker container

    Args:
        image_tag: Docker image to run
        tests: List of specific test names (e.g., "path/to/test.py::test_name")
        is_django: Whether this is a Django repo
        timeout: Timeout per test in seconds
        instance_id: Instance ID (e.g., "psf__requests-2344") for repo-specific handling

    Returns:
        Dict mapping test names to status (PASSED, FAILED, ERROR, SKIPPED)
    """
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

                # Run Django test
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

                    # Parse pytest output
                    full_output = result.stdout + "\n" + result.stderr

                    # Look for test status in output
                    # Match: test_name PASSED/FAILED/ERROR/SKIPPED
                    match = re.search(r'(\S+?)::((?:\w+::)?test_\w+(?:\[.*?\])?):?\s+(PASSED|FAILED|ERROR|SKIPPED)', full_output)
                    if match:
                        status = match.group(3)
                        status_map[test_name] = status
                    else:
                        # If no match found, assume failure
                        status_map[test_name] = "FAILED"

                except subprocess.TimeoutExpired:
                    status_map[test_name] = "TIMEOUT"

            return status_map

        for test_file, test_list in test_by_file.items():
            # Run entire test file (like full_validation.py)
            # This is necessary because:
            # 1. Nose-style test classes ERROR when selected explicitly
            # 2. Instance file may omit class names (file::test vs file::Class::test)
            # 3. We only REPORT on tests from instance, but run whole file to collect results

            # Check if this is a meson-python project
            check_meson = subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c",
                 "grep -q 'meson-python' /testbed/pyproject.toml 2>/dev/null && echo 'yes' || echo 'no'"],
                capture_output=True,
                text=True
            )
            is_meson = 'yes' in check_meson.stdout

            if is_meson:
                # For meson-python: Copy tests to /tmp to avoid importing incomplete source
                # This ensures matplotlib is imported from site-packages (with C extensions)
                # instead of /testbed/lib (incomplete source)

                # Determine which test directory to copy based on test file path
                if test_file.startswith('lib/mpl_toolkits/'):
                    # Extract toolkit name and copy entire mpl_toolkits tree
                    # e.g., lib/mpl_toolkits/mplot3d/tests/test_axes3d.py
                    test_relpath = test_file.replace('lib/mpl_toolkits/', '')
                    cmd = (
                        f"cd /testbed && "
                        f"cp -r lib/mpl_toolkits /tmp/mpl_toolkits && "
                        f"cd /tmp && "
                        f"python -m pytest mpl_toolkits/{test_relpath} -v --tb=short --no-header --color=no 2>&1 || "
                        f"python -m pytest mpl_toolkits/{test_relpath} -v --tb=short --color=no 2>&1 || true"
                    )
                else:
                    # lib/matplotlib/tests case
                    test_basename = test_file.split('/')[-1]
                    cmd = (
                        f"cd /testbed && "
                        f"cp -r lib/matplotlib/tests /tmp/matplotlib_tests && "
                        f"cp -r lib/matplotlib/testing /tmp/matplotlib_testing 2>/dev/null || true && "
                        f"cd /tmp && "
                        f"python -m pytest matplotlib_tests/{test_basename} -v --tb=short --no-header --color=no 2>&1 || "
                        f"python -m pytest matplotlib_tests/{test_basename} -v --tb=short --color=no 2>&1 || true"
                    )
            else:
                # Standard approach: run from /testbed
                cmd = (
                    f"cd /testbed && "
                    f"python -m pytest {test_file} -v --tb=short --no-header --color=no 2>&1 || "
                    f"python -m pytest {test_file} -v --tb=short --color=no 2>&1 || true"
                )

            try:
                result = subprocess.run(
                    ["docker", "run", "--rm", image_tag, "bash", "-c", cmd],
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )

                # Parse pytest output
                full_output = result.stdout + "\n" + result.stderr

                # Collect ALL test results from the file
                all_results = {}
                for line in full_output.split('\n'):
                    # Match: file.py::test or file.py::Class::test PASSED/FAILED/ERROR/SKIPPED
                    # Note: pytest may output either "test_name PASSED" or "test_name: PASSED"
                    match = re.search(r'(\S+?)::((?:\w+::)?test_\w+(?:\[.*?\])?):?\s+(PASSED|FAILED|ERROR|SKIPPED)', line)
                    if match:
                        file_part = match.group(1)
                        test_part = match.group(2)
                        status = match.group(3)

                        # Normalize file path
                        if file_part.startswith('/testbed/'):
                            file_part = file_part[9:]

                        # For meson-python: convert copied test paths back to original
                        if is_meson:
                            if file_part.startswith('matplotlib_tests/'):
                                file_part = 'lib/matplotlib/tests/' + file_part.replace('matplotlib_tests/', '')
                            elif file_part.startswith('mpl_toolkits/'):
                                file_part = 'lib/' + file_part

                        full_path = f"{file_part}::{test_part}"
                        all_results[full_path] = status

                # Match requested tests to actual results
                # Only report on tests that are in the instance file (test_list)
                for requested_test in test_list:
                    # Try exact match first
                    if requested_test in all_results:
                        status_map[requested_test] = all_results[requested_test]
                        continue

                    # Handle class-based tests where instance omits or has wrong class name
                    # e.g., requested="file::test", actual="file::TestClass::test"
                    # e.g., requested="file::OldClass::test", actual="file::NewClass::test"
                    parts = requested_test.split('::')
                    if len(parts) >= 2:  # file::test or file::class::test
                        file_path = parts[0]
                        test_name = parts[-1]  # Last part is always test name
                        # Find ALL matching tests (there might be multiple in different classes)
                        matches = []
                        for full_path, status in all_results.items():
                            if full_path.startswith(file_path + '::') and full_path.endswith('::' + test_name):
                                matches.append(status)

                        if matches:
                            # If multiple matches, aggregate: all must pass
                            if any(s in ['FAILED', 'ERROR'] for s in matches):
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
    2. jiayuanz3/memory:<instance_id with _ and __ -> .> (user's custom format)
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

    # Pattern 2: jiayuanz3/memory format (replace __ with .)
    # Convert: astropy__astropy-4973 -> astropy.astropy-4973
    memory_tag = f"jiayuanz3/memory:{instance_id.replace('__', '.')}"
    result = subprocess.run(
        ["docker", "images", "-q", memory_tag],
        capture_output=True,
        text=True
    )
    if result.stdout.strip():
        return memory_tag

    # Pattern 3: Search all images for matching tag
    # Get all docker images and search for instance_id pattern
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        instance_pattern = instance_id.replace('__', '.')
        for line in result.stdout.strip().split('\n'):
            if instance_pattern in line:
                return line.strip()

    # Pattern 4: Auto-pull from jiayuanz3/memory if not found locally
    if auto_pull:
        print(f"  → Image not found locally, pulling from Docker Hub...")
        pull_result = subprocess.run(
            ["docker", "pull", memory_tag],
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
        alt_tag = f"jiayuanz3/memory:{instance_id.replace('__', '.')}"

        print(f"✗ Image not found for instance: {instance_id}")
        print(f"  Expected one of:")
        print(f"    - {expected_tag}")
        print(f"    - {alt_tag}")
        print(f"    - Any image with tag containing '{instance_id.replace('__', '.')}'")

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

    # Write model patch to patch.diff (with Python 3 sanitization)
    model_patch = prediction.get('model_patch', prediction.get('patch', ''))
    original_patch = model_patch
    model_patch = _sanitize_patch_for_python3(model_patch or '')

    patch_file.write_text(model_patch or '')
    execution_log.append(f"Model patch written to {patch_file}")

    # Log if sanitization was applied
    if original_patch != model_patch and model_patch:
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

    # Detect if Django repo
    is_django = 'django' in instance['repo'].lower()

    # Get infrastructure fixes (if needed)
    from .build_instance import RepoConfig, get_commit_date
    repo = instance['repo']

    # SymPy instances need longer timeout due to slow symbolic computation
    # (especially for test_wester.py which has 236 tests with symbolic sets)
    is_sympy = 'sympy' in repo.lower()
    test_timeout = 1800 if is_sympy else 600  # 30 min for sympy, 10 min for others
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

    # Create eval.sh script (for reference, matching original swebench)
    eval_script = f"""#!/bin/bash
set -euxo pipefail

cd /testbed

# Apply test patch if exists
if [ -f /test.patch ]; then
    git apply /test.patch || echo "Test patch already applied or failed"
fi

# Apply model patch
git apply /patch.diff

# Run tests
{"python tests/runtests.py" if is_django else "python -m pytest"} -v
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

        cmd_apply_test = f"cd /testbed && cat <<'PATCH_EOF' | git apply -\n{test_patch}\nPATCH_EOF"
        container_name = f"apply_testpatch_{instance_id.replace('/', '_').replace('__', '_')}"

        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        result = subprocess.run(
            ["docker", "run", "--name", container_name, image_tag, "bash", "-c", cmd_apply_test],
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

        if result.returncode != 0:
            print(f"  ⚠ Test patch failed (may already be in repo)")
            execution_log.append(f"  ⚠ Test patch apply returned code {result.returncode}")
        else:
            print(f"  ✓ Test patch applied")
            execution_log.append(f"  ✓ Test patch applied successfully")

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
            # The test_patch includes code changes that need to be part of the installed package
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
    else:
        # No test patch, use original image
        test_patched_image = image_tag
        execution_log.append("No test patch to apply")

    # Step 1.5: Apply infrastructure fixes (if needed)
    if infra_fixes:
        print(f"→ Applying {len(infra_fixes)} infrastructure fix(es)...")
        execution_log.append(f"\nApplying {len(infra_fixes)} infrastructure fixes...")

        for description, patch_content in infra_fixes:
            print(f"  → {description}")
            execution_log.append(f"  → {description}")

            cmd_apply_infra = f"cd /testbed && cat <<'PATCH_EOF' | git apply -\n{patch_content}\nPATCH_EOF"
            container_name = f"apply_infra_{instance_id.replace('/', '_').replace('__', '_')}"

            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

            result = subprocess.run(
                ["docker", "run", "--name", container_name, test_patched_image, "bash", "-c", cmd_apply_infra],
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

    tests_before = run_specific_tests_in_container(test_patched_image, all_tests, is_django, timeout=test_timeout, instance_id=instance_id)

    output_log.append(f"Collected {len(tests_before)} test(s)")
    for test, status in sorted(tests_before.items()):
        output_log.append(f"  {test}: {status}")

    print(f"  ✓ Ran {len(tests_before)} test(s)")
    execution_log.append(f"  ✓ Collected {len(tests_before)} test results")

    # Step 3: Apply model patch
    print(f"→ Applying model patch...")
    execution_log.append("\nApplying model patch...")
    output_log.append("\n" + "=" * 60)
    output_log.append("Applying model patch")
    output_log.append("=" * 60)

    cmd_apply_model = f"cd /testbed && cat <<'PATCH_EOF' | git apply -\n{model_patch}\nPATCH_EOF"
    # Create unique name for model-patched image (handle tags with or without :latest)
    if ":latest" in image_tag:
        model_patched_image = image_tag.replace(":latest", f":{run_id}_patched")
    else:
        # For tags like jiayuanz3/memory:astropy.astropy-4973
        model_patched_image = f"{image_tag}_{run_id}_patched"
    container_name = f"apply_patch_{instance_id.replace('/', '_').replace('__', '_')}"

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    result = subprocess.run(
        ["docker", "run", "--name", container_name, test_patched_image, "bash", "-c", cmd_apply_model],
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

    # For meson-python: Reinstall after model_patch to incorporate code changes
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

    # Step 4: Run tests AFTER model patch
    print(f"→ Running tests after patch...")
    execution_log.append("\nRunning tests AFTER model patch...")
    output_log.append("\n" + "=" * 60)
    output_log.append("Tests AFTER model patch")
    output_log.append("=" * 60)

    tests_after = run_specific_tests_in_container(model_patched_image, all_tests, is_django, timeout=test_timeout, instance_id=instance_id)

    output_log.append(f"Collected {len(tests_after)} test(s)")
    for test, status in sorted(tests_after.items()):
        output_log.append(f"  {test}: {status}")

    print(f"  ✓ Ran {len(tests_after)} test(s)")
    execution_log.append(f"  ✓ Collected {len(tests_after)} test results")

    # Step 5: Compare results
    execution_log.append("\nComparing test results...")
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
