#!/usr/bin/env python3
"""
Build instance Docker images for all 8 supported languages.

This script:
1. Detects language from repo marker files (Cargo.toml, go.mod, pom.xml, etc.)
2. Detects the required language version from project files, with date-based fallback
3. Builds a Docker image using the language-specific Dockerfile template

Platform: Ubuntu 22.04 + gcc 11 (Linux/amd64) — all Docker images target linux/amd64.
Note: Python path adapted from full_validation.py (macOS/clang); other languages target Linux directly.

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

    def get_package_name(self) -> str:
        """Get the importable Python package name for this repo"""
        mappings = {
            'scikit-learn': 'sklearn',
            'pytest': 'pytest',
            'requests': 'requests',
            'seaborn': 'seaborn',
            'matplotlib': 'matplotlib',
            'django': 'django',
            'astropy': 'astropy',
            'sympy': 'sympy',
            'xarray': 'xarray',
            'pylint': 'pylint',
        }
        for key, value in mappings.items():
            if key in self.repo:
                return value
        return self.repo_name.replace('-', '_')

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

            # Old matplotlib (pre-2021) uses np.float in polar.py; NumPy >=1.20 raises
            # DeprecationWarning (error via filterwarnings=error). NumPy >=1.19 also adds
            # VisibleDeprecationWarning for ragged-sequence ndarrays in test code.
            # Pin numpy<1.19 for old commits — mirrors full_validation.py lines 966-985.
            if commit_date < "2021-01-01":
                numpy_dep = 'numpy<1.19'
            else:
                numpy_dep = 'numpy<2'

            return [numpy_dep, 'pillow', pyparsing_dep, 'python-dateutil', 'cycler', 'pandas']
        elif 'seaborn' in self.repo:
            return ['numpy<2', 'pandas', 'matplotlib']
        elif 'astropy' in self.repo:
            try:
                year = int(commit_date.split('-')[0])
            except (ValueError, IndexError):
                year = 2020
            # Old astropy (pre-2022) uses nose-style setup(self)/teardown(self) fixtures,
            # which pytest 7+ treats as a hard error (PytestRemovedIn8Warning).
            # Newer astropy requires pytest>=7 (setup.cfg minversion = 7.0).
            if year < 2022:
                pytest_dep = 'pytest<7'
            else:
                pytest_dep = 'pytest'
            return ['numpy<2', 'scipy', 'pytest-astropy', 'pytest-doctestplus', pytest_dep]
        elif 'sphinx' in self.repo:
            # Jinja2 3.0 (released May 2021) removed environmentfilter/contextfilter/evalcontextfilter.
            # Sphinx 4.0 (April 2021) added Jinja2 3.0 support via pass_environment etc.
            # Pin jinja2<3.0 for sphinx commits before Sphinx 4.0 era.
            try:
                year = int(commit_date.split('-')[0])
                month = int(commit_date.split('-')[1])
            except (ValueError, IndexError):
                year, month = 2020, 1
            if year < 2021 or (year == 2021 and month < 5):
                # markupsafe 2.0 removed soft_unicode which jinja2<3.0 uses
                return ['jinja2<3.0', 'markupsafe<2.0', 'docutils<0.17', 'html5lib']
            return ['html5lib']
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
                # For newer SymPy (2017+): try two variants of the fix depending on
                # which era of pytest.py the commit uses.
                # Variant A: 2015-2018 era - uses "from py.test import skip, raises"
                # and has a custom skip() that raises Skipped (not recognized by pytest).
                # This fix replaces skip() with pytest.skip() and fixes py.test.mark refs.
                # (String concatenated to avoid triple-quote collision with the hunk context.)
                pytest_fix_py_test = (
                    "diff --git a/sympy/utilities/pytest.py b/sympy/utilities/pytest.py\n"
                    "index f2ab9e7d3b..871b1cad94 100644\n"
                    "--- a/sympy/utilities/pytest.py\n"
                    "+++ b/sympy/utilities/pytest.py\n"
                    "@@ -126,7 +126,7 @@ def wrapper():\n"
                    "         wrapper = functools.update_wrapper(wrapper, func)\n"
                    "         return wrapper\n"
                    " \n"
                    "-    def skip(str):\n"
                    "-        raise Skipped(str)\n"
                    "+    def skip(reason):\n"
                    "+        import pytest as _pm; _pm.skip(reason)\n"
                    " \n"
                    "     def SKIP(reason):\n"
                    "@@ -151,8 +151,9 @@ def func_wrapper():\n"
                    "         return func_wrapper\n"
                    " \n"
                    " else:\n"
                    "-    XFAIL = py.test.mark.xfail\n"
                    "-    slow = py.test.mark.slow\n"
                    "+    import pytest as _pm\n"
                    "+    XFAIL = _pm.mark.xfail\n"
                    "+    slow = _pm.mark.slow\n"
                    " \n"
                    "     def SKIP(reason):\n"
                    "         def skipping(func):\n"
                )
                fixes.append(("SymPy pytest compatibility (fix py.test.mark and skip for pytest 7+, py.test era)", pytest_fix_py_test))
                # Variant B: post-2018 era - uses "_pytest" imports directly
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
                # Variant C: 2019-2021 era - has SKIP/nocache_fail in else branch too
                # (added contextlib/warns imports, extra py.test.mark attributes)
                pytest_fix_2020 = (
                    "diff --git a/sympy/utilities/pytest.py b/sympy/utilities/pytest.py\n"
                    "index 400001e1ce..9e9c66d65c 100644\n"
                    "--- a/sympy/utilities/pytest.py\n"
                    "+++ b/sympy/utilities/pytest.py\n"
                    "@@ -201,10 +201,11 @@ def warns(warningcls, **kwargs):\n"
                    " \n"
                    " \n"
                    " else:\n"
                    "-    XFAIL = py.test.mark.xfail\n"
                    "-    SKIP = py.test.mark.skip\n"
                    "-    slow = py.test.mark.slow\n"
                    "-    nocache_fail = py.test.mark.nocache_fail\n"
                    "+    import pytest as _pm\n"
                    "+    XFAIL = _pm.mark.xfail\n"
                    "+    SKIP = _pm.mark.skip\n"
                    "+    slow = _pm.mark.slow\n"
                    "+    nocache_fail = _pm.mark.nocache_fail\n"
                    " \n"
                    " \n"
                    " @contextlib.contextmanager\n"
                )
                fixes.append(("SymPy pytest compatibility (fix py.test.mark for pytest 7+, 2020 era)", pytest_fix_2020))

        # Astropy: np.rank removed in NumPy 1.15; np.asscalar removed in NumPy 1.24.
        # Old astropy (pre-2019-06) still references these in function_helpers.py,
        # causing an AttributeError at import time when running with newer NumPy.
        if 'astropy/astropy' in self.repo and commit_date < '2020-01-01':
            astropy_nprank_fix = """diff --git a/astropy/units/quantity_helper/function_helpers.py b/astropy/units/quantity_helper/function_helpers.py
--- a/astropy/units/quantity_helper/function_helpers.py
+++ b/astropy/units/quantity_helper/function_helpers.py
@@ -128,6 +128,6 @@ UNSUPPORTED_FUNCTIONS |= {
 # variable so that we can check consistency in the test routine -
 # test_quantity_non_ufuncs.py)
-IGNORED_FUNCTIONS = {
-    # Deprecated
-    np.rank, np.asscalar,
+_np_rank = getattr(np, 'rank', None)
+_np_asscalar = getattr(np, 'asscalar', None)
+IGNORED_FUNCTIONS = set(f for f in [_np_rank, _np_asscalar] if f is not None) | {
     # I/O - useless for Quantity, since no way to store the unit.
"""
            fixes.append(("Astropy np.rank/np.asscalar compatibility (removed in newer NumPy)", astropy_nprank_fix))

            # pytest-doctestplus 0.9.0 is incompatible with pytest 7.x:
            # _getconftest_pathlist() missing 'rootpath' argument.
            # Disable the plugin via setup.cfg addopts to allow collection.
            astropy_doctestplus_fix = """diff --git a/setup.cfg b/setup.cfg
--- a/setup.cfg
+++ b/setup.cfg
@@ -102,3 +102,3 @@ remote_data_strict = true
 remote_data_strict = true
-addopts = -p no:warnings
+addopts = -p no:warnings -p no:doctestplus
 asdf_schema_root = astropy/io/misc/asdf/data/schemas
"""
            fixes.append(("Astropy pytest-doctestplus 0.9.0/pytest-7 compatibility", astropy_doctestplus_fix))

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


# ============================================================================
# LANGUAGE DETECTION AND NON-PYTHON VERSION DETECTION
# ============================================================================

def detect_language(repo_dir: Path) -> str:
    """Detect primary language from repo marker files. Mirrors per-language validator logic."""
    if (repo_dir / "Cargo.toml").exists():
        return "rust"
    if (repo_dir / "go.mod").exists():
        return "go"
    if any((repo_dir / f).exists() for f in ["pom.xml", "build.gradle", "build.gradle.kts", "build.xml"]):
        return "java"
    if (repo_dir / "composer.json").exists():
        return "php"
    # Check Python markers before package.json — some Python repos (e.g. django/django)
    # ship a package.json for frontend assets but are fundamentally Python projects.
    if any((repo_dir / f).exists() for f in ["setup.py", "pyproject.toml", "setup.cfg", "manage.py"]):
        return "python"
    if (repo_dir / "package.json").exists():
        return "javascript"
    if (repo_dir / "Gemfile").exists():
        return "ruby"
    has_build_system = any((repo_dir / f).exists() for f in ["Makefile", "CMakeLists.txt", "configure.ac", "Makefile.am"])
    if has_build_system:
        c_files = list(repo_dir.glob("*.c")) + list(repo_dir.glob("src/*.c"))
        if c_files:
            return "c"
    return "python"


def detect_rust_version(repo_dir: Path, created_at: str) -> str:
    """Detect Rust version from rust-toolchain or Cargo.toml, with date fallback."""
    for name in ["rust-toolchain", "rust-toolchain.toml"]:
        tf = repo_dir / name
        if tf.exists():
            content = tf.read_text()
            m = re.search(r'channel\s*=\s*"(\d+\.\d+)', content)
            if m:
                return m.group(1)
            m = re.search(r'"(\d+\.\d+\.\d+)"', content)
            if m:
                return m.group(1)
    cargo = repo_dir / "Cargo.toml"
    if cargo.exists():
        m = re.search(r'rust-version\s*=\s*"(\d+\.\d+)', cargo.read_text())
        if m:
            return m.group(1)
    try:
        year = int(created_at.split('-')[0])
    except (ValueError, IndexError):
        year = 2020
    if year < 2019:
        return "1.30"
    elif year < 2021:
        return "1.50"
    elif year < 2023:
        return "1.60"
    else:
        return "1.70"


def detect_go_version(repo_dir: Path, created_at: str) -> str:
    """Detect Go version from go.mod, with date fallback."""
    go_mod = repo_dir / "go.mod"
    if go_mod.exists():
        m = re.search(r'^go\s+(\d+\.\d+)', go_mod.read_text(), re.MULTILINE)
        if m:
            return m.group(1)
    try:
        year = int(created_at.split('-')[0])
    except (ValueError, IndexError):
        year = 2020
    if year < 2019:
        return "1.11"
    elif year < 2021:
        return "1.13"
    elif year < 2022:
        return "1.16"
    elif year < 2023:
        return "1.18"
    elif year < 2024:
        return "1.20"
    else:
        return "1.21"


def detect_java_version(repo_dir: Path, created_at: str) -> str:
    """Detect Java version from pom.xml or build.gradle, with date fallback.

    Collects ALL version hints from the project files (source, target, release),
    takes the maximum, then maps to the nearest conda-forge-available version:
      <= 8  → 8
      9-11  → 11  (conda-forge has no Java 9/10; --release flag needs Java 9+)
      12-17 → 17
      18+   → 21
    """
    def _normalize(v: str) -> int:
        """'1.6' → 6, '17.0.1' → 17, '9' → 9"""
        v = v.strip()
        if v.startswith('1.') and len(v) >= 3:
            try:
                return int(v.split('.')[1])
            except ValueError:
                pass
        try:
            return int(v.split('.')[0])
        except ValueError:
            return 0

    def _to_conda(ver: int) -> str:
        """Map any Java version to the nearest conda-forge-available openjdk.

        Java 11 is skipped intentionally: conda-forge's openjdk=11.0.1 (2018
        build) has broken TLS and cannot download plugins from Maven Central.
        Java 17 is the next LTS, is widely available, and supports --release
        8 through 17, covering all projects that would have used Java 11.
        """
        if ver <= 8:
            return "8"
        if ver <= 17:
            return "17"
        return "21"

    versions: List[int] = []

    pom = repo_dir / "pom.xml"
    if pom.exists():
        content = pom.read_text()
        # source / target / java.version property (old-style "1.6" or modern "11")
        for pattern in [
            r'<maven\.compiler\.source>([\d.]+)',
            r'<maven\.compiler\.target>([\d.]+)',
            r'<java\.version>([\d.]+)',
        ]:
            for m in re.finditer(pattern, content):
                v = _normalize(m.group(1))
                if v > 0:
                    versions.append(v)
        # <release>9</release> inside maven-compiler-plugin — requires Java 9+
        for m in re.finditer(r'<release>(\d+)</release>', content):
            v = int(m.group(1))
            if v > 0:
                versions.append(v)

    for name in ["build.gradle", "build.gradle.kts"]:
        gf = repo_dir / name
        if gf.exists():
            m = re.search(
                r'(?:sourceCompatibility|targetCompatibility)\s*[=:]\s*["\']?(?:JavaVersion\.VERSION_)?(\d+)["\']?',
                gf.read_text()
            )
            if m:
                v = _normalize(m.group(1))
                if v > 0:
                    versions.append(v)

    if versions:
        return _to_conda(max(versions))

    # Date-based fallback
    try:
        year = int(created_at.split('-')[0])
    except (ValueError, IndexError):
        year = 2020
    if year < 2018:
        return "8"
    elif year < 2021:
        return "11"
    elif year < 2024:
        return "17"
    else:
        return "21"


def detect_php_version(repo_dir: Path, created_at: str) -> str:
    """Detect PHP version from composer.json, with date fallback."""
    composer = repo_dir / "composer.json"
    if composer.exists():
        try:
            data = json.loads(composer.read_text())
            php_req = data.get('require', {}).get('php', '')
            m = re.search(r'(\d+\.\d+)', php_req)
            if m:
                return m.group(1)
        except Exception:
            pass
    try:
        year = int(created_at.split('-')[0])
    except (ValueError, IndexError):
        year = 2020
    if year < 2019:
        return "7.2"
    elif year < 2021:
        return "7.4"
    elif year < 2023:
        return "8.0"
    else:
        return "8.1"


def detect_js_version(repo_dir: Path, created_at: str) -> str:
    """Detect Node.js major version from .nvmrc or package.json, with date fallback."""
    nvmrc = repo_dir / ".nvmrc"
    if nvmrc.exists():
        v = nvmrc.read_text().strip().lstrip('v')
        m = re.match(r'(\d+)', v)
        if m:
            return m.group(1)
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            engines = data.get('engines', {}).get('node', '')
            m = re.search(r'(\d+)', engines)
            if m:
                return m.group(1)
        except Exception:
            pass
    try:
        year = int(created_at.split('-')[0])
    except (ValueError, IndexError):
        year = 2020
    if year < 2019:
        return "10"
    elif year < 2021:
        return "14"
    elif year < 2023:
        return "16"
    else:
        return "18"


def detect_ruby_version(repo_dir: Path, created_at: str) -> str:
    """Detect Ruby version from .ruby-version or Gemfile, with date fallback."""
    rv = repo_dir / ".ruby-version"
    if rv.exists():
        v = rv.read_text().strip().lstrip('ruby-')
        m = re.match(r'(\d+\.\d+)', v)
        if m:
            return m.group(1)
    gemfile = repo_dir / "Gemfile"
    if gemfile.exists():
        m = re.search(r"ruby\s+['\"]([^'\"]+)", gemfile.read_text())
        if m:
            parts = m.group(1).split('.')
            if len(parts) >= 2:
                return f"{parts[0]}.{parts[1]}"
    try:
        year = int(created_at.split('-')[0])
    except (ValueError, IndexError):
        year = 2020
    if year < 2019:
        return "2.5"
    elif year < 2021:
        return "2.6"
    elif year < 2022:
        return "2.7"
    elif year < 2024:
        return "3.0"
    else:
        return "3.1"


def get_language_dockerfile(language: str, script_dir: Path) -> Path:
    """Get Dockerfile path for the given language. Python uses the existing Dockerfile.instance."""
    if language == "python":
        return script_dir / "templates" / "Dockerfile.instance"
    return script_dir / "templates" / f"Dockerfile.instance.{language}"


def _py_extract_imports(patch: str) -> set:
    """Extract top-level Python package names from import statements in added lines of a patch."""
    pkgs: set = set()
    for line in patch.split('\n'):
        if not line.startswith('+') or line.startswith('+++'):
            continue
        m = re.match(r'^\+\s*import\s+([a-zA-Z_][a-zA-Z0-9_]*)', line)
        if m:
            pkgs.add(m.group(1))
            continue
        m = re.match(r'^\+\s*from\s+([a-zA-Z_][a-zA-Z0-9_]*)', line)
        if m:
            pkgs.add(m.group(1))
    return pkgs


def _py_extract_install_requires(patch: str) -> set:
    """Extract new package names added to install_requires in setup.cfg/pyproject.toml patch."""
    pkgs: set = set()
    in_install_requires = False
    for line in patch.split('\n'):
        if re.match(r'^\+?\s*install_requires\s*=', line):
            in_install_requires = True
            continue
        if in_install_requires and re.match(r'^\+?\s*\[', line):
            in_install_requires = False
            continue
        if in_install_requires and line.startswith('+') and not line.startswith('+++'):
            pkg_line = line[1:].strip()
            if pkg_line and not pkg_line.startswith('#'):
                m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_.-]*)(?:[>=<!<\[].*)?$', pkg_line)
                if m:
                    pkgs.add(m.group(1).lower().replace('-', '_'))
        if not in_install_requires and line.startswith('+') and not line.startswith('+++'):
            m = re.match(r'^\+\s{4,}([a-zA-Z_][a-zA-Z0-9_.-]*)(?:[>=<!<\[].*)?$', line)
            if m:
                pkgs.add(m.group(1).lower().replace('-', '_'))
    return pkgs


def build_instance_image(
    instance: dict,
    force_rebuild: bool = False,
    arch: str = "x86_64",
) -> Tuple[bool, str]:
    """
    Build a Docker image for a specific instance

    Args:
        instance: Instance data from dataset JSON
        force_rebuild: If True, rebuild even if image exists
        arch: Target architecture ("x86_64" or "arm64"), default "x86_64"

    Returns:
        (success, image_tag)
    """
    instance_id = instance['instance_id']
    repo = instance['repo']
    base_commit = instance['base_commit']
    created_at = instance.get('created_at', '2020-01-01')

    # Generate image tag
    image_tag = f"sweb.simple.{instance_id.replace('__', '.').lower()}:latest"

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

    repo_url = f"https://github.com/{repo}.git"

    # Clone repo to detect language and version
    print("→ Cloning repo to detect language and version...")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"

        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
            capture_output=True
        )
        if result.returncode != 0:
            print(f"✗ Failed to clone repo: {repo}")
            return False, ""

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

        # Detect language
        language = detect_language(repo_dir)
        print(f"  ✓ Detected language: {language}")

        commit_date = get_commit_date(repo_dir, base_commit)

        if language == "python":
            python_version = detect_python_version(repo_dir, created_at)
            print(f"  ✓ Detected Python version: {python_version}")
            build_deps, has_setuptools = get_build_requirements(repo_dir, repo)
            if not has_setuptools:
                historical_setuptools = get_historical_setuptools(commit_date, build_deps)
                build_deps.insert(0, historical_setuptools)
                print(f"  ✓ Commit date: {commit_date}, using {historical_setuptools}")
        elif language == "rust":
            rust_version = detect_rust_version(repo_dir, created_at)
            print(f"  ✓ Detected Rust version: {rust_version}")
        elif language == "go":
            go_version = detect_go_version(repo_dir, created_at)
            print(f"  ✓ Detected Go version: {go_version}")
        elif language == "java":
            java_version = detect_java_version(repo_dir, created_at)
            print(f"  ✓ Detected Java version: {java_version}")
        elif language == "php":
            php_version = detect_php_version(repo_dir, created_at)
            print(f"  ✓ Detected PHP version: {php_version}")
        elif language == "javascript":
            node_version = detect_js_version(repo_dir, created_at)
            print(f"  ✓ Detected Node.js version: {node_version}")
        elif language == "ruby":
            ruby_version = detect_ruby_version(repo_dir, created_at)
            print(f"  ✓ Detected Ruby version: {ruby_version}")
        else:  # c
            print(f"  ✓ C project detected (compiler from base image)")

    # Get Dockerfile and build args
    script_dir = Path(__file__).parent.parent
    dockerfile_path = get_language_dockerfile(language, script_dir)

    print(f"→ Building Docker image ({language})...")
    print(f"  This may take 5-10 minutes...")

    common_args = [
        "--build-arg", f"REPO_URL={repo_url}",
        "--build-arg", f"REPO_NAME={repo.split('/')[-1]}",
        "--build-arg", f"BASE_COMMIT={base_commit}",
        "--build-arg", f"CREATED_AT={created_at}",
    ]

    if language == "python":
        repo_config = RepoConfig(repo)
        runtime_deps = repo_config.get_runtime_deps(commit_date)
        env_vars = repo_config.get_env_vars()

        # For old matplotlib (pre-2021): pin numpy<1.19 in BUILD_DEPS so the C extensions
        # are compiled against that version.  Mirrors full_validation.py lines 966-985.
        # The runtime_deps already pins numpy<1.19 (via get_runtime_deps); pinning here
        # ensures the build-time numpy matches, preventing ABI mismatch at test time.
        if 'matplotlib' in repo and commit_date < "2021-01-01":
            build_deps = [
                'numpy<1.19' if dep.lower().startswith('numpy') or 'oldest-supported-numpy' in dep.lower()
                else dep
                for dep in build_deps
            ]
            if not any(dep.lower().startswith('numpy') for dep in build_deps):
                build_deps.append('numpy<1.19')

        print(f"→ Build deps: {build_deps}")
        print(f"→ Runtime deps: {runtime_deps}")
        if env_vars:
            print(f"→ Environment vars: {env_vars}")
        build_args = common_args + [
            "--build-arg", f"PYTHON_VERSION={python_version}",
            "--build-arg", f"BUILD_DEPS={' '.join(build_deps)}",
            "--build-arg", f"RUNTIME_DEPS={' '.join(runtime_deps)}",
            "--build-arg", f"CFLAGS={env_vars.get('CFLAGS', '')}",
        ]
    elif language == "rust":
        build_args = common_args + ["--build-arg", f"RUST_VERSION={rust_version}"]
    elif language == "go":
        build_args = common_args + ["--build-arg", f"GO_VERSION={go_version}"]
    elif language == "java":
        build_args = common_args + ["--build-arg", f"JAVA_VERSION={java_version}"]
    elif language == "php":
        build_args = common_args + ["--build-arg", f"PHP_VERSION={php_version}"]
    elif language == "javascript":
        build_args = common_args + ["--build-arg", f"NODE_VERSION={node_version}"]
    elif language == "ruby":
        build_args = common_args + ["--build-arg", f"RUBY_VERSION={ruby_version}"]
    else:  # c
        build_args = common_args

    # Use buildx with --load to produce a simple single-platform manifest (not a manifest list).
    # This ensures docker push creates a plain manifest pullable from any machine.
    platform = "linux/arm64/v8" if arch == "arm64" else "linux/amd64"
    result = subprocess.run(
        [
            "docker", "buildx", "build",
            "--platform", platform,
            "--provenance=false",
            "--load",
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

    # PHP post-build: bake all environment requirements into the image so
    # run_evaluation receives a fully ready image without runtime workarounds.
    if language == "php":
        # Step 1: Ensure SQLite extension is present
        print(f"→ Ensuring PHP SQLite extension is installed...")
        check_result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c",
             "php -m | grep -qi pdo_sqlite && echo HAS || echo MISSING"],
            capture_output=True, text=True, timeout=30
        )
        if "MISSING" in check_result.stdout:
            ver_result = subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c",
                 "php --version 2>/dev/null | head -1"],
                capture_output=True, text=True, timeout=15
            )
            m_ver = re.search(r'PHP\s+(\d+\.\d+)', ver_result.stdout)
            php_ver_str = m_ver.group(1) if m_ver else php_version
            install_cmd = (
                "apt-get update -qq 2>/dev/null && "
                f"(apt-get install -y php{php_ver_str}-sqlite3 2>/dev/null || "
                "apt-get install -y php-sqlite3 2>/dev/null || true)"
            )
            ext_container = f"php_ext_{instance_id.replace('/', '_').replace('__', '_')}"
            subprocess.run(["docker", "rm", "-f", ext_container], capture_output=True)
            subprocess.run(
                ["docker", "run", "--name", ext_container, image_tag, "bash", "-c", install_cmd],
                capture_output=True, text=True, timeout=120
            )
            subprocess.run(["docker", "commit", ext_container, image_tag], capture_output=True)
            subprocess.run(["docker", "rm", "-f", ext_container], capture_output=True)
            print(f"  ✓ PHP SQLite extension installed")
        else:
            print(f"  ✓ PHP SQLite extension already present")

        # Step 2: Ensure PHP version meets PHPUnit's requirement
        print(f"→ Checking PHPUnit PHP version requirement...")
        phpunit_ver_result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c",
             "cd /testbed && vendor/bin/phpunit --version 2>&1 | head -5 || true"],
            capture_output=True, text=True, timeout=30
        )
        req_match = re.search(r'requires PHP >= (\d+\.\d+)', phpunit_ver_result.stdout + phpunit_ver_result.stderr)
        if req_match:
            required_ver = req_match.group(1)
            cur_result = subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c",
                 "php --version 2>/dev/null | head -1 || true"],
                capture_output=True, text=True, timeout=15
            )
            cur_match = re.search(r'PHP\s+(\d+\.\d+)', cur_result.stdout)
            if cur_match:
                current_ver = cur_match.group(1)
                def _ver_tuple(v):
                    return tuple(int(x) for x in v.split('.'))
                if _ver_tuple(current_ver) < _ver_tuple(required_ver):
                    print(f"  → PHPUnit requires PHP >= {required_ver} (image has PHP {current_ver}), upgrading...")
                    upgrade_cmd = (
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
                    upgrade_container = f"php_upgrade_{instance_id.replace('/', '_').replace('__', '_')}"
                    subprocess.run(["docker", "rm", "-f", upgrade_container], capture_output=True)
                    subprocess.run(
                        ["docker", "run", "--name", upgrade_container, image_tag, "bash", "-c", upgrade_cmd],
                        capture_output=True, text=True, timeout=300
                    )
                    subprocess.run(["docker", "commit", upgrade_container, image_tag], capture_output=True)
                    subprocess.run(["docker", "rm", "-f", upgrade_container], capture_output=True)
                    print(f"  ✓ PHP upgraded to {required_ver}")
                else:
                    print(f"  ✓ PHP {current_ver} already meets PHPUnit requirement (>= {required_ver})")
        else:
            print(f"  ✓ PHPUnit has no additional PHP version requirement")

    # For Python: pre-install packages that test_patch imports but the model patch adds to
    # install_requires.  Without this, pytest collection fails with ModuleNotFoundError
    # (e.g. appdirs) because the base image is built at base_commit before the dependency
    # was added.  Baking it into the image means every model evaluation gets it for free.
    if language == "python":
        test_patch = instance.get('test_patch', '')
        gt_patch = instance.get('patch', '')
        if test_patch and gt_patch:
            _test_imports = _py_extract_imports(test_patch)
            _gt_deps = _py_extract_install_requires(gt_patch)
            _needed = _test_imports & _gt_deps
            if _needed:
                print(f"→ Pre-installing {len(_needed)} test-patch dependency(ies): {', '.join(sorted(_needed))}")
                _install_pkgs = ' '.join(sorted(_needed))
                _install_cmd = f"cd /testbed && /opt/conda/envs/testbed/bin/pip install {_install_pkgs} -q 2>&1"
                _dep_container = f"predeps_{instance_id.replace('/', '_').replace('__', '_')}"
                subprocess.run(["docker", "rm", "-f", _dep_container], capture_output=True)
                _dep_result = subprocess.run(
                    ["docker", "run", "--name", _dep_container, image_tag, "bash", "-c", _install_cmd],
                    capture_output=True, text=True, timeout=120
                )
                if _dep_result.returncode == 0:
                    subprocess.run(["docker", "commit", _dep_container, image_tag], capture_output=True)
                    print(f"  ✓ Pre-installed: {', '.join(sorted(_needed))}")
                else:
                    print(f"  ⚠ Pre-install failed: {_dep_result.stdout[-200:]}")
                subprocess.run(["docker", "rm", "-f", _dep_container], capture_output=True)

    # Python post-build: verify the package is importable.
    # pip install -e . uses || true in the Dockerfile, so build failures are silent.
    # Repos with C extensions (scikit-learn, astropy) need cython<0.30 pinned; if a newer
    # Cython was present (e.g. from a previous pip call), the compilation silently fails.
    # Mirrors full_validation.py step 6: verify import, retry with pinned build deps if needed.
    if language == "python":
        repo_config = RepoConfig(repo)
        pkg_name = repo_config.get_package_name()
        print(f"→ Verifying Python package '{pkg_name}' is importable...")
        check_result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "bash", "-c",
             f"python -c 'import {pkg_name}' 2>/dev/null && echo OK || echo FAILED"],
            capture_output=True, text=True, timeout=30
        )
        if "FAILED" in check_result.stdout:
            print(f"  ⚠ Package '{pkg_name}' not importable — rebuilding with pinned build deps...")
            needs_no_isolation = any(r in repo for r in ['scikit-learn', 'astropy', 'matplotlib'])
            cflags = '-Wno-error=incompatible-pointer-types' if any(r in repo for r in ['scikit-learn', 'astropy']) else ''
            rebuild_parts = ["cd /testbed"]
            if cflags:
                rebuild_parts.append(f"export CFLAGS='{cflags}'")
            # Install pinned build deps (cython<0.30 required for old scikit-learn/astropy Cython code)
            # Each dep must be single-quoted so the shell does not interpret < as redirection
            pinned_build_deps = ' '.join(f"'{dep}'" for dep in repo_config.get_build_deps())
            rebuild_parts.append(
                f"/opt/conda/envs/testbed/bin/pip install {pinned_build_deps} -q 2>&1 || true"
            )
            pip_flags = "--no-build-isolation" if needs_no_isolation else ""
            rebuild_parts.append(
                f"/opt/conda/envs/testbed/bin/pip install -e . {pip_flags} -q 2>&1 || "
                f"/opt/conda/envs/testbed/bin/pip install . {pip_flags} -q 2>&1 || "
                f"/opt/conda/envs/testbed/bin/python setup.py develop -q 2>&1 || true"
            )
            rebuild_cmd = " && ".join(rebuild_parts)
            rebuild_container = f"rebuild_pkg_{instance_id.replace('/', '_').replace('__', '_')}"
            subprocess.run(["docker", "rm", "-f", rebuild_container], capture_output=True)
            rebuild_result = subprocess.run(
                ["docker", "run", "--name", rebuild_container, image_tag, "bash", "-c", rebuild_cmd],
                capture_output=True, text=True, timeout=600
            )
            subprocess.run(["docker", "commit", rebuild_container, image_tag], capture_output=True)
            subprocess.run(["docker", "rm", "-f", rebuild_container], capture_output=True)
            # Final verification
            recheck = subprocess.run(
                ["docker", "run", "--rm", image_tag, "bash", "-c",
                 f"python -c 'import {pkg_name}' 2>/dev/null && echo OK || echo FAILED"],
                capture_output=True, text=True, timeout=30
            )
            if "OK" in recheck.stdout:
                print(f"  ✓ Package '{pkg_name}' rebuilt and importable")
            else:
                print(f"  ✗ Package '{pkg_name}' still not importable after rebuild — image may produce 0 test results")
        else:
            print(f"  ✓ Package '{pkg_name}' importable")

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
    parser.add_argument(
        "--arch",
        default="x86_64",
        choices=["x86_64", "arm64"],
        help="Target architecture (default: x86_64)"
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
            force_rebuild=args.force_rebuild,
            arch=args.arch,
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
