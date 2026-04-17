#!/usr/bin/env python3
"""
SWE-bench Instance Validation Script

Workflow:
1. Clone repo at base_commit
2. Detect Python version from repo files
3. Create isolated conda environment
4. Install dependencies
5. Find and run tests before patches
6. Apply patches and run tests after
7. Identify FAIL_TO_PASS and PASS_TO_PASS tests

Usage:
    python3 full_validation.py instance.json [--output validated.json] [--keep-env]
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple, Optional


# ============================================================================
# REPO-SPECIFIC CONFIGURATIONS
# ============================================================================

class RepoConfig:
    """Configuration for repo-specific behaviors"""

    def __init__(self, repo: str):
        self.repo = repo.lower()
        self.repo_name = repo.split('/')[-1]

    def get_package_name(self) -> str:
        """Get importable package name"""
        mappings = {
            'scikit-learn': 'sklearn',
            'pytest': 'pytest',
            'requests': 'requests',
            'flask': 'flask',
            'seaborn': 'seaborn',
            'matplotlib': 'matplotlib',
            'django': 'django',
            'astropy': 'astropy',
        }
        for key, value in mappings.items():
            if key in self.repo:
                return value
        return self.repo_name.replace('-', '_')

    def get_build_deps(self) -> List[str]:
        """Get build-time dependencies (excluding setuptools - handled by historical detection)"""
        if 'scikit-learn' in self.repo or 'sklearn' in self.repo:
            return ['numpy<2', 'scipy==1.3.3', 'cython<0.30']
        elif 'astropy' in self.repo:
            return ['numpy<2', 'cython<0.30', 'setuptools-scm', 'extension-helpers']
        else:
            return ['wheel']

    def get_runtime_deps(self) -> List[str]:
        """Get runtime dependencies"""
        if 'scikit-learn' in self.repo:
            return ['numpy<2', 'scipy', 'joblib', 'threadpoolctl', 'pandas']
        elif 'matplotlib' in self.repo:
            # Note: freetype is installed via conda for system library (not pip)
            return ['numpy<2', 'pillow', 'pyparsing', 'python-dateutil', 'cycler', 'pandas']
        elif 'seaborn' in self.repo:
            return ['numpy<2', 'pandas', 'matplotlib']
        elif 'astropy' in self.repo:
            return ['numpy<2', 'scipy', 'pytest-astropy', 'pytest-doctestplus']
        elif 'django' in self.repo:
            return ['pytz', 'sqlparse', 'asgiref']
        elif 'flask' in self.repo:
            return ['werkzeug<2.1', 'jinja2', 'click', 'itsdangerous']
        elif 'requests' in self.repo:
            return ['urllib3', 'chardet', 'certifi', 'idna']
        elif 'sphinx-doc/sphinx' in self.repo:
            return ['html5lib']
        return []

    def needs_special_install(self) -> Optional[str]:
        """Returns special installation method if needed"""
        if 'scikit-learn' in self.repo or 'astropy' in self.repo or 'matplotlib' in self.repo:
            return 'no-build-isolation'
        elif 'sphinx-doc/sphinx' in self.repo:
            return 'ignore-installed'
        return None

    def get_env_vars(self) -> Dict[str, str]:
        """Get environment variables for building"""
        import platform
        env = {}

        if platform.system() == 'Darwin':
            if 'scikit-learn' in self.repo:
                env.update({
                    'SKLEARN_NO_OPENMP': '1',
                    'CC': 'clang',
                    'CXX': 'clang++',
                })
            if 'astropy' in self.repo:
                # -UTARGET_OS_MAC: prevents cfitsio/zlib zutil.h from defining
                # fdopen(fd,mode) as NULL (triggered by TARGET_OS_MAC being predefined
                # by Apple's clang), which conflicts with stdio.h's fdopen declaration
                # under newer macOS SDKs (Xcode 17+) and causes a syntax error.
                env['CFLAGS'] = '-std=gnu89 -Wno-implicit-function-declaration -UTARGET_OS_MAC'

        # Add general flags for old C code compatibility
        if 'scikit-learn' in self.repo or 'astropy' in self.repo:
            cflags = env.get('CFLAGS', '')
            env['CFLAGS'] = f"{cflags} -Wno-error=incompatible-function-pointer-types".strip()

        return env

    def uses_django_runner(self) -> bool:
        """Check if repo uses Django test runner"""
        return 'django/django' in self.repo

    def get_infrastructure_fixes(self, commit_date: str) -> List[Tuple[str, str]]:
        """
        Get infrastructure fix patches to apply before running tests.

        Returns list of (description, patch_content) tuples for repo-specific
        fixes needed at certain commit dates to make tests runnable.

        These are NOT solution patches - they fix broken test infrastructure
        in old commits (e.g., pytest compatibility issues).

        Args:
            commit_date: Commit date in YYYY-MM-DD format

        Returns:
            List of (description, patch) tuples to apply before running tests
        """
        fixes = []

        # SymPy: pytest compatibility fix (needed before 2016-10-10)
        # Bug: sympy/utilities/pytest.py doesn't import 'raises' from py.test
        # when USE_PYTEST=True, causing "ImportError: cannot import name 'raises'"
        # Fixed in commit 6ff2372fd5 (2016-10-10)
        if 'sympy/sympy' in self.repo and commit_date < '2016-10-10':
            pytest_fix = """diff --git a/sympy/utilities/pytest.py b/sympy/utilities/pytest.py
index 82ba9cdc5d..9f96d169d9 100644
--- a/sympy/utilities/pytest.py
+++ b/sympy/utilities/pytest.py
@@ -10,7 +10,7 @@ from sympy.core.compatibility import get_function_name

 try:
     import py
-    from py.test import skip
+    from py.test import skip, raises
     USE_PYTEST = getattr(sys, '_running_pytest', False)
 except ImportError:
     USE_PYTEST = False
"""
            fixes.append(("SymPy pytest compatibility (add 'raises' import)", pytest_fix))

        # Django: html_parser.HTMLParseError removed in Python 3.5+
        # Old Django commits (before ~2016) assign HTMLParseError directly without
        # a try/except, crashing at import time on Python 3.5+ with AttributeError.
        # This makes runtests.py crash before any tests run, giving 0 results.
        if 'django/django' in self.repo and commit_date < '2016-01-01':
            html_parser_fix = """diff --git a/django/utils/html_parser.py b/django/utils/html_parser.py
--- a/django/utils/html_parser.py
+++ b/django/utils/html_parser.py
@@ -7,8 +7,13 @@ current_version = sys.version_info
 use_workaround = (
     (current_version < (2, 7, 3)) or
     (current_version >= (3, 0) and current_version < (3, 2, 3))
 )

-HTMLParseError = _html_parser.HTMLParseError
+try:
+    HTMLParseError = _html_parser.HTMLParseError
+except AttributeError:
+    # HTMLParseError was removed in Python 3.5+.
+    class HTMLParseError(Exception):
+        pass

 if not use_workaround:
"""
            fixes.append(("Django html_parser HTMLParseError compatibility (Python 3.5+)", html_parser_fix))

        return fixes


# ============================================================================
# MAIN VALIDATOR CLASS
# ============================================================================

class EnvironmentValidator:
    def __init__(self, instance: dict, workspace: Path, keep_env: bool = False):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.env_name = f"swe_temp_{instance['instance_id'].replace('/', '_').replace('-', '_')}"
        self.keep_env = keep_env
        self.python_version = None
        self.config = RepoConfig(instance['repo'])
        self.using_non_editable = False
        self._env_python_cache = None  # Cache for get_env_python()
        self._runtime_env_vars: dict = {}  # Extra env vars applied to every run_in_env call
        self._is_meson_build: bool = False  # Set during install; used by apply_patch reinstall
        self._install_env_vars: dict = {}  # Build-time env vars (LDFLAGS, PKG_CONFIG_PATH, etc.)

    # ========================================================================
    # STEP 1: REPOSITORY SETUP
    # ========================================================================

    def setup_repo(self):
        """Clone repository at base_commit"""
        print(f"[1/7] Setting up repository...")
        repo_url = f"https://github.com/{self.instance['repo']}.git"

        # Clone shallow, fetch specific commit, checkout
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(self.repo_dir)],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "fetch", "--depth=100", "origin", self.instance['base_commit']],
            cwd=self.repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "checkout", self.instance['base_commit']],
            cwd=self.repo_dir, check=True, capture_output=True
        )

        print(f"      ✓ Cloned at {self.instance['base_commit'][:8]}")

    # ========================================================================
    # STEP 2: PYTHON VERSION DETECTION
    # ========================================================================

    @staticmethod
    def parse_version(version_str: str) -> Tuple[int, int]:
        """Parse version string '3.10' -> (3, 10)"""
        try:
            parts = version_str.split('.')
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            return (3, 7)

    def get_python_version_from_date(self, created_at: str) -> str:
        """
        Determine appropriate Python version based on creation date.

        Historical Python version timeline:
        - Python 3.6: Sept 2016 - Dec 2018
        - Python 3.7: June 2018 - June 2020
        - Python 3.8: Oct 2019 - Oct 2021
        - Python 3.9: Oct 2020 - Oct 2022
        - Python 3.10: Oct 2021 - Oct 2023
        - Python 3.11: Oct 2022+
        """
        try:
            # Parse ISO 8601 format: "2017-05-18T18:07:02Z"
            year = int(created_at.split('-')[0])
            month = int(created_at.split('-')[1])

            # Use the most common Python version for that time period
            if year < 2017:
                return "3.5"
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
            return "3.7"  # Ultimate fallback

    def detect_python_version(self) -> str:
        """Auto-detect Python version from repo files"""
        print(f"[2/7] Detecting Python version...")

        MIN_VERSION = (3, 6)

        # Check multiple sources in priority order
        # pyproject.toml is most authoritative for modern projects, check it first
        sources = [
            (self.repo_dir / ".python-version", r'(\d+\.\d+)'),
            (self.repo_dir / "pyproject.toml", r'requires-python\s*=\s*["\']>=?\s*(\d+\.\d+)'),
            (self.repo_dir / "setup.py", r'python_requires\s*=\s*["\']>=?(\d+\.\d+)'),
            (self.repo_dir / "setup.cfg", r'python_requires\s*=\s*>=?(\d+\.\d+)'),
            (self.repo_dir / "tox.ini", r'py(\d)(\d+)'),
        ]

        for filepath, pattern in sources:
            if not filepath.exists():
                continue

            content = filepath.read_text()
            match = re.search(pattern, content)

            if match:
                if filepath.name == "tox.ini":
                    version = f"{match.group(1)}.{match.group(2)}"
                else:
                    version = match.group(1)

                # For pyproject.toml, verify this is the minimum, not just a test version
                if filepath.name == "tox.ini":
                    # Skip tox.ini if we can infer from date that a higher version is needed
                    created_at = self.instance.get('created_at', '')
                    if created_at:
                        date_version = self.get_python_version_from_date(created_at)
                        if self.parse_version(date_version) > self.parse_version(version):
                            print(f"      ⚠ Found Python {version} in tox.ini, but using {date_version} based on date")
                            return date_version

                if self.parse_version(version) >= MIN_VERSION:
                    print(f"      ✓ Found Python {version} in {filepath.name}")
                    return version

        # Fallback: use created_at date to determine appropriate Python version
        created_at = self.instance.get('created_at', '')
        if created_at:
            version = self.get_python_version_from_date(created_at)
            print(f"      ⚠ Using Python {version} based on creation date ({created_at[:10]})")
            return version

        # Ultimate fallback
        DEFAULT = "3.7"
        print(f"      ⚠ Using default Python {DEFAULT}")
        return DEFAULT

    # ========================================================================
    # STEP 3: CONDA ENVIRONMENT
    # ========================================================================

    def create_environment(self, python_version: str):
        """Create conda environment with specified Python version"""
        print(f"[3/7] Creating conda environment (Python {python_version})...")
        env = os.environ.copy()
        # On Apple Silicon, older Python builds (e.g., 3.6/3.7) are only available for osx-64.
        try:
            major, minor, *_ = (int(p) for p in python_version.split("."))
        except Exception:
            major, minor = 0, 0
        if platform.machine() == "arm64" and (major, minor) < (3, 8):
            env["CONDA_SUBDIR"] = "osx-64"

        # Remove if exists
        result = subprocess.run(["conda", "env", "list"], capture_output=True, text=True)
        if self.env_name in result.stdout:
            subprocess.run(
                ["conda", "env", "remove", "-n", self.env_name, "-y"],
                capture_output=True
            )

        # Create new
        result = subprocess.run(
            [
                "conda", "create", "-n", self.env_name, f"python={python_version}",
                "-y", "-q", "-c", "conda-forge", "-c", "defaults"
            ],
            capture_output=True, text=True, env=env
        )

        # If online creation fails (e.g., conda-forge unreachable), retry with cached packages
        if result.returncode != 0:
            result = subprocess.run(
                [
                    "conda", "create", "-n", self.env_name, f"python={python_version}",
                    "-y", "-q", "--offline"
                ],
                capture_output=True, text=True, env=env
            )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create environment: {result.stderr}")

        print(f"      ✓ Environment '{self.env_name}' created")

    def get_env_python(self) -> Path:
        """Get path to Python executable in conda environment"""
        if self._env_python_cache is not None:
            return self._env_python_cache

        result = subprocess.run(["conda", "info", "--json"], capture_output=True, text=True)
        conda_info = json.loads(result.stdout)

        for env in conda_info.get("envs", []):
            if self.env_name in env:
                env_path = Path(env)
                python_path = env_path / "bin" / "python"
                if not python_path.exists():
                    python_path = env_path / "python.exe"  # Windows
                self._env_python_cache = python_path
                return python_path

        raise RuntimeError(f"Environment {self.env_name} not found")

    def run_in_env(self, command: List[str], cwd: Optional[Path] = None,
                   env_vars: dict = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        """Run command in conda environment"""
        env_python = self.get_env_python()

        # Replace python/pip with full path
        if command[0] == "python":
            command[0] = str(env_python)
        elif command[0] == "pip":
            command = [str(env_python), "-m", "pip"] + command[1:]

        # Setup environment
        env = os.environ.copy()
        env['PATH'] = f"{env_python.parent}:{env.get('PATH', '')}"
        # Apply persistent runtime env vars (e.g. DYLD_LIBRARY_PATH for conda libs)
        if self._runtime_env_vars:
            env.update(self._runtime_env_vars)
        if env_vars:
            env.update(env_vars)

        try:
            return subprocess.run(
                command,
                cwd=cwd or self.repo_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout
            )
        except subprocess.TimeoutExpired:
            # Return a fake result indicating timeout (treated as test failure)
            return subprocess.CompletedProcess(command, returncode=-1,
                                               stdout="", stderr=f"TIMEOUT after {timeout}s")

    # ========================================================================
    # STEP 4: DEPENDENCY INSTALLATION
    # ========================================================================

    def get_commit_date(self) -> str:
        """Get the commit date of base_commit in YYYY-MM-DD format"""
        result = subprocess.run(
            ["git", "show", "-s", "--format=%ci", self.instance['base_commit']],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            # Output format: "2020-08-15 10:30:45 -0700"
            date_str = result.stdout.strip().split()[0]
            return date_str
        return "2021-01-01"  # Fallback to recent date

    def get_historical_setuptools(self, commit_date: str, build_reqs: List[str]) -> str:
        """
        Map commit date to appropriate setuptools version.

        Special handling for Cython: Old Cython (<0.30) requires setuptools.dep_util,
        which was removed in setuptools 50. So if Cython<0.30 is present, force setuptools<50.

        Special handling for setuptools_scm: setuptools_scm>=7 requires setuptools>=60.

        Historical setuptools timeline:
        - setuptools 69.x (2024+): Modern, breaks old code
        - setuptools 58.x (2021-2022): Python 3.10+ era
        - setuptools 50.x (2020): Removed dep_util module (breaks old Cython!)
        - setuptools 45.x (2019-2020): Last stable with dep_util
        - setuptools 40.x (2018-2019): Python 3.7 era
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

    def get_build_requirements(self) -> List[str]:
        """Extract build requirements from base_commit's pyproject.toml or setup.py"""
        build_reqs = []
        has_setuptools_constraint = False

        # Check pyproject.toml first
        pyproject = self.repo_dir / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                # Look for [build-system] requires = [...] with proper bracket depth tracking
                # to handle nested brackets like setuptools_scm[toml]>=3.4
                build_sys_match = re.search(r'\[build-system\].*?requires\s*=\s*\[', content, re.DOTALL)
                reqs_text = None
                if build_sys_match:
                    # Find the matching closing bracket using depth counting
                    start = build_sys_match.end() - 1  # position of opening '['
                    depth = 0
                    in_string = False
                    string_char = None
                    i = start
                    while i < len(content):
                        ch = content[i]
                        if in_string:
                            if ch == '\\':
                                i += 2
                                continue
                            if ch == string_char:
                                in_string = False
                        elif ch in ('"', "'"):
                            in_string = True
                            string_char = ch
                        elif ch == '[':
                            depth += 1
                        elif ch == ']':
                            depth -= 1
                            if depth == 0:
                                reqs_text = content[start + 1:i]
                                break
                        i += 1
                if reqs_text is not None:
                    # Match complete quoted strings: "..." or '...' (avoids nested quotes in PEP 508 markers)
                    for req_match in re.finditer(r'"([^"]*)"|\'([^\']*)\'', reqs_text):
                        # Group 1 is double-quoted, Group 2 is single-quoted
                        req = req_match.group(1) if req_match.group(1) is not None else req_match.group(2)
                        # Strip PEP 508 environment markers (e.g., "; python_version=='3.10'")
                        req = req.split(';')[0].strip()
                        if not req:
                            continue
                        # Pin numpy<2 for compatibility
                        if req.lower().startswith('numpy') or 'oldest-supported-numpy' in req.lower():
                            if not any(op in req for op in ['==', '>=', '<=', '>', '<', '~=']):
                                req = 'numpy<2'
                        # Check if setuptools (NOT setuptools_scm, setuptools-scm, etc.) has version constraint
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
            setup_py = self.repo_dir / "setup.py"
            if setup_py.exists():
                try:
                    content = setup_py.read_text()
                    match = re.search(r'setup_requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
                    if match:
                        reqs_text = match.group(1)
                        # Match complete quoted strings: "..." or '...'
                        for req_match in re.finditer(r'"([^"]*)"|\'([^\']*)\'', reqs_text):
                            # Group 1 is double-quoted, Group 2 is single-quoted
                            req = req_match.group(1) if req_match.group(1) is not None else req_match.group(2)
                            # Strip PEP 508 environment markers
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
            build_reqs = self.config.get_build_deps()

        # Always add historical setuptools if no version constraint found
        if not has_setuptools_constraint:
            commit_date = self.get_commit_date()
            historical_setuptools = self.get_historical_setuptools(commit_date, build_reqs)
            build_reqs.insert(0, historical_setuptools)

        # Jinja2 2.10.x requires old MarkupSafe (soft_unicode removed in newer versions)
        if any(req.lower().startswith("jinja2==2.10") for req in build_reqs):
            if not any(req.lower().startswith("markupsafe") for req in build_reqs):
                build_reqs.append("markupsafe<2.1")

        # Cython 3.0 (July 2023) has breaking changes incompatible with old .pyx files.
        # Cap Cython<3.0 for scikit-learn commits before June 2023 (when sklearn 1.3
        # added Cython 3 support).
        if 'scikit-learn' in self.instance.get('repo', ''):
            commit_date = self.get_commit_date()
            if commit_date and commit_date < '2023-06-01':
                build_reqs = [
                    re.sub(r'^[Cc]ython(>=[\d.]+)?$', 'Cython>=0.29.24,<3.0', req)
                    if req.lower().startswith('cython') else req
                    for req in build_reqs
                ]
        return build_reqs

    def get_pytest_requirements_from_base_commit(self) -> List[str]:
        """Extract pytest requirements from base_commit's test configuration"""
        pytest_reqs = []

        # 1. Check tox.ini
        tox_ini = self.repo_dir / "tox.ini"
        if tox_ini.exists():
            try:
                content = tox_ini.read_text()
                in_testenv = False
                in_deps = False
                for line in content.split('\n'):
                    stripped = line.strip()
                    if stripped.startswith('[testenv'):
                        in_testenv = True
                        in_deps = False
                    elif in_testenv and stripped.startswith('deps'):
                        in_deps = True
                        if '=' in stripped:
                            deps_part = stripped.split('=', 1)[1].strip()
                            if deps_part and 'pytest' in deps_part.lower():
                                pytest_reqs.append(deps_part)
                    elif in_testenv and in_deps:
                        if stripped.startswith('[') or (stripped and not line[0].isspace() and '=' in stripped):
                            in_deps = False
                        elif stripped and not stripped.startswith('#') and 'pytest' in stripped.lower():
                            pytest_reqs.append(stripped)
            except Exception:
                pass

        # 2. Check requirements files
        test_req_files = [
            'requirements/testing/minver.txt',
            'requirements/testing.txt',
            'requirements-test.txt',
            'test-requirements.txt',
            'requirements_test.txt',
            'test_requirements.txt',
        ]
        for req_file in test_req_files:
            req_path = self.repo_dir / req_file
            if req_path.exists():
                try:
                    content = req_path.read_text()
                    for line in content.split('\n'):
                        line = line.strip()
                        if line and not line.startswith('#') and 'pytest' in line.lower():
                            pytest_reqs.append(line)
                except Exception:
                    pass

        # 3. Check setup.py
        setup_py = self.repo_dir / "setup.py"
        if setup_py.exists():
            try:
                content = setup_py.read_text()
                # Look for tests_require or extras_require['test']
                for match in re.finditer(r'tests_require\s*=\s*\[(.*?)\]', content, re.DOTALL):
                    reqs_text = match.group(1)
                    for req_match in re.finditer(r'["\']([^"\']+)["\']', reqs_text):
                        req = req_match.group(1)
                        if 'pytest' in req.lower():
                            pytest_reqs.append(req)
                for match in re.finditer(r'extras_require\s*=\s*\{(.*?)\}', content, re.DOTALL):
                    extras_text = match.group(1)
                    # Look for 'test': [...] or "test": [...]
                    test_match = re.search(r'["\']test["\']\s*:\s*\[(.*?)\]', extras_text, re.DOTALL)
                    if test_match:
                        reqs_text = test_match.group(1)
                        for req_match in re.finditer(r'["\']([^"\']+)["\']', reqs_text):
                            req = req_match.group(1)
                            if 'pytest' in req.lower():
                                pytest_reqs.append(req)
            except Exception:
                pass

        # 4. Check setup.cfg
        setup_cfg = self.repo_dir / "setup.cfg"
        if setup_cfg.exists():
            try:
                content = setup_cfg.read_text()
                in_testing = False
                for line in content.split('\n'):
                    stripped = line.strip()
                    if stripped.startswith('[') and ('test' in stripped.lower() or 'options.extras_require' in stripped.lower()):
                        in_testing = True
                    elif stripped.startswith('['):
                        in_testing = False
                    elif in_testing and 'pytest' in stripped.lower() and '=' in stripped:
                        # Extract requirement
                        req = stripped.split('=', 1)[-1].strip()
                        if req:
                            pytest_reqs.append(req)
            except Exception:
                pass

        # 5. Check pyproject.toml
        pyproject = self.repo_dir / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                # Look for test dependencies
                test_match = re.search(r'\[project\.optional-dependencies\].*?test\s*=\s*\[(.*?)\]', content, re.DOTALL)
                if test_match:
                    reqs_text = test_match.group(1)
                    for req_match in re.finditer(r'["\']([^"\']+)["\']', reqs_text):
                        req = req_match.group(1)
                        if 'pytest' in req.lower():
                            pytest_reqs.append(req)
            except Exception:
                pass

        # 6. Check CI files (.travis.yml, .github/workflows/*.yml)
        ci_files = [
            self.repo_dir / ".travis.yml",
            self.repo_dir / ".github" / "workflows" / "test.yml",
            self.repo_dir / ".github" / "workflows" / "tests.yml",
            self.repo_dir / ".github" / "workflows" / "ci.yml",
        ]
        for ci_file in ci_files:
            if ci_file.exists():
                try:
                    content = ci_file.read_text()
                    # Look for pip install pytest or pytest== patterns
                    for match in re.finditer(r'pytest[=<>!]+[\d\.]+', content):
                        pytest_reqs.append(match.group(0))
                except Exception:
                    pass

        return pytest_reqs

    def get_date_based_pytest_constraint(self) -> str:
        """Get safe pytest version constraint based on commit date"""
        commit_date = self.get_commit_date()
        try:
            year = int(commit_date.split('-')[0])
            month = int(commit_date.split('-')[1])

            # Pytest version timeline:
            # - pytest 3.0: Nov 2016
            # - pytest 3.6: June 2018
            # - pytest 4.0: Nov 2018
            # - pytest 4.6: June 2019
            # - pytest 5.0: June 2019
            # - pytest 6.0: July 2020
            # - pytest 7.0: Feb 2022

            if year < 2017:
                return "pytest>=2.8,<3.0"
            elif year == 2017:
                return "pytest>=3.0,<3.6"
            elif year == 2018 and month < 6:
                return "pytest>=3.0,<3.6"
            elif year == 2018:
                return "pytest>=3.6,<4.0"
            elif year == 2019 and month < 6:
                return "pytest>=3.6,<4.6"
            elif year == 2019:
                return "pytest>=4.6,<5.0"
            elif year == 2020:
                return "pytest>=4.6,<6.0"
            elif year == 2021:
                return "pytest>=5.0,<7.0"
            elif year == 2022:
                return "pytest>=6.0,<7.0"
            elif year >= 2023:
                return "pytest>=6.0,<8.0"
            else:
                # Ultimate fallback based on Python version
                version_tuple = self.parse_version(self.python_version)
                if version_tuple <= (3, 6):
                    return "pytest>=3.0,<4.6"
                elif version_tuple == (3, 7):
                    return "pytest>=4.6,<7.0"
                elif version_tuple <= (3, 9):
                    return "pytest>=5.0,<8.0"
                else:
                    return "pytest>=6.0,<8.0"
        except (ValueError, IndexError):
            # If date parsing fails, use Python version fallback
            version_tuple = self.parse_version(self.python_version)
            if version_tuple <= (3, 6):
                return "pytest>=3.0,<4.6"
            elif version_tuple == (3, 7):
                return "pytest>=4.6,<7.0"
            elif version_tuple <= (3, 9):
                return "pytest>=5.0,<8.0"
            else:
                return "pytest>=6.0,<8.0"

    def get_pytest_version(self) -> str:
        """Get pytest version from base_commit or fallback to date-based compatibility"""
        # Get date-based constraint first (this is our safety net)
        date_based_constraint = self.get_date_based_pytest_constraint()

        # Try to get from base_commit
        base_pytest_reqs = self.get_pytest_requirements_from_base_commit()

        if base_pytest_reqs:
            # Use the first pytest requirement with version constraints
            for req in base_pytest_reqs:
                if req.lower().startswith('pytest') and not any(x in req.lower() for x in ['pytest-', 'pytest_']):
                    if any(op in req for op in ['==', '>=', '<=', '>', '<', '~=', '!=']):
                        # Check if it has upper bound
                        has_upper_bound = any(op in req for op in ['<', '<=', '==', '~='])

                        if has_upper_bound:
                            # Has upper bound, use as-is
                            print(f"      → Using pytest from base_commit: {req}")
                            return req
                        else:
                            # No upper bound (e.g., pytest>=3.0), apply date-based cap
                            # Extract the lower bound and combine with date-based upper bound
                            date_upper = date_based_constraint.split(',')[1] if ',' in date_based_constraint else date_based_constraint
                            combined = f"{req},{date_upper}"
                            print(f"      → Base commit has '{req}', applying date-based cap: {combined}")
                            return combined

        # No base_commit requirement found, use date-based
        print(f"      → Using date-based pytest constraint: {date_based_constraint}")
        return date_based_constraint

    def get_hypothesis_constraint(self) -> str:
        """Get compatible hypothesis version based on commit date"""
        # Use commit date to determine appropriate hypothesis version
        # This ensures we install versions contemporary with the codebase

        commit_date = self.get_commit_date()
        try:
            year = int(commit_date.split('-')[0])
            month = int(commit_date.split('-')[1])

            # Hypothesis version timeline:
            # - hypothesis 1.x: 2013-2015
            # - hypothesis 2.x: 2015-2016
            # - hypothesis 3.0-3.44: 2016-2017
            # - hypothesis 3.45-3.82: 2017-2018
            # - hypothesis 4.0-4.57: late 2018-2019 (requires pytest 4.3+)
            # - hypothesis 5.0-5.43: 2019-2020
            # - hypothesis 6.0+: 2020+

            if year < 2016:
                return "hypothesis>=1.0,<3.0"
            elif year == 2016:
                return "hypothesis>=3.0,<3.45"
            elif year == 2017:
                return "hypothesis>=3.0,<3.82"
            elif year == 2018 and month < 6:
                # Early 2018 - use hypothesis 3.x to avoid pytest 4.3+ requirement
                return "hypothesis>=3.45,<3.82"
            elif year == 2018:
                # Late 2018 - still use hypothesis 3.x for safety
                return "hypothesis>=3.70,<4.0"
            elif year == 2019:
                # 2019 - can use hypothesis 4.x
                return "hypothesis>=4.0,<5.0"
            elif year == 2020:
                return "hypothesis>=5.0,<6.0"
            elif year == 2021:
                return "hypothesis>=5.0,<6.50"
            elif year >= 2022:
                return "hypothesis>=6.0"
            else:
                return "hypothesis>=3.0,<4.0"
        except (ValueError, IndexError):
            # Fallback to safe default
            return "hypothesis>=3.0,<4.0"

    def _fix_mpl_toolkits_init(self):
        """
        Replace the broken pkg_resources.declare_namespace call in
        lib/mpl_toolkits/__init__.py with the modern pkgutil.extend_path approach.

        Newer setuptools (>= 67) emits a DeprecationWarning for
        pkg_resources.declare_namespace(), which the matplotlib test conftest
        (filterwarnings = error) turns into an error causing ALL tests to ERROR.

        This fix must be re-applied after any 'git checkout .' that resets the file.
        """
        mpl_toolkits_init = self.repo_dir / "lib" / "mpl_toolkits" / "__init__.py"
        if mpl_toolkits_init.exists():
            content = mpl_toolkits_init.read_text()
            if 'pkg_resources' in content and 'declare_namespace' in content:
                mpl_toolkits_init.write_text(
                    "from pkgutil import extend_path\n"
                    "__path__ = extend_path(__path__, __name__)\n"
                )
                print(f"      ✓ Fixed mpl_toolkits/__init__.py (replaced pkg_resources)")

    def install_dependencies(self):
        """Install all dependencies from base_commit environment"""
        print(f"[4/7] Installing dependencies...")

        # 1. Upgrade pip (conservative)
        self.run_in_env(["pip", "install", "--upgrade", "pip<24", "-q"])

        # 2. Get build requirements
        build_reqs = self.get_build_requirements()
        # Astropy before NumPy 1.20 removal of np.rank/np.asscalar
        numpy_constraint = None
        if 'astropy' in self.instance['repo']:
            commit_date = self.get_commit_date()
            if commit_date and commit_date < "2020-01-01":
                numpy_constraint = "numpy<1.18"
                # Prefer conda binary for old NumPy to avoid pip build failures
                env_python = self.get_env_python()
                conda_prefix = env_python.parent.parent
                conda_result = subprocess.run(
                    ["conda", "install", "-p", str(conda_prefix), "-c", "conda-forge", numpy_constraint, "-y", "-q"],
                    capture_output=True, text=True
                )
                if conda_result.returncode != 0:
                    print(f"      ⚠ Warning: conda install {numpy_constraint} failed")
                else:
                    print(f"      → Installed {numpy_constraint} via conda for old astropy")
                # Remove numpy from pip build deps to avoid upgrading it
                build_reqs = [
                    req for req in build_reqs
                    if not req.lower().startswith("numpy") and "oldest-supported-numpy" not in req.lower()
                ]

        # Matplotlib before NumPy 1.19 ragged-array / 1.20 np.float deprecations.
        # Old matplotlib (pre-2021) uses np.float in polar.py; NumPy >=1.20 turns
        # that DeprecationWarning into a test failure via filterwarnings=error.
        # NumPy >=1.19 adds VisibleDeprecationWarning for ragged-sequence ndarrays
        # (e.g. np.array(line2d.get_data()) in test patches), also raised as error.
        # Pin numpy<1.19 for commits predating these releases to keep tests green.
        if 'matplotlib' in self.instance['repo'] and numpy_constraint is None:
            commit_date = self.get_commit_date()
            if commit_date and commit_date < "2021-01-01":
                numpy_constraint = "numpy<1.19"
                env_python = self.get_env_python()
                conda_prefix = env_python.parent.parent
                conda_result = subprocess.run(
                    ["conda", "install", "-p", str(conda_prefix), "-c", "conda-forge", numpy_constraint, "-y", "-q"],
                    capture_output=True, text=True
                )
                if conda_result.returncode != 0:
                    print(f"      ⚠ Warning: conda install {numpy_constraint} failed")
                else:
                    print(f"      → Installed {numpy_constraint} via conda for old matplotlib")
                build_reqs = [
                    req for req in build_reqs
                    if not req.lower().startswith("numpy") and "oldest-supported-numpy" not in req.lower()
                ]

        # 2.5. For matplotlib, install system build dependencies via conda and write mplsetup.cfg
        if 'matplotlib' in self.instance['repo']:
            try:
                env_python = self.get_env_python()
                conda_prefix = env_python.parent.parent
                created_at = self.instance.get('created_at', '')
                year = int(created_at.split('-')[0]) if created_at else 9999

                # Install core system libraries needed by matplotlib's C extensions
                # (freetype, libpng, pkg-config, qhull are required for setuptools-based builds;
                #  meson + ninja are needed for meson-python build backend used since ~2023)
                conda_deps = ["freetype", "libpng", "pkg-config", "qhull", "meson", "ninja"]

                if year < 2019:
                    # Replace freetype with period-appropriate version for old commits
                    conda_deps = [d for d in conda_deps if d != "freetype"]
                    conda_deps.append("freetype=2.9")

                result = subprocess.run(
                    ["conda", "install", "-p", str(conda_prefix), "-c", "conda-forge"]
                    + conda_deps + ["-y", "-q"],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    # Retry without version-pinned freetype
                    conda_deps_fallback = [d for d in conda_deps if d != "freetype=2.9"] + ["freetype"]
                    subprocess.run(
                        ["conda", "install", "-p", str(conda_prefix), "-c", "conda-forge"]
                        + conda_deps_fallback + ["-y", "-q"],
                        capture_output=True
                    )

                # Write mplsetup.cfg to force use of system freetype and qhull
                # (avoids building bundled freetype-2.6.1 which fails with modern clang)
                mplsetup_cfg = self.repo_dir / "mplsetup.cfg"
                mplsetup_cfg.write_text("[libs]\nsystem_freetype = True\nsystem_qhull = True\n")
                # Old matplotlib (<= ~3.5 from 2021) reads 'setup.cfg', not 'mplsetup.cfg'.
                # setupext.py: `setup_cfg = os.environ.get('MPLSETUPCFG') or 'setup.cfg'`
                # Write to setup.cfg as well so system_freetype=True is actually honoured.
                setup_cfg = self.repo_dir / "setup.cfg"
                if not setup_cfg.exists():
                    setup_cfg.write_text("[libs]\nsystem_freetype = True\nsystem_qhull = True\n")
            except:
                pass  # Silently fail - this is a best-effort optimization

        # 3. Install build dependencies from base_commit
        if build_reqs:
            print(f"      → Installing build deps from base_commit")

            # Create constraints file to prevent setuptools upgrades
            constraints_file = self.workspace / "constraints.txt"
            setuptools_constraint = None

            # Extract setuptools constraint if present (only upper-bound constraints)
            for req in build_reqs:
                if req.startswith('setuptools'):
                    # Only use as constraint if it's upper-bound only (e.g., setuptools<60)
                    # Don't use for range constraints (e.g., setuptools>=60,<70)
                    if '<' in req and '>=' not in req:
                        setuptools_constraint = req
                        break

            # Write constraints file
            constraint_lines = []
            if setuptools_constraint:
                constraint_lines.append(setuptools_constraint)
            if numpy_constraint:
                constraint_lines.append(numpy_constraint)
            if constraint_lines:
                constraints_file.write_text("\n".join(constraint_lines) + "\n")

            # If a strict upper-bound setuptools constraint is needed, force-reinstall it
            # first in a separate step. Bundling it with other deps risks silent failure
            # (e.g. conflict resolution skips the downgrade) leaving a newer setuptools
            # in place, which breaks old Cython that imports setuptools.dep_util.
            if setuptools_constraint and '<' in setuptools_constraint and '>=' not in setuptools_constraint:
                print(f"      → Force-installing {setuptools_constraint} before other build deps")
                st_result = self.run_in_env(["pip", "install", "--force-reinstall", setuptools_constraint, "-q"])
                if st_result.returncode != 0:
                    print(f"        ✗ Failed to force-install {setuptools_constraint}: {st_result.stderr[-300:]}")
                    raise RuntimeError("Failed to install required setuptools version")

            # Install all build deps together with constraints
            install_cmd = ["pip", "install"] + build_reqs + ["-q"]
            if constraint_lines:
                install_cmd.extend(["-c", str(constraints_file)])

            result = self.run_in_env(install_cmd)
            if result.returncode != 0:
                print(f"        ⚠ Warning: Some build deps failed to install")

        # 4. Install main package
        print(f"      → Installing main package...")
        env_vars = self.config.get_env_vars()
        install_method = self.config.needs_special_install()

        # For matplotlib, add LDFLAGS so the linker finds conda-installed libs (e.g. libqhull_r)
        # Also set DYLD_LIBRARY_PATH so runtime linker finds conda libs (e.g. libc++.1.dylib
        # installed by conda-forge with @rpath install name — without DYLD_LIBRARY_PATH the
        # meson C++ compiler sanity-check binary can compile but fails to run, and compiled
        # matplotlib extension modules can't load libfreetype/libqhull at import time).
        is_meson_build = False
        if 'matplotlib' in self.instance['repo']:
            try:
                env_python = self.get_env_python()
                conda_lib = str(env_python.parent.parent / "lib")
                existing_ldflags = env_vars.get('LDFLAGS', '')
                env_vars['LDFLAGS'] = f"-L{conda_lib} {existing_ldflags}".strip()
                existing_dyld = env_vars.get('DYLD_LIBRARY_PATH', '')
                env_vars['DYLD_LIBRARY_PATH'] = f"{conda_lib}:{existing_dyld}".rstrip(':')
                # Persist DYLD_LIBRARY_PATH for all subsequent run_in_env calls
                # (import checks, test runs) so compiled extensions find conda libs.
                self._runtime_env_vars['DYLD_LIBRARY_PATH'] = env_vars['DYLD_LIBRARY_PATH']
            except Exception:
                pass

            # Detect meson-python build backend (used by matplotlib since ~2023)
            pyproject = self.repo_dir / "pyproject.toml"
            if pyproject.exists() and 'mesonpy' in pyproject.read_text():
                is_meson_build = True
                self._is_meson_build = True
                # For meson builds, pkg-config needs the conda env's lib/pkgconfig in its
                # search path so that system-freetype/system-qhull dependencies are found.
                # (conda doesn't activate PKG_CONFIG_PATH when we run the env's Python directly)
                try:
                    env_python = self.get_env_python()
                    conda_pkgconfig = str(env_python.parent.parent / "lib" / "pkgconfig")
                    existing_pkgconfig = env_vars.get('PKG_CONFIG_PATH', '')
                    env_vars['PKG_CONFIG_PATH'] = f"{conda_pkgconfig}:{existing_pkgconfig}".rstrip(':')
                except Exception:
                    pass

        # Store build-time env vars for later reinstalls (e.g. in apply_patch non-editable mode)
        self._install_env_vars = dict(env_vars)

        # For old matplotlib (<= ~3.3, pre-2020), patch src/ft2font.cpp before building.
        # Modern Apple clang (Xcode 15+) treats assignment of 'unsigned char *' (FT_Byte*)
        # to 'char *' as a hard C++ error. Fix: change 'char *tags' → 'unsigned char *tags'.
        # This is semantically correct — outline.tags is FT_Byte* (unsigned char*) in freetype.
        if 'matplotlib' in self.instance['repo']:
            ft2font_cpp = self.repo_dir / "src" / "ft2font.cpp"
            try:
                if ft2font_cpp.exists():
                    src = ft2font_cpp.read_text()
                    if 'char *tags;' in src and 'unsigned char *tags;' not in src:
                        patched = src.replace('char *tags;', 'unsigned char *tags;')
                        ft2font_cpp.write_text(patched)
                        print(f"      → Patched ft2font.cpp: char *tags → unsigned char *tags (clang compat)")
            except Exception:
                pass

        if install_method == 'no-build-isolation':
            cmd = ["pip", "install", "-e", ".", "--no-build-isolation"]
        elif install_method == 'ignore-installed':
            cmd = ["pip", "install", "-e", ".", "--ignore-installed"]
        else:
            cmd = ["pip", "install", "-e", "."]

        # For meson-python builds, mplsetup.cfg is ignored; pass options via --config-settings
        if is_meson_build:
            cmd += [
                "--config-settings=setup-args=-Dsystem-freetype=true",
                "--config-settings=setup-args=-Dsystem-qhull=true",
            ]

        # Add constraints file if exists to prevent setuptools upgrades
        constraints_file = self.workspace / "constraints.txt"
        if constraints_file.exists():
            cmd.extend(["-c", str(constraints_file)])

        result = self.run_in_env(cmd, env_vars=env_vars)


        if result.returncode != 0:
            # Fallback: try without editable mode.
            # Preserve --no-build-isolation so the fallback uses the already-configured
            # environment (with pinned setuptools etc.) rather than creating a fresh
            # isolated build env where pip-installed build-backend deps ignore constraints.
            cmd = ["pip", "install", "."]
            if install_method == 'no-build-isolation':
                cmd.append("--no-build-isolation")
            elif install_method == 'ignore-installed':
                cmd.append("--ignore-installed")
            if is_meson_build:
                cmd += [
                    "--config-settings=setup-args=-Dsystem-freetype=true",
                    "--config-settings=setup-args=-Dsystem-qhull=true",
                ]
            if constraints_file.exists():
                cmd.extend(["-c", str(constraints_file)])
            result = self.run_in_env(cmd, env_vars=env_vars)

            if result.returncode != 0:
                print(f"      ✗ Installation failed:")
                print(f"        {result.stderr[-500:]}")
                raise RuntimeError("Failed to install package")

        print(f"      ✓ Main package installed")

        # For matplotlib editable installs, remove the broken *-nspkg.pth file.
        # pip install -e . --no-build-isolation generates a matplotlib-*-nspkg.pth
        # that calls pkgutil.extend_path for mpl_toolkits but fails with
        # "AttributeError: 'NoneType' has no attribute 'loader'" during Python
        # startup, causing ConftestImportFailure -> all tests ERROR.
        # The __editable__.matplotlib-*.pth already adds repo/lib to sys.path,
        # so the nspkg.pth is redundant and safe to remove.
        if 'matplotlib' in self.instance['repo']:
            cleanup_result = self.run_in_env(["python", "-c",
                "import site, glob, os; "
                "[os.remove(f) for d in site.getsitepackages() "
                "for f in glob.glob(os.path.join(d, '*-nspkg.pth')) "
                "if 'matplotlib' in os.path.basename(f) or 'mpl' in os.path.basename(f)]"
            ])
            if cleanup_result.returncode == 0:
                print(f"      ✓ Cleaned up matplotlib nspkg.pth files")

            self._fix_mpl_toolkits_init()

        # 5. Install runtime dependencies (repo-specific as these may not be in base files)
        runtime_deps = self.config.get_runtime_deps()
        if runtime_deps:
            force_runtime_reinstall = False
            if 'astropy' in self.instance['repo']:
                commit_date = self.get_commit_date()
                if commit_date and commit_date < "2020-01-01":
                    # Remove numpy from pip runtime deps to avoid upgrading it
                    runtime_deps = [
                        req for req in runtime_deps
                        if not req.lower().startswith("numpy") and "oldest-supported-numpy" not in req.lower()
                    ]
                    force_runtime_reinstall = True
                # Old astropy (pre-2022) uses nose-style setup(self)/teardown(self) fixtures,
                # which pytest 7+ treats as a hard error (PytestRemovedIn8Warning).
                # Newer astropy requires pytest>=7 (setup.cfg minversion = 7.0).
                if commit_date and commit_date < "2022-01-01":
                    runtime_deps = [req for req in runtime_deps if not req.lower().startswith("pytest")]
                    runtime_deps.append("pytest<7")
            cmd = ["pip", "install"]
            if force_runtime_reinstall:
                cmd += ["--upgrade", "--force-reinstall"]
            cmd += runtime_deps + ["-q"]
            if constraints_file.exists():
                cmd.extend(["-c", str(constraints_file)])
            self.run_in_env(cmd)

        # 5b. Pin jinja2<3.0 for old sphinx commits.
        # Jinja2 3.0 (May 2021) removed `environmentfilter` which sphinx used until
        # ~Sphinx 4.1 (July 2021).  Installing the latest jinja2 on old sphinx
        # commits causes conftest collection to fail with ImportError → 0 tests.
        if 'sphinx-doc/sphinx' in self.instance['repo']:
            commit_date = self.get_commit_date()
            if commit_date and commit_date < '2022-01-01':
                # jinja2 3.0 (May 2021) removed environmentfilter; markupsafe 2.1
                # (Feb 2022) removed soft_unicode that jinja2 2.x relied on.
                # Both must be pinned together for old sphinx commits.
                print(f"      → Pinning jinja2<3.0 + markupsafe<2.1 for old sphinx commit ({commit_date})")
                self.run_in_env(["pip", "install", "jinja2<3.0", "markupsafe<2.1", "-q"])

        # 6. Verify installation
        pkg_name = self.config.get_package_name()
        result = self.run_in_env(["python", "-c", f"import {pkg_name}"], env_vars=env_vars)
        if result.returncode == 0:
            print(f"      ✓ Verified import of '{pkg_name}'")
        else:
            print(f"      ⚠ Warning: Could not import '{pkg_name}'")
            print(f"      → Retrying with non-editable install...")

            # Fallback to non-editable install
            cmd = ["pip", "install", ".", "--force-reinstall", "--no-deps"]
            if install_method == 'ignore-installed':
                cmd.append("--ignore-installed")
            if is_meson_build:
                cmd += [
                    "--config-settings=setup-args=-Dsystem-freetype=true",
                    "--config-settings=setup-args=-Dsystem-qhull=true",
                ]
            if constraints_file.exists():
                cmd.extend(["-c", str(constraints_file)])
            result = self.run_in_env(cmd, env_vars=env_vars)

            if result.returncode != 0:
                print(f"      ✗ Non-editable install failed:")
                print(f"        {result.stderr[-500:]}")
                raise RuntimeError("Failed to install package in non-editable mode")

            # Verify again
            result = self.run_in_env(["python", "-c", f"import {pkg_name}"], env_vars=env_vars)
            if result.returncode == 0:
                self.using_non_editable = True
                print(f"      ✓ Non-editable install successful")
            else:
                print(f"      ✗ Import still fails after non-editable install")
                raise RuntimeError(f"Cannot import {pkg_name}")

        # 7. Install pytest from base_commit or compatible version
        # Skip if the repo IS pytest itself (already installed from source)
        is_pytest_repo = 'pytest-dev/pytest' in self.instance['repo'] or 'pytest' == self.config.get_package_name()
        version_tuple = self.parse_version(self.python_version)

        if not is_pytest_repo:
            pytest_version = self.get_pytest_version()

            # For Python 3.6, install compatible pluggy first
            if version_tuple <= (3, 6):
                cmd = ["pip", "install", "pluggy<1.0", "--force-reinstall", "-q"]
                if constraints_file.exists():
                    cmd.extend(["-c", str(constraints_file)])
                self.run_in_env(cmd)

            # Get compatible hypothesis version based on commit date
            hypothesis_version = self.get_hypothesis_constraint()
            print(f"      → Using hypothesis constraint: {hypothesis_version}")

            # Fix hypothesis < 6.0 incompatibility with importlib_metadata >= 5.0
            # hypothesis 5.x uses .get() method which was removed in importlib_metadata 5.0
            # Only apply this constraint for old hypothesis versions to avoid affecting other repos
            packages_to_install = [pytest_version, hypothesis_version]
            if any(constraint in hypothesis_version for constraint in ["<6.0", "<5.", "<4."]):
                packages_to_install.append("importlib-metadata<5.0")

            cmd = ["pip", "install"] + packages_to_install + ["-q"]
            if constraints_file.exists():
                cmd.extend(["-c", str(constraints_file)])
            self.run_in_env(cmd)

            # Pin pytest upper bound in constraints file so subsequent pip installs
            # (e.g. pytest-cov, pytest-xdist) cannot upgrade pytest beyond the
            # version we just installed.  Without this, pytest-cov<4.0 resolves to
            # 3.x which requires pytest>=5, silently upgrading e.g. pytest 4.6 → 7.x.
            upper_match = re.search(r',(<[^,]+)', pytest_version)
            if upper_match:
                pytest_pin_line = f"pytest{upper_match.group(1)}"
                with open(constraints_file, 'a') as f:
                    f.write(f"\n{pytest_pin_line}\n")
        else:
            print(f"      → Skipping pytest installation (using local pytest from repo)")
            # Still install hypothesis for pytest's own tests
            hypothesis_version = self.get_hypothesis_constraint()
            print(f"      → Using hypothesis constraint: {hypothesis_version}")
            packages_to_install = [hypothesis_version]
            if any(constraint in hypothesis_version for constraint in ["<6.0", "<5.", "<4."]):
                packages_to_install.append("importlib-metadata<5.0")
            cmd = ["pip", "install"] + packages_to_install + ["-q"]
            constraints_file = self.workspace / "constraints.txt"
            if constraints_file.exists():
                cmd.extend(["-c", str(constraints_file)])
            self.run_in_env(cmd)

        # Install pytest plugins for Python 3.7+
        if version_tuple > (3, 6):
            # Check if base_commit specifies plugins
            base_pytest_reqs = self.get_pytest_requirements_from_base_commit()
            plugin_specs = [req for req in base_pytest_reqs
                          if any(plugin in req.lower() for plugin in ['pytest-cov', 'pytest-xdist', 'pytest-timeout'])]

            if not plugin_specs:
                plugin_specs = ["pytest-cov<4.0", "pytest-xdist<3.0"]

            if plugin_specs:
                cmd = ["pip", "install"] + plugin_specs + ["-q"]
                if constraints_file.exists():
                    cmd.extend(["-c", str(constraints_file)])
                self.run_in_env(cmd)

        print(f"      ✓ pytest installed")

        # 8. Install optional packages required by test_patch (e.g. scipy, numexpr)
        optional_deps = self._get_optional_test_deps()
        if optional_deps:
            print(f"      → Installing optional test deps: {optional_deps}")
            self.run_in_env(["pip", "install"] + optional_deps + ["-q"])

        # 9. Install system tools required by test_patch (e.g. graphviz)
        self._install_optional_system_tools()

    def _get_optional_test_deps(self) -> List[str]:
        """Scan test_patch for newly imported packages and return ones to install."""
        # Packages that may appear in tests but are not installed by default.
        # Maps the module name to the pip package name.
        OPTIONAL = {
            'scipy': 'scipy',
            'numexpr': 'numexpr',
            'symengine': 'symengine',
            'appdirs': 'appdirs',
            'dask': 'dask[complete]',
            'distributed': 'distributed',
            'sparse': 'sparse',
            'bottleneck': 'bottleneck',
            'zarr': 'zarr',
            'netCDF4': 'netCDF4',
        }
        test_patch = self.instance.get('test_patch', '')
        # Already-installed packages (the main repo package and its runtime deps)
        already = {self.config.get_package_name()} | set(
            dep.split('<')[0].split('>')[0].split('=')[0].lower()
            for dep in self.config.get_runtime_deps()
        )
        to_install = []
        for mod, pkg in OPTIONAL.items():
            if pkg.lower().split('[')[0] in already:
                continue
            # Match import_module('mod') / import_module("mod")
            if re.search(rf"""import_module\(['"]{re.escape(mod)}""", test_patch):
                to_install.append(pkg)
                continue
            # Match new top-level import lines added by the patch:
            #   +import mod
            #   +from mod import ...
            if re.search(rf'^\+\s*(?:import|from)\s+{re.escape(mod)}(?:\s|$|\.)', test_patch, re.MULTILINE):
                to_install.append(pkg)
                continue
            # Also scan test files modified by the patch for pytest.importorskip("mod")
            # at module level — these cause the entire file to be skipped if not installed.
            test_files_in_patch = re.findall(r'diff --git a/(.*?test[^\s]*\.py) b/', test_patch)
            for test_file_path in test_files_in_patch:
                full_path = self.repo_dir / test_file_path
                if not full_path.exists():
                    continue
                try:
                    file_content = full_path.read_text()
                    if re.search(rf"""pytest\.importorskip\(['"]{re.escape(mod)}['"]""", file_content):
                        to_install.append(pkg)
                        break
                except Exception:
                    pass
        return to_install

    def _install_optional_system_tools(self):
        """Install binary tools (via conda) required by test_patch."""
        test_patch = self.instance.get('test_patch', '')
        patch = self.instance.get('patch', '')
        combined = test_patch + patch

        # graphviz: needed when tests use if_graphviz_found fixture or graphviz extension
        needs_graphviz = bool(
            re.search(r'if_graphviz_found|graphviz_output_format|graphviz_dot', combined)
        )
        if needs_graphviz and not shutil.which('dot'):
            print(f"      → Installing graphviz via conda (required by tests)...")
            env_python = self.get_env_python()
            conda_prefix = env_python.parent.parent
            result = subprocess.run(
                ["conda", "install", "-p", str(conda_prefix), "-c", "conda-forge",
                 "graphviz", "-y", "-q"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"      ⚠ Warning: graphviz install failed")
            else:
                print(f"      ✓ graphviz installed")

    # ========================================================================
    # STEP 5: TEST DISCOVERY
    # ========================================================================

    def extract_test_files(self) -> List[str]:
        """Extract test file paths from test_patch"""
        test_files = []
        patch = self.instance.get('test_patch', '')

        for match in re.finditer(r'diff --git a/(.*?) b/', patch):
            file_path = match.group(1)
            if file_path.endswith('.py') and self._is_test_file(file_path):
                test_files.append(file_path)

        return test_files

    def _is_test_file(self, file_path: str) -> bool:
        """
        Check if a file path is a test file.

        A file is considered a test file if:
        - It's in a test/tests/testing directory, OR
        - Its filename starts with test_ or ends with _test.py

        This avoids false positives like src/_pytest/tmpdir.py
        """
        parts = Path(file_path).parts
        filename = Path(file_path).name

        # Check if in test directory
        test_dirs = {'test', 'tests', 'testing'}
        if any(part in test_dirs for part in parts):
            return True

        # Check if filename matches test pattern
        if filename.startswith('test_') or filename.endswith('_test.py'):
            return True

        return False

    def extract_modified_tests(self) -> set:
        """
        Extract specific test names modified in test_patch
        Returns: {'tests/test_foo.py::TestClass::test_method', ...}
        """
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')

        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            if 'test' in file_path and file_path.endswith('.py'):
                current_test = None
                current_class = None
                current_test_from_hunk = False  # True when current_test came from @@ header
                tests_with_changes = {}

                lines = patch_content.split('\n')
                for idx, line in enumerate(lines):
                    line_content = line[1:] if line and line[0] in ' +-' else line

                    # Track class and test method (only module-level classes)
                    class_match = re.match(r'^class (\w+)', line_content)
                    if class_match:
                        current_class = class_match.group(1)

                    # Reset class context when we hit a module-level def
                    if re.match(r'^def ', line_content):
                        current_class = None

                    test_match = re.search(r'^\s*def (test_\w+)\s*\(', line_content)
                    if test_match:
                        current_test = test_match.group(1)
                        current_test_from_hunk = False

                    # Also detect test name from hunk headers: @@ -N,N +N,N @@ def test_name():
                    # This catches hunks that start mid-function (no def line in context)
                    hunk_match = re.search(r'^@@.*@@\s+def (test_\w+)\s*\(', line)
                    if hunk_match:
                        current_test = hunk_match.group(1)
                        current_test_from_hunk = True

                    # If hunk header context doesn't show a def test_ (e.g. shows a
                    # local class like "class Dummy:"), fall back to scanning the actual
                    # test file backwards from the hunk's start line to find the
                    # enclosing test function (e.g. test_grouper_private in 25352).
                    if line.startswith('@@') and not hunk_match:
                        hunk_orig_match = re.search(r'^@@ -(\d+)', line)
                        if hunk_orig_match and hasattr(self, 'repo_dir') and self.repo_dir.exists():
                            hunk_orig_line = int(hunk_orig_match.group(1))
                            actual_file = self.repo_dir / file_path
                            if actual_file.exists():
                                try:
                                    file_lines = actual_file.read_text(errors='replace').splitlines()
                                    for file_line in reversed(file_lines[:hunk_orig_line]):
                                        fn_match = re.search(r'^\s*def (test_\w+)\s*\(', file_line)
                                        if fn_match:
                                            current_test = fn_match.group(1)
                                            break
                                except (OSError, UnicodeDecodeError):
                                    pass

                    # Check for actual changes
                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line[1:].strip().startswith('#'))

                    if current_test and is_change:
                        # If the changed line is a decorator (e.g. -@XFAIL), it belongs
                        # to the *next* function, not the one from the hunk header.
                        # Look ahead for the first def test_... line to find the real owner.
                        if re.match(r'^[+-]\s*@', line):
                            for ahead in lines[idx + 1:]:
                                ahead_content = ahead[1:] if ahead and ahead[0] in ' +-' else ahead
                                ahead_def = re.search(r'^\s*def (test_\w+)\s*\(', ahead_content)
                                if ahead_def:
                                    tests_with_changes[ahead_def.group(1)] = current_class
                                    break
                        elif current_test_from_hunk and not re.search(r'def (test_\w+)', line_content):
                            # current_test came from the @@ hunk header, not a real def in the
                            # diff. The changed lines (e.g. new parametrize values) may belong
                            # to the *next* def test_ that appears in context lines below.
                            # Look ahead (including context lines) to find it.
                            next_test = None
                            for ahead in lines[idx + 1:]:
                                if ahead.startswith('@@'):
                                    break  # next hunk — stop
                                ahead_content = ahead[1:] if ahead and ahead[0] in ' +-' else ahead
                                ahead_def = re.search(r'^\s*def (test_\w+)\s*\(', ahead_content)
                                if ahead_def:
                                    next_test = ahead_def.group(1)
                                    break
                            tests_with_changes[next_test or current_test] = current_class
                        else:
                            tests_with_changes[current_test] = current_class

                # Build test identifiers
                for test_name, class_name in tests_with_changes.items():
                    if class_name:
                        modified_tests.add(f"{file_path}::{class_name}::{test_name}")
                    else:
                        modified_tests.add(f"{file_path}::{test_name}")

            i += 2

        return modified_tests

    # ========================================================================
    # STEP 6: TEST EXECUTION
    # ========================================================================

    def run_tests(self, test_files: List[str], debug: bool = False) -> Dict[str, str]:
        """
        Run tests and return status map
        Returns: {"test_file.py::test_name": "PASSED"|"FAILED"|"ERROR"}
        """
        if self.config.uses_django_runner():
            return self._run_django_tests(test_files, debug)
        else:
            return self._run_pytest_tests(test_files, debug)

    def _fix_test_paths_for_repo(self, status_map: Dict[str, str]) -> Dict[str, str]:
        """
        Fix test paths for repos that need class names added.

        Some repos use class-based tests where pytest output may omit class names,
        but the proper format requires them. This method adds missing class names
        by inspecting the actual test files.

        Repos that USE class-based tests (need fixing):
        - mwaskom/seaborn: Tests are in classes like TestPolyFit 
        - psf/requests: Tests are in classes like RequestsTestCase 
        - pytest-dev/pytest: Tests are in classes like TestAssertRewrite 
        - pylint-dev/pylint: Tests are in classes like TestFixme
        - pydata/xarray: Tests are in classes like TestVariable, TestDataArray

        Repos that use STANDALONE functions (no fixing needed):
        - pallets/flask: Tests are module-level functions
        - matplotlib/matplotlib: Most tests are module-level functions
        - scikit-learn/scikit-learn: Most tests are module-level functions
        - sympy/sympy: Most tests are module-level functions
        """
        # Define repos that need class name fixing
        REPOS_WITH_CLASS_TESTS = [
            'mwaskom/seaborn',
            'psf/requests',
            'pytest-dev/pytest',
            'pylint-dev/pylint',
            'pydata/xarray',
            'sphinx-doc/sphinx',
        ]

        # Check if this repo needs fixing
        needs_fixing = any(repo in self.instance['repo'] for repo in REPOS_WITH_CLASS_TESTS)
        if not needs_fixing:
            return status_map

        fixed_map = {}

        for test_path, status in status_map.items():
            # Check if path is missing class name (only has file::test_name)
            parts = test_path.split('::')
            if len(parts) == 2:  # file::test_name (missing class)
                file_path, test_name = parts

                # Read the test file to find the class name
                test_file_full = self.repo_dir / file_path
                if test_file_full.exists():
                    try:
                        content = test_file_full.read_text()

                        # Scan line by line to find the most recent module-level class
                        # definition before the target test method.  The old regex
                        # (r'class.*?def test_name', re.DOTALL) was wrong because
                        # its lazy .*? still spans across class boundaries and latches
                        # onto the first class in the file rather than the enclosing one.
                        current_class = None
                        class_name = None
                        for line in content.splitlines():
                            # Module-level class (no leading whitespace)
                            cls_m = re.match(r'^class\s+(\w+)', line)
                            if cls_m:
                                current_class = cls_m.group(1)
                            # Test method definition (indented)
                            def_m = re.match(r'\s+def\s+' + re.escape(test_name) + r'\s*\(', line)
                            if def_m and current_class:
                                class_name = current_class
                                break

                        if class_name:
                            fixed_path = f"{file_path}::{class_name}::{test_name}"
                            fixed_map[fixed_path] = status
                        else:
                            # No class found, keep original (might be standalone function)
                            fixed_map[test_path] = status
                    except Exception as e:
                        # If we can't read/parse the file, keep original
                        fixed_map[test_path] = status
                else:
                    fixed_map[test_path] = status
            else:
                # Already has class name or more complex structure, keep as-is
                fixed_map[test_path] = status

        return fixed_map

    def _run_pytest_tests(self, test_files: List[str], debug: bool) -> Dict[str, str]:
        """Run tests using pytest"""
        status_map = {}

        # For pytest repo, temporarily patch pyproject.toml and tox.ini to remove
        # minversion check since dev version is 0.1.dev which is < 2.0 requirement
        # (setuptools-scm can't find the git tag in a shallow clone)
        is_pytest_repo = 'pytest-dev/pytest' in self.instance['repo']
        pyproject_backup = None
        toxini_backup = None
        if is_pytest_repo:
            pyproject_path = self.repo_dir / "pyproject.toml"
            if pyproject_path.exists():
                pyproject_backup = pyproject_path.read_text()
                content = pyproject_backup.replace('minversion = "2.0"', 'minversion = "0"')
                content = content.replace("minversion = '2.0'", "minversion = '0'")
                pyproject_path.write_text(content)
            # tox.ini may also have [pytest] minversion that blocks test collection
            toxini_path = self.repo_dir / "tox.ini"
            if toxini_path.exists():
                toxini_backup = toxini_path.read_text()
                content = re.sub(r'(?m)^(\s*minversion\s*=\s*)[\d.]+', r'\g<1>0', toxini_backup)
                toxini_path.write_text(content)

        base_cmd = ["python", "-m", "pytest"]

        # For matplotlib repos, suppress DeprecationWarnings from third-party libraries
        # (pyparsing, pkg_resources/setuptools) that the conftest filterwarnings=error
        # would otherwise turn into errors, causing all tests to ERROR via conftest setup.
        # Specifically:
        # - pyparsing >=3.0 deprecated enablePackrat() -> PyparsingDeprecationWarning
        # - setuptools deprecated pkg_resources API -> DeprecationWarning (from mpl_toolkits/__init__)
        # Both come from mpl internals calling these APIs at import time.
        if 'matplotlib' in self.instance['repo']:
            # Suppress third-party deprecation warnings that the conftest
            # filterwarnings=error turns into errors.
            # - pyparsing>=3.0: PyparsingDeprecationWarning (not a DeprecationWarning
            #   subclass) from pyparsing.util when enablePackrat() is called.
            #   Only add the filter if pyparsing.warnings exists (it was added in
            #   pyparsing 3.x; older versions don't have this submodule).
            # - setuptools>=67: pkg_resources is deprecated as an API
            check_pyparsing = self.run_in_env(
                ["python", "-c", "import pyparsing.warnings"])
            if check_pyparsing.returncode == 0:
                base_cmd += ["-W", "ignore::pyparsing.warnings.PyparsingDeprecationWarning"]
            base_cmd += ["-W", "ignore::DeprecationWarning:pkg_resources"]

        # Use per-test timeout when running specific test IDs to avoid infinite loops
        # in tests that expose bugs where the pre-fix code hangs (e.g., infinite recursion)
        is_specific = any('::' in f for f in test_files)
        per_test_timeout = 120 if is_specific else None  # 2 min per specific test

        try:
            for test_file in test_files:
                # Handle both "file.py" and "file.py::test_name" style IDs
                file_part = test_file.split('::')[0]
                test_path = self.repo_dir / file_part
                if not test_path.exists():
                    continue

                # Use relative path to avoid conftest plugin conflicts and disable color for parsing
                result = self.run_in_env(
                    base_cmd + [test_file, "-v", "--tb=short", "--no-header", "--color=no"],
                    timeout=per_test_timeout
                )

                # Retry without --no-header if not supported
                if result.returncode != 0 and "--no-header" in result.stderr:
                    result = self.run_in_env(
                        base_cmd + [test_file, "-v", "--tb=short", "--color=no"],
                        timeout=per_test_timeout
                    )

                # Retry with addopts cleared if pyproject.toml addopts contains
                # plugin-specific flags that aren't installed (e.g. --mypy-*)
                if result.returncode == 4 and "unrecognized arguments" in result.stderr:
                    result = self.run_in_env(
                        base_cmd + [test_file, "-v", "--tb=short", "--no-header", "--color=no",
                                    "-o", "addopts="],
                        timeout=per_test_timeout
                    )
                    if result.returncode != 0 and "--no-header" in result.stderr:
                        result = self.run_in_env(
                            base_cmd + [test_file, "-v", "--tb=short", "--color=no",
                                        "-o", "addopts="],
                            timeout=per_test_timeout
                        )

                # Debug output
                if debug and result.returncode != 0:
                    print(f"      [DEBUG] {test_file}: exit code {result.returncode}")
                    print(f"      [DEBUG] stdout:\n{result.stdout[-1000:]}")
                    print(f"      [DEBUG] stderr:\n{result.stderr[-1000:]}")

                # Parse output
                initial_count = len(status_map)

                # Repos with class-based tests that need class names preserved
                repos_with_classes = ['mwaskom/seaborn', 'psf/requests', 'pytest-dev/pytest', 'pylint-dev/pylint', 'pydata/xarray', 'sphinx-doc/sphinx']
                preserve_class_names = any(repo in self.instance['repo'] for repo in repos_with_classes)

                for line in result.stdout.split('\n'):
                    # Match test output including parametrized tests like test_name[param1-param2]
                    match = re.search(r'(\S+?)::(test_\w+(?:\[.*?\])?)\s+(PASSED|FAILED|ERROR|SKIPPED)', line)
                    if match:
                        # For repos with class-based tests, preserve class names from pytest output
                        if preserve_class_names:
                            # Keep full path from pytest output to preserve class names
                            full_test_name = f"{match.group(1)}::{match.group(2)}"
                        else:
                            normalized_path = test_file if test_file in match.group(1) else match.group(1)
                            full_test_name = f"{normalized_path}::{match.group(2)}"
                        status_map[full_test_name] = match.group(3)

        finally:
            # Restore pyproject.toml and tox.ini if we modified them
            if pyproject_backup is not None:
                pyproject_path = self.repo_dir / "pyproject.toml"
                pyproject_path.write_text(pyproject_backup)
            if toxini_backup is not None:
                toxini_path = self.repo_dir / "tox.ini"
                toxini_path.write_text(toxini_backup)

        # Fix test paths for specific repos (e.g., add missing class names)
        status_map = self._fix_test_paths_for_repo(status_map)

        return status_map

    def _run_django_tests(self, test_files: List[str], debug: bool) -> Dict[str, str]:
        """Run tests using Django's test runner"""
        status_map = {}
        runtests_path = self.repo_dir / "tests" / "runtests.py"

        if not runtests_path.exists():
            return status_map

        for test_file in test_files:
            # Convert path to Django module format
            # tests/migrations/test_commands.py -> migrations.test_commands
            parts = Path(test_file).parts
            if len(parts) >= 3 and parts[0] == "tests" and test_file.endswith('.py'):
                module_path = '.'.join(parts[1:-1] + (parts[-1][:-3],))
            else:
                continue

            # Run Django tests
            result = self.run_in_env(
                ["python", str(runtests_path), module_path, "-v", "2", "--parallel=1"],
                env_vars={"PYTHONPATH": str(self.repo_dir)}
            )

            # Fallback without --parallel for older Django
            if result.returncode == 2 and "--parallel" in result.stderr:
                result = self.run_in_env(
                    ["python", str(runtests_path), module_path, "-v", "2"],
                    env_vars={"PYTHONPATH": str(self.repo_dir)}
                )

            # Parse Django output
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
                        module_path = '/'.join(parts[:-2])
                        file_part = parts[-2]
                        test_file_path = f"tests/{module_path}/{file_part}.py"
                        full_test_name = f"{test_file_path}::{test_class}::{test_name}"
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
                            module_path = '/'.join(parts[:-2])
                            file_part = parts[-2]
                            test_file_path = f"tests/{module_path}/{file_part}.py"
                            full_test_name = f"{test_file_path}::{test_class}::{test_name}"
                            status_map[full_test_name] = status
                        pending_test = None

        return status_map

    # ========================================================================
    # STEP 7: PATCH APPLICATION & RESULT COMPARISON
    # ========================================================================

    def apply_patch(self, patch_content: str, patch_name: str):
        """Apply a patch to the repository"""
        if not patch_content or not patch_content.strip():
            return

        def _strip_binary_diffs(patch_text: str) -> str:
            parts = patch_text.split("diff --git ")
            if len(parts) == 1:
                return patch_text
            kept = [parts[0]]
            for part in parts[1:]:
                diff_block = "diff --git " + part
                # "Binary files ... differ" is a stub (no content) — skip it.
                # "GIT binary patch" contains actual base85-encoded data — keep it.
                if "Binary files" in diff_block and "GIT binary patch" not in diff_block:
                    continue
                kept.append(diff_block)
            return "".join(kept)

        patch_content = _strip_binary_diffs(patch_content)
        if not patch_content or not patch_content.strip():
            print(f"      ⚠ Skipping {patch_name} patch (only binary diffs)")
            return

        patch_file = self.workspace / f"{patch_name}.patch"
        patch_file.write_text(patch_content)

        result = subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(f"Patch failed: {patch_name}\n{result.stderr}")

        # If using non-editable install, reinstall to pick up changes
        if self.using_non_editable:
            constraints_file = self.workspace / "constraints.txt"
            env_vars = self.config.get_env_vars()
            install_method = self.config.needs_special_install()

            cmd = ["pip", "install", ".", "--force-reinstall", "--no-deps", "-q"]
            if install_method == 'ignore-installed':
                cmd.append("--ignore-installed")
            if self._is_meson_build:
                cmd += [
                    "--config-settings=setup-args=-Dsystem-freetype=true",
                    "--config-settings=setup-args=-Dsystem-qhull=true",
                ]
            if constraints_file.exists():
                cmd.extend(["-c", str(constraints_file)])

            self.run_in_env(cmd, env_vars=self._install_env_vars or env_vars)

    def compare_results(
        self,
        before: Dict[str, str],
        after: Dict[str, str],
        filter_to_modified: set = None,
    ) -> Tuple[List[str], List[str]]:
        """
        Compare test results before and after patch
        Returns: (FAIL_TO_PASS, PASS_TO_PASS)

        Note: New tests (not in before) that pass are considered FAIL_TO_PASS
        """
        fail_to_pass = []
        pass_to_pass = []

        all_tests = set(before.keys()) | set(after.keys())

        for test in sorted(all_tests):
            before_status = before.get(test)
            after_status = after.get(test)

            if after_status == 'PASSED':
                if before_status is None or before_status in ('FAILED', 'ERROR'):
                    # Test was failing/missing and now passes
                    if filter_to_modified is None or self._test_in_filter(test, filter_to_modified):
                        fail_to_pass.append(test)
                else:
                    # Test was passing and still passes
                    pass_to_pass.append(test)
            elif after_status == 'SKIPPED' and before_status is None:
                # Test is newly introduced by test_patch (not present in before results)
                # but SKIPPED due to environment limitations (e.g. Oracle-specific tests
                # decorated with @skipUnlessDBFeature). If the test is in filter_set it
                # was explicitly added alongside the fix; on the target platform it would
                # fail without the fix and pass with it, so treat it as FAIL_TO_PASS.
                if filter_to_modified is not None and self._test_in_filter(test, filter_to_modified):
                    fail_to_pass.append(test)

        return fail_to_pass, pass_to_pass

    def _test_in_filter(self, test_name: str, filter_set: set) -> bool:
        """Check if test matches any pattern in filter set"""
        if test_name in filter_set:
            return True

        # Strip parametrize suffix [params] for base-name comparison
        test_base = re.sub(r'\[.*\]$', '', test_name.split('::')[-1])
        test_file = test_name.split('::')[0]

        for pattern in filter_set:
            pattern_base = re.sub(r'\[.*\]$', '', pattern.split('::')[-1])
            pattern_file = pattern.split('::')[0]
            # Match same file and same base test name (handles parametrized variants
            # and class-name differences between filter and result keys)
            if pattern_base == test_base and pattern_file == test_file:
                return True

        return False

    # ========================================================================
    # MAIN VALIDATION WORKFLOW
    # ========================================================================

    def validate(self) -> Tuple[List[str], List[str]]:
        """Run full validation workflow"""
        # Steps 1-4: Setup
        self.setup_repo()
        self.python_version = self.detect_python_version()
        self.create_environment(self.python_version)
        self.install_dependencies()

        # Step 4.5: Apply infrastructure fixes (if needed)
        commit_date = self.get_commit_date()
        infrastructure_fixes = self.config.get_infrastructure_fixes(commit_date)
        if infrastructure_fixes:
            print(f"[4.5/7] Applying {len(infrastructure_fixes)} infrastructure fix(es)...")
            for description, patch_content in infrastructure_fixes:
                try:
                    print(f"      → {description}")
                    self.apply_patch(patch_content, f"infra_fix_{len(infrastructure_fixes)}")
                except RuntimeError as e:
                    # Infrastructure fix failed - might already be applied or not needed
                    # This is non-fatal, continue with validation
                    print(f"      ⚠ Fix not needed or already applied")

        # Step 5: Find tests
        test_files = self.extract_test_files()
        print(f"[5/7] Found {len(test_files)} test file(s)")
        for tf in test_files:
            print(f"      - {tf}")

        # Step 6-7: Run tests before/after patches
        print(f"[6/7] Running tests...")

        # Determine patch strategy
        test_patch = self.instance.get('test_patch', '')
        solution_patch = self.instance.get('patch', '')

        # Check if test_patch contains both tests and fixes
        # Extract all modified .py files and check if any are non-test files (fixes)
        has_fix_in_test_patch = False
        for match in re.finditer(r'diff --git a/(.*?) b/', test_patch):
            file_path = match.group(1)
            if file_path.endswith('.py') and not self._is_test_file(file_path):
                has_fix_in_test_patch = True
                break

        if has_fix_in_test_patch:
            # test_patch contains both test and fix - split them
            print(f"      → test_patch contains fix, splitting...")
            test_only, fix_only = self._split_test_patch(test_patch)

            if test_only:
                self.apply_patch(test_only, "test_only")
            results_before = self.run_tests(test_files, debug=False)

            if fix_only:
                self.apply_patch(fix_only, "fix")
            # Also apply solution_patch — the main fix lives there, not just in fix_only
            if solution_patch:
                try:
                    self.apply_patch(solution_patch, "solution")
                except RuntimeError:
                    pass  # may already be partially applied if fix_only overlaps
            results_after = self.run_tests(test_files, debug=False)

            filter_set = None  # No filtering needed
        else:
            # Standard case: test_patch has tests, solution_patch has fix

            # Use specific test IDs from test_patch when available (much faster than full files)
            specific_test_ids = list(self.extract_modified_tests())
            if specific_test_ids:
                print(f"      → Fast path: running {len(specific_test_ids)} specific test(s) from test_patch")
                for t in specific_test_ids:
                    print(f"        {t}")
                run_targets = specific_test_ids
            else:
                print(f"      → No specific tests found, running full test files")
                run_targets = test_files

            # Check if test_patch breaks test collection
            print(f"      → Checking test_patch compatibility...")
            baseline = self.run_tests(run_targets, debug=False)

            self.apply_patch(test_patch, "test_patch")
            with_test_patch = self.run_tests(run_targets, debug=False)

            if specific_test_ids:
                # With specific IDs: collection_broken if test_patch applied but 0 tests collected
                collection_broken = len(with_test_patch) == 0
            else:
                collection_broken = len(with_test_patch) < len(baseline) * 0.5

            if collection_broken:
                # test_patch needs fix first - apply solution_patch before test_patch
                print(f"      ⚠ test_patch incompatible with base_commit")
                print(f"      → Using fix-first strategy")

                # Reset and apply fix first
                subprocess.run(["git", "checkout", "."], cwd=self.repo_dir, capture_output=True)
                subprocess.run(["git", "clean", "-fd"], cwd=self.repo_dir, capture_output=True)

                # Re-apply repo workarounds that git checkout . may have undone
                if 'matplotlib' in self.instance['repo']:
                    self._fix_mpl_toolkits_init()

                # Re-apply infrastructure fixes that git checkout . may have undone
                for _, patch_content in infrastructure_fixes:
                    try:
                        self.apply_patch(patch_content, "infra_reapply")
                    except RuntimeError:
                        pass  # Already applied or not applicable

                self.apply_patch(solution_patch, "solution")
                self.apply_patch(test_patch, "test_patch")

                # Resolve missing class names in run_targets now that test files are patched
                run_target_dict = {path: "TARGET" for path in run_targets}
                fixed_target_dict = self._fix_test_paths_for_repo(run_target_dict)
                run_targets = list(fixed_target_dict.keys())

                # BEFORE: revert source changes temporarily, run full test files
                patch_file = self.workspace / "solution_reverse.patch"
                patch_file.write_text(solution_patch)
                subprocess.run(["git", "apply", "-R", str(patch_file)],
                             cwd=self.repo_dir, capture_output=True)

                results_before = self.run_tests(test_files, debug=False)

                # AFTER: re-apply source changes, run full test files
                self.apply_patch(solution_patch, "solution")
                results_after = self.run_tests(test_files, debug=False)

                # Filter to modified tests only
                filter_set = self.extract_modified_tests()
                print(f"      → Filtering to {len(filter_set)} modified tests")

                # Fix filter paths for repos with class-based tests
                filter_dict = {path: "FILTER" for path in filter_set}
                fixed_filter_dict = self._fix_test_paths_for_repo(filter_dict)
                filter_set = set(fixed_filter_dict.keys())
            else:
                # Standard path
                print(f"      → Using standard strategy")
                results_before = with_test_patch

                self.apply_patch(solution_patch, "solution")

                if specific_test_ids:
                    # Run full test files after fix to capture PASS_TO_PASS
                    print(f"      → Running full test files after fix (for PASS_TO_PASS)...")
                    results_after = self.run_tests(test_files, debug=False)

                    # Augment results_before: tests not in specific_test_ids are assumed
                    # to have been passing — this prevents them from being misclassified
                    # as FAIL_TO_PASS in compare_results (before=None + after=PASSED).
                    specific_test_set = set(specific_test_ids)
                    for test in results_after:
                        if test not in specific_test_set and test not in results_before:
                            results_before[test] = "PASSED"
                else:
                    results_after = self.run_tests(run_targets, debug=False)

                filter_set = None

        print(f"      ✓ Tests completed ({len(results_before)} before, {len(results_after)} after)")

        # Step 7: Compare results
        print(f"[7/7] Comparing results...")
        fail_to_pass, pass_to_pass = self.compare_results(
            results_before, results_after, filter_set
        )

        print(f"      ✓ FAIL_TO_PASS: {len(fail_to_pass)} tests")
        for test in fail_to_pass:
            print(f"        - {test}")
        print(f"      ✓ PASS_TO_PASS: {len(pass_to_pass)} tests")

        return fail_to_pass, pass_to_pass

    def _split_test_patch(self, test_patch: str) -> Tuple[str, str]:
        """Split patch into test-only and fix-only parts"""
        test_hunks = []
        fix_hunks = []

        parts = re.split(r'(diff --git a/[^\n]+\n)', test_patch)

        i = 1
        while i < len(parts) - 1:
            header = parts[i]
            content = parts[i + 1]
            full_patch = header + content

            # Extract file path from header: "diff --git a/path/to/file.py b/..."
            file_match = re.search(r'diff --git a/(.*?) b/', header)
            if file_match:
                file_path = file_match.group(1)
                if self._is_test_file(file_path):
                    test_hunks.append(full_patch)
                else:
                    fix_hunks.append(full_patch)
            else:
                # Fallback to old logic if regex doesn't match
                if 'testing/' in header or '/test_' in header:
                    test_hunks.append(full_patch)
                else:
                    fix_hunks.append(full_patch)

            i += 2

        return ''.join(test_hunks), ''.join(fix_hunks)

    def cleanup(self):
        """Remove conda environment"""
        if not self.keep_env:
            print(f"\n[Cleanup] Removing environment '{self.env_name}'...")
            subprocess.run(
                ["conda", "env", "remove", "-n", self.env_name, "-y"],
                capture_output=True
            )
            print(f"      ✓ Environment removed")


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def validate_instance(instance_path: str, output_path: Optional[str] = None, keep_env: bool = False):
    """Main validation function"""
    # Generate output path
    if output_path is None:
        input_path = Path(instance_path)
        output_filename = input_path.name.replace('_part1.json', '_part2.json')
        if output_filename == input_path.name:
            output_filename = input_path.stem + '_part2.json'
        output_path = str(input_path.parent / output_filename)

    # Load instance
    with open(instance_path) as f:
        data = json.load(f)
        instance = data[0] if isinstance(data, list) else data

    # Run validation
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        validator = EnvironmentValidator(instance, workspace, keep_env)

        try:
            fail_to_pass, pass_to_pass = validator.validate()

            # Save results
            instance['FAIL_TO_PASS'] = fail_to_pass
            instance['PASS_TO_PASS'] = pass_to_pass
            instance['environment_setup_commit'] = instance['base_commit']

            with open(output_path, 'w') as f:
                json.dump(instance, f, indent=2)

            print(f"\n✓ Validation complete!")
            print(f"  Output: {output_path}")
            print(f"  FAIL_TO_PASS: {len(fail_to_pass)}, PASS_TO_PASS: {len(pass_to_pass)}")

        finally:
            validator.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench instance validation with environment management"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep conda environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
