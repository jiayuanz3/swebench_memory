#!/usr/bin/env python3
"""
Build instance Docker images following full_validation.py workflow

This script:
1. Detects Python version from repo
2. Detects dependencies based on repo type
3. Builds a Docker image following full_validation.py steps exactly

Platform: Ubuntu 22.04 + gcc 11 (Linux)
Note: Adapted from full_validation.py which runs on macOS + clang

Usage:
    python -m swebench_memory.harness.build_instance --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple


BASE_IMAGE_TAG = "sweb.simple.base:latest"


class RepoConfig:
    """
    Repository-specific configuration
    Copied from full_validation.py to ensure identical behavior
    """

    def __init__(self, repo: str):
        self.repo = repo.lower()
        self.repo_name = repo.split('/')[-1]

    def get_build_deps(self) -> List[str]:
        """Get build-time dependencies (fallback if not found in pyproject.toml/setup.py)"""
        if 'scikit-learn' in self.repo or 'sklearn' in self.repo:
            return ['numpy<2', 'scipy==1.3.3', 'cython<0.30']
        elif 'astropy' in self.repo:
            return ['numpy<2', 'cython<0.30', 'setuptools-scm', 'extension-helpers']
        else:
            return ['wheel']

    def get_runtime_deps(self, commit_date: str = "2020-01-01") -> List[str]:
        """Get runtime dependencies"""
        if 'sympy' in self.repo:
            # SymPy requires hypothesis for test suite (even for non-hypothesis tests)
            # conftest.py raises ImportError if hypothesis is not installed
            # Use date-based versioning for compatibility
            deps = []
            try:
                year = int(commit_date.split('-')[0])

                if year < 2016:
                    hypothesis_constraint = "hypothesis>=1.0,<3.0"
                    # Python 2.7 era, no importlib_metadata needed
                elif year == 2016:
                    hypothesis_constraint = "hypothesis>=3.0,<3.45"
                    # Python 2.7/3.5 era, no importlib_metadata needed
                elif year == 2017:
                    hypothesis_constraint = "hypothesis>=3.0,<3.82"
                    # Python 3.6 era, no importlib_metadata needed
                elif year == 2018:
                    hypothesis_constraint = "hypothesis>=3.70,<4.0"
                    # Python 3.7, may need importlib_metadata constraint
                    deps.append("importlib_metadata<5.0")
                elif year == 2019:
                    hypothesis_constraint = "hypothesis>=4.0,<5.0"
                    # Python 3.7, needs importlib_metadata constraint
                    deps.append("importlib_metadata<5.0")
                elif year == 2020:
                    hypothesis_constraint = "hypothesis>=5.0,<6.0"
                    # Python 3.7, needs importlib_metadata constraint for hypothesis 5.x
                    deps.append("importlib_metadata<5.0")
                elif year >= 2021:
                    hypothesis_constraint = "hypothesis>=6.0"
                    # Python 3.8+, no importlib_metadata constraint needed (stdlib)
                else:
                    hypothesis_constraint = "hypothesis>=3.0,<4.0"
                    deps.append("importlib_metadata<5.0")
            except (ValueError, IndexError):
                hypothesis_constraint = "hypothesis>=3.0,<4.0"
                deps.append("importlib_metadata<5.0")

            deps.insert(0, hypothesis_constraint)
            return deps
        elif 'scikit-learn' in self.repo:
            return ['numpy<2', 'scipy', 'joblib', 'threadpoolctl', 'pandas']
        elif 'matplotlib' in self.repo:
            # For matplotlib using deprecated pyparsing API, pin pyparsing to avoid errors
            # pyparsing 3.1+ deprecated enablePackrat() and parseString()
            # matplotlib stopped using these deprecated APIs around late 2024
            try:
                # Parse date as YYYY-MM-DD
                year = int(commit_date.split('-')[0])
                month = int(commit_date.split('-')[1]) if len(commit_date.split('-')) > 1 else 1

                # Pin pyparsing for matplotlib before October 2024
                if year < 2024 or (year == 2024 and month < 10):
                    pyparsing_dep = 'pyparsing>=2.3.1,<3.1'
                else:
                    pyparsing_dep = 'pyparsing'
            except (ValueError, IndexError):
                pyparsing_dep = 'pyparsing>=2.3.1,<3.1'

            return ['numpy<2', 'pillow', pyparsing_dep, 'python-dateutil', 'cycler', 'pandas']
        elif 'seaborn' in self.repo:
            return ['numpy<2', 'pandas', 'matplotlib']
        elif 'astropy' in self.repo:
            return ['numpy<2', 'scipy', 'pytest-astropy', 'pytest-doctestplus']
        elif 'django' in self.repo:
            # tzdata is required for Django 3.2+ with zoneinfo/backports.zoneinfo
            return ['pytz', 'sqlparse', 'asgiref', 'tzdata']
        elif 'flask' in self.repo:
            return ['werkzeug<2.1', 'jinja2', 'click', 'itsdangerous']
        elif 'requests' in self.repo:
            return ['urllib3', 'chardet', 'certifi', 'idna']
        elif 'pylint' in self.repo:
            # pylint requires specific astroid version for compatibility
            # astroid provides the doc_node attribute needed by docstring_checker.py
            # py package is needed for tests/test_self.py which imports from py._path.local
            # From setup.cfg at base_commit 273a8b2 (2022-05-06):
            return [
                'astroid>=2.11.5,<=2.13.0',  # Slightly wider than <=2.12.0-dev0 for availability
                'platformdirs>=2.2.0',
                'dill>=0.2',
                'py>=1.8.0'  # Required for test_self.py imports
            ]
        return []

    def needs_no_build_isolation(self) -> bool:
        """Check if repo needs --no-build-isolation flag (copied from full_validation.py)"""
        return 'scikit-learn' in self.repo or 'astropy' in self.repo

    def get_env_vars(self) -> dict:
        """
        Get environment variables for building (adapted from full_validation.py lines 94-114)

        Differences from full_validation.py:
        - full_validation.py runs on macOS with clang → uses Darwin-specific flags
        - swebench_memory runs in Docker with Ubuntu 22.04 + gcc 11 → uses Linux-compatible flags

        Key changes:
        - Skipped: SKLEARN_NO_OPENMP, CC=clang, CXX=clang++ (macOS-only)
        - Skipped: -std=gnu89 -Wno-implicit-function-declaration (for macOS clang)
        - Changed: -Wincompatible-function-pointer-types → -Wincompatible-pointer-types
          Reason: gcc 11 uses different flag name than clang
        """
        env = {}

        # For old C code compatibility on Linux gcc 11 (Ubuntu 22.04)
        # This flag allows compilation of legacy C extensions with pointer type issues
        if 'scikit-learn' in self.repo or 'astropy' in self.repo:
            env['CFLAGS'] = '-Wno-error=incompatible-pointer-types'

        return env

    def get_infrastructure_fixes(self, commit_date: str) -> List[Tuple[str, str]]:
        """
        Get infrastructure fix patches to apply before running tests.
        Copied from full_validation.py lines 120-183

        Returns list of (description, patch_content) tuples for repo-specific
        fixes needed at certain commit dates to make tests runnable.

        These are NOT solution patches - they fix broken test infrastructure
        in old commits (e.g., pytest compatibility issues).
        """
        fixes = []

        # SymPy: pytest compatibility fix for pytest 7+
        # Fix py.test.mark.* references which were removed in pytest 7
        if 'sympy/sympy' in self.repo:
            # For older SymPy (before 2017-01-01): simpler fix without _pytest imports
            if commit_date < '2017-01-01':
                pytest_fix_old = """diff --git a/sympy/utilities/pytest.py b/sympy/utilities/pytest.py
--- a/sympy/utilities/pytest.py
+++ b/sympy/utilities/pytest.py
@@ -10,6 +10,7 @@ from sympy.core.compatibility import get_function_name

 try:
     import py
+    import pytest
     from py.test import skip
     USE_PYTEST = getattr(sys, '_running_pytest', False)
 except ImportError:
@@ -149,8 +150,9 @@ def func_wrapper():
         return func_wrapper

 else:
-    XFAIL = py.test.mark.xfail
-    slow = py.test.mark.slow
+    XFAIL = pytest.mark.xfail
+    slow = pytest.mark.slow
+    raises = pytest.raises

     def SKIP(reason):
         def skipping(func):
"""
                fixes.append(("SymPy pytest compatibility (fix py.test.mark for pytest 7+)", pytest_fix_old))
            else:
                # For newer SymPy (2017+): fix with _pytest imports
                pytest_fix = """diff --git a/sympy/utilities/pytest.py b/sympy/utilities/pytest.py
--- a/sympy/utilities/pytest.py
+++ b/sympy/utilities/pytest.py
@@ -10,6 +10,7 @@ from sympy.utilities.exceptions import SymPyDeprecationWarning

 try:
     import py
+    import pytest
     from _pytest.python_api import raises
     from _pytest.recwarn import warns
     from _pytest.outcomes import skip, Failed
@@ -196,8 +197,8 @@ def raises(expectedException, code=None):
             raise Failed(msg)


 else:
-    XFAIL = py.test.mark.xfail
-    slow = py.test.mark.slow
+    XFAIL = pytest.mark.xfail
+    slow = pytest.mark.slow

     def SKIP(reason):
         def skipping(func):
"""
                fixes.append(("SymPy pytest compatibility (fix py.test.mark for pytest 7+)", pytest_fix))

        # Django: HTMLParseError compatibility fix (needed before Django 1.9, ~2015-12-01)
        if 'django/django' in self.repo and commit_date < '2015-12-01':
            htmlparser_fix = """diff --git a/django/utils/html_parser.py b/django/utils/html_parser.py
--- a/django/utils/html_parser.py
+++ b/django/utils/html_parser.py
@@ -9,7 +9,12 @@
     (current_version >= (3, 0) and current_version < (3, 2, 3))
 )

-HTMLParseError = _html_parser.HTMLParseError
+try:
+    HTMLParseError = _html_parser.HTMLParseError
+except AttributeError:
+    # HTMLParseError was removed in Python 3.5
+    class HTMLParseError(Exception):
+        pass

 if not use_workaround:
     if current_version >= (3, 4):
"""
            fixes.append(("Django HTMLParseError compatibility (Python 3.5+)", htmlparser_fix))

        return fixes

    def uses_django_runner(self) -> bool:
        """Check if repo uses Django test runner (copied from full_validation.py)"""
        return 'django/django' in self.repo


def get_commit_date(repo_dir: Path, commit: str) -> str:
    """Get commit date in YYYY-MM-DD format from git"""
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%ci', commit],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True
        )
        date_str = result.stdout.strip().split()[0]  # Extract YYYY-MM-DD part
        return date_str
    except Exception:
        return "2021-01-01"  # Fallback


def get_historical_setuptools(commit_date: str, build_reqs: List[str]) -> str:
    """
    Map commit date to appropriate setuptools version.
    Copied from full_validation.py lines 416-476

    Special handling for Cython: Old Cython (<0.30) requires setuptools.dep_util,
    which was removed in setuptools 50. So if Cython<0.30 is present, force setuptools<50.

    Special handling for setuptools_scm: setuptools_scm>=7 requires setuptools>=60.
    """
    # Check if old Cython is in dependencies - it requires setuptools.dep_util
    for req in build_reqs:
        if req.lower().startswith('cython'):
            # Old Cython versions need setuptools<50
            if 'cython==0.29' in req.lower() or 'cython<0.30' in req.lower():
                return "setuptools<50"
            # Parse version if present
            match = re.search(r'cython[=<>!]+(\d+)\.(\d+)', req.lower())
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                if major == 0 and minor < 30:
                    return "setuptools<50"

    # Check if setuptools_scm>=7 is in dependencies - it requires setuptools>=64
    for req in build_reqs:
        if 'setuptools_scm' in req.lower() or 'setuptools-scm' in req.lower():
            # Parse version if present
            match = re.search(r'setuptools[-_]scm>=(\d+)', req.lower())
            if match:
                version = int(match.group(1))
                if version >= 7:
                    # setuptools_scm 7+ works best with setuptools 64+
                    return "setuptools>=64,<70"

    try:
        year = int(commit_date.split('-')[0])
        month = int(commit_date.split('-')[1])

        # Map year to setuptools version
        if year >= 2024 or (year == 2023 and month >= 6):
            return "setuptools<70"
        elif year >= 2022 or (year == 2021 and month >= 6):
            return "setuptools<60"
        elif year >= 2021:
            return "setuptools<58"
        elif year == 2020 and month >= 8:
            return "setuptools<50"
        elif year >= 2019:
            return "setuptools<50"
        elif year >= 2018:
            return "setuptools<45"
        else:
            return "setuptools<40"
    except (ValueError, IndexError):
        return "setuptools<58"


def get_build_requirements(repo_dir: Path, repo: str) -> List[str]:
    """
    Extract build requirements from base_commit's pyproject.toml or setup.py
    Copied from full_validation.py lines 478-557
    """
    build_reqs = []
    has_setuptools_constraint = False

    # Check pyproject.toml first
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            # Look for [build-system] requires = [...]
            match = re.search(r'\[build-system\].*?requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
            if match:
                reqs_text = match.group(1)
                # Match complete quoted strings: "..." or '...'
                for req_match in re.finditer(r'"([^"]*)"|\'([^\']*)\'', reqs_text):
                    req = req_match.group(1) if req_match.group(1) is not None else req_match.group(2)
                    # Strip PEP 508 environment markers
                    req = req.split(';')[0].strip()
                    if not req:
                        continue
                    # Pin numpy<2 for compatibility
                    if req.lower().startswith('numpy') or 'oldest-supported-numpy' in req.lower():
                        if not any(op in req for op in ['==', '>=', '<=', '>', '<', '~=']):
                            req = 'numpy<2'
                    # Check if setuptools has version constraint
                    req_lower = req.lower()
                    if req_lower == 'setuptools' or req_lower.startswith(('setuptools==', 'setuptools>=', 'setuptools<=', 'setuptools<', 'setuptools>', 'setuptools~=', 'setuptools!=')):
                        if any(op in req for op in ['==', '>=', '<=', '>', '<', '~=', '!=']):
                            has_setuptools_constraint = True
                        else:
                            # setuptools without version - skip, we'll add historical
                            continue
                    build_reqs.append(req)
        except Exception:
            pass

    # Check setup.py for setup_requires if not found
    if not build_reqs:
        setup_py = repo_dir / "setup.py"
        if setup_py.exists():
            try:
                content = setup_py.read_text()
                match = re.search(r'setup_requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
                if match:
                    reqs_text = match.group(1)
                    for req_match in re.finditer(r'"([^"]*)"|\'([^\']*)\'', reqs_text):
                        req = req_match.group(1) if req_match.group(1) is not None else req_match.group(2)
                        req = req.split(';')[0].strip()
                        if not req:
                            continue
                        req_lower = req.lower()
                        if req_lower == 'setuptools' or req_lower.startswith(('setuptools==', 'setuptools>=', 'setuptools<=', 'setuptools<', 'setuptools>', 'setuptools~=', 'setuptools!=')):
                            if any(op in req for op in ['==', '>=', '<=', '>', '<', '~=', '!=']):
                                has_setuptools_constraint = True
                            else:
                                continue
                        build_reqs.append(req)
            except Exception:
                pass

    # Fallback to repo-specific defaults if nothing found
    if not build_reqs:
        config = RepoConfig(repo)
        build_reqs = config.get_build_deps()

    return build_reqs, has_setuptools_constraint


def detect_python_version(repo_dir: Path, created_at: str) -> str:
    """
    Detect Python version from repo files
    Logic copied from full_validation.py lines 276-334
    """
    MIN_VERSION = (3, 6)

    # Check multiple sources in priority order
    sources = [
        (repo_dir / ".python-version", r'(\d+\.\d+)'),
        (repo_dir / "pyproject.toml", r'requires-python\s*=\s*["\']>=?\s*(\d+\.\d+)'),
        (repo_dir / "setup.py", r'python_requires\s*=\s*["\']>=?(\d+\.\d+)'),
        (repo_dir / "setup.cfg", r'python_requires\s*=\s*>=?(\d+\.\d+)'),
    ]

    for filepath, pattern in sources:
        if not filepath.exists():
            continue

        content = filepath.read_text()
        match = re.search(pattern, content)

        if match:
            version = match.group(1)

            # Parse and validate
            try:
                parts = version.split('.')
                major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

                if (major, minor) >= MIN_VERSION:
                    return f"{major}.{minor}"
            except (ValueError, IndexError):
                continue

    # Special handling for matplotlib with dynamic python_requires
    # Check setup.py for py_min_version variable
    setup_py = repo_dir / "setup.py"
    if setup_py.exists():
        content = setup_py.read_text()
        # Match: py_min_version = (3, 9) or similar
        match = re.search(r'py_min_version\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)', content)
        if match:
            major, minor = int(match.group(1)), int(match.group(2))
            if (major, minor) >= MIN_VERSION:
                return f"{major}.{minor}"

    # Fallback: use date-based detection (like full_validation.py line 239)
    try:
        year = int(created_at.split('-')[0])
        month = int(created_at.split('-')[1])

        if year < 2017:
            return "3.6"
        elif year == 2017 or (year == 2018 and month < 6):
            return "3.6"
        elif year == 2018 or (year == 2019 and month < 10):
            return "3.7"
        elif year == 2019 or year == 2020:
            return "3.7"
        elif year == 2021:
            return "3.8"
        elif year == 2022:
            return "3.9"
        elif year == 2023:
            return "3.10"
        else:
            return "3.11"
    except (ValueError, IndexError):
        return "3.7"


def build_instance_image(
    instance: dict,
    force_rebuild: bool = False
) -> Tuple[bool, str]:
    """
    Build a Docker image for a specific instance

    Args:
        instance: Instance data from dataset JSON
        force_rebuild: If True, rebuild even if image exists

    Returns:
        (success, image_tag)
    """
    instance_id = instance['instance_id']
    repo = instance['repo']
    base_commit = instance['base_commit']
    created_at = instance.get('created_at', '2020-01-01')

    # Generate image tag
    image_tag = f"sweb.simple.{instance_id.replace('__', '.')}:latest"

    print("=" * 60)
    print(f"Building instance: {instance_id}")
    print("=" * 60)

    # Check if image already exists
    if not force_rebuild:
        result = subprocess.run(
            ["docker", "images", "-q", image_tag],
            capture_output=True,
            text=True
        )
        if result.stdout.strip():
            print(f"✓ Image already exists: {image_tag}")
            print(f"  Use --force-rebuild to rebuild")
            return True, image_tag

    # Check base image exists
    result = subprocess.run(
        ["docker", "images", "-q", BASE_IMAGE_TAG],
        capture_output=True,
        text=True
    )
    if not result.stdout.strip():
        print(f"✗ Base image not found: {BASE_IMAGE_TAG}")
        print(f"  Run: python -m swebench_memory.harness.build_base")
        return False, ""

    # Clone repo to detect Python version (like full_validation.py)
    print("→ Cloning repo to detect Python version...")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_url = f"https://github.com/{repo}.git"

        # Clone
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
            capture_output=True
        )
        if result.returncode != 0:
            print(f"✗ Failed to clone repo: {repo}")
            return False, ""

        # Fetch and checkout specific commit
        subprocess.run(
            ["git", "fetch", "--depth=100", "origin", base_commit],
            cwd=repo_dir,
            capture_output=True
        )
        subprocess.run(
            ["git", "checkout", base_commit],
            cwd=repo_dir,
            capture_output=True
        )

        # Detect Python version
        python_version = detect_python_version(repo_dir, created_at)
        print(f"  ✓ Detected Python version: {python_version}")

        # Get build requirements from pyproject.toml/setup.py (like full_validation.py)
        build_deps, has_setuptools = get_build_requirements(repo_dir, repo)

        # Add historical setuptools if no constraint found (like full_validation.py)
        commit_date = get_commit_date(repo_dir, base_commit)
        if not has_setuptools:
            historical_setuptools = get_historical_setuptools(commit_date, build_deps)
            build_deps.insert(0, historical_setuptools)
            print(f"  ✓ Detected commit date: {commit_date}, using {historical_setuptools}")

    # Get repo-specific settings (like full_validation.py)
    repo_config = RepoConfig(repo)
    runtime_deps = repo_config.get_runtime_deps(commit_date)
    env_vars = repo_config.get_env_vars()

    print(f"→ Build deps: {build_deps}")
    print(f"→ Runtime deps: {runtime_deps}")
    if env_vars:
        print(f"→ Environment vars: {env_vars}")

    # Get Dockerfile path
    script_dir = Path(__file__).parent.parent
    dockerfile_path = script_dir / "templates" / "Dockerfile.instance"

    # Build image
    print(f"→ Building Docker image...")
    print(f"  This may take 5-10 minutes...")

    build_args = [
        "--build-arg", f"PYTHON_VERSION={python_version}",
        "--build-arg", f"REPO_URL={repo_url}",
        "--build-arg", f"REPO_NAME={repo.split('/')[-1]}",
        "--build-arg", f"BASE_COMMIT={base_commit}",
        "--build-arg", f"CREATED_AT={created_at}",
        "--build-arg", f"BUILD_DEPS={' '.join(build_deps)}",
        "--build-arg", f"RUNTIME_DEPS={' '.join(runtime_deps)}",
        "--build-arg", f"CFLAGS={env_vars.get('CFLAGS', '')}",
    ]

    result = subprocess.run(
        [
            "docker", "build",
            "--platform", "linux/amd64",
            "-f", str(dockerfile_path),
            "-t", image_tag,
            *build_args,
            str(script_dir / "templates")
        ],
        text=True
    )

    if result.returncode != 0:
        print(f"✗ Failed to build image: {instance_id}")
        return False, ""

    print()
    print("=" * 60)
    print(f"✓ Instance image built: {image_tag}")
    print("=" * 60)

    return True, image_tag


def main():
    parser = argparse.ArgumentParser(
        description="Build SWE-bench Memory instance images"
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        required=True,
        help="Path to dataset JSON file"
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild even if images exist"
    )

    args = parser.parse_args()

    # Load dataset
    dataset_path = Path(args.dataset_name)
    if not dataset_path.exists():
        print(f"✗ Dataset not found: {dataset_path}")
        sys.exit(1)

    with open(dataset_path) as f:
        instances = json.load(f)

    if not isinstance(instances, list):
        instances = [instances]

    print(f"Building {len(instances)} instance(s)...")
    print()

    # Build each instance
    success_count = 0
    for instance in instances:
        success, image_tag = build_instance_image(
            instance,
            force_rebuild=args.force_rebuild
        )
        if success:
            success_count += 1
        print()

    print("=" * 60)
    print(f"✓ Successfully built {success_count}/{len(instances)} images")
    print("=" * 60)

    sys.exit(0 if success_count == len(instances) else 1)


if __name__ == "__main__":
    main()
