#!/usr/bin/env python3
"""
SWE-bench Multilingual Instance Validation Script - C language only

Supports: C
WITH PROPER VERSION DETECTION AND MANAGEMENT

Usage:
    python3 full_validation_multilingual_c.py instance.json [--output validated.json]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
import platform


# ============================================================================
# VERSION DETECTION UTILITIES
# ============================================================================

class VersionDetector:
    """Detect required language versions from project files and dates"""

    @staticmethod
    def parse_version(version_str: str) -> Tuple[int, ...]:
        """Parse version string '3.10' -> (3, 10)"""
        try:
            return tuple(int(x) for x in version_str.split('.'))
        except (ValueError, AttributeError):
            return (0,)

    @staticmethod
    def get_version_from_date(created_at: str, language: str) -> str:
        """Determine appropriate version based on creation date"""
        try:
            year = int(created_at.split('-')[0])
            month = int(created_at.split('-')[1])
        except (ValueError, IndexError):
            year = 2020  # Default fallback

        if language == "python":
            # Python version timeline
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

        elif language == "java":
            # Java version timeline
            if year < 2018:
                return "8"
            elif year < 2021:
                return "11"
            elif year < 2024:
                return "17"
            else:
                return "21"

        elif language == "rust":
            # Rust is usually compatible, but use stable for date
            if year < 2019:
                return "1.30"
            elif year < 2021:
                return "1.50"
            elif year < 2023:
                return "1.60"
            else:
                return "1.70"

        elif language == "go":
            # Go version timeline
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

        elif language == "javascript":
            # Node.js LTS versions
            if year < 2019:
                return "10"
            elif year < 2021:
                return "12"
            elif year < 2022:
                return "14"
            elif year < 2023:
                return "16"
            elif year < 2024:
                return "18"
            else:
                return "20"

        elif language == "php":
            # PHP version timeline
            if year < 2019:
                return "7.2"
            elif year < 2021:
                return "7.4"
            elif year < 2023:
                return "8.0"
            else:
                return "8.1"

        elif language == "ruby":
            # Ruby version timeline
            if year < 2019:
                return "2.5"
            elif year < 2021:
                return "2.6"
            elif year < 2022:
                return "2.7"
            elif year < 2023:
                return "3.0"
            else:
                return "3.1"

        return "latest"


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

class LanguageDetector:
    """Detect programming language from repository structure"""

    @staticmethod
    def detect(repo_dir: Path) -> str:
        """
        Detect language from repository files.
        Returns: 'rust', 'go', 'java', 'javascript', 'php', 'ruby', 'c', 'python'
        """
        # Check in priority order (most specific first)

        # Rust - Cargo.toml is definitive
        if (repo_dir / "Cargo.toml").exists():
            return "rust"

        # Go - go.mod is definitive
        if (repo_dir / "go.mod").exists():
            return "go"

        # Java - pom.xml, build.gradle, or build.xml (Apache Ant)
        if (repo_dir / "pom.xml").exists() or \
           (repo_dir / "build.gradle").exists() or \
           (repo_dir / "build.gradle.kts").exists() or \
           (repo_dir / "build.xml").exists():
            return "java"

        # Java - fallback: any .java source files in src/
        if list(repo_dir.glob("src/**/*.java")) or list(repo_dir.glob("**/src/**/*.java")):
            return "java"

        # JavaScript/TypeScript - package.json
        if (repo_dir / "package.json").exists():
            return "javascript"

        # PHP - composer.json
        if (repo_dir / "composer.json").exists():
            return "php"

        # Ruby - Gemfile
        if (repo_dir / "Gemfile").exists():
            return "ruby"

        # Python - setup.py, pyproject.toml, or requirements.txt
        if (repo_dir / "setup.py").exists() or \
           (repo_dir / "pyproject.toml").exists() or \
           (repo_dir / "requirements.txt").exists():
            return "python"

        # C - Makefile/Autotools and .c files
        has_build_system = (
            (repo_dir / "Makefile").exists() or
            (repo_dir / "configure.ac").exists() or
            (repo_dir / "Makefile.am").exists() or
            (repo_dir / "CMakeLists.txt").exists()
        )
        if has_build_system:
            c_files = list(repo_dir.rglob("*.c"))
            if c_files:
                return "c"

        return "unknown"


# ============================================================================
# BASE VALIDATOR CLASS
# ============================================================================

class BaseValidator:
    """Base class for language-specific validators"""

    def __init__(self, instance: dict, workspace: Path, repo_dir: Path, parent=None):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = repo_dir
        self.env_vars = os.environ.copy()

        # Set CGO_ENABLED=0 to avoid dynamic linking issues (dyld errors on macOS)
        # This creates statically linked binaries that don't depend on system libraries
        self.env_vars['CGO_ENABLED'] = '0'

        self.detected_version = None
        self.actual_version = None
        self.parent = parent  # Reference to MultilingualValidator for run_in_env
        self.setup_failed = False  # Track if critical setup steps failed

    def detect_required_version(self) -> str:
        """Detect required language version from project files"""
        raise NotImplementedError

    def get_actual_version(self) -> str:
        """Get currently installed version"""
        raise NotImplementedError

    def setup_version(self, required_version: str):
        """Setup/install the required version"""
        raise NotImplementedError

    def setup_environment(self):
        """Setup language-specific environment with version detection"""
        print(f"[3/7] Setting up environment...")

        # Detect required version
        required_version = self.detect_required_version()
        self.detected_version = required_version
        print(f"      → Required version: {required_version}")

        # Try to setup the version (language-specific)
        try:
            self.setup_version(required_version)
        except Exception as e:
            print(f"      ⚠ Version setup failed: {e}")

        # Get actual version after setup
        actual_version = self.get_actual_version()
        self.actual_version = actual_version
        print(f"      → Actual version: {actual_version}")

        # Warn if mismatch but continue (non-fatal)
        if not self.is_version_compatible(required_version, actual_version):
            print(f"      ⚠ Version mismatch (required: {required_version}, actual: {actual_version})")
            print(f"      → Continuing anyway - tests may fail due to version incompatibility")

        print(f"      ✓ Environment ready")

    def is_version_compatible(self, required: str, actual: str) -> bool:
        """Check if actual version is compatible with required version"""
        if required == "latest" or actual == "latest":
            return True

        try:
            req_parts = [int(x) for x in required.split('.')]
            act_parts = [int(x) for x in actual.split('.')]

            # Major version must match for most languages
            if req_parts[0] != act_parts[0]:
                return False

            # Minor version should be >= required (but allow some flexibility)
            if len(req_parts) > 1 and len(act_parts) > 1:
                if act_parts[1] < req_parts[1] - 2:  # Allow 2 minor versions back
                    return False

            return True
        except (ValueError, IndexError):
            return True  # If we can't parse, assume compatible

    def install_dependencies(self):
        """Install project dependencies"""
        raise NotImplementedError

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run tests and return status map

        Args:
            test_files: Optional list of test file paths to run. If None, runs all tests.
            debug: Enable debug output
            accept_snapshots: For snapshot testing (e.g., Rust insta), auto-accept new snapshots
        """
        raise NotImplementedError

    def extract_test_files_from_patch(self, patch: str) -> List[str]:
        """Extract test file paths from patch"""
        test_files = []
        for match in re.finditer(r'diff --git a/(.*?) b/', patch):
            file_path = match.group(1)
            if self.is_test_file(file_path):
                test_files.append(file_path)
        return list(set(test_files))

    def is_test_file(self, file_path: str) -> bool:
        """Check if file is a test file"""
        raise NotImplementedError

    def _rebuild_assets(self):
        """Try to rebuild binary assets using common patterns"""
        # Language-specific rebuild commands
        rebuild_commands = []

        # Rust: cargo build with build-assets feature
        if (self.repo_dir / "Cargo.toml").exists():
            rebuild_commands.append(["cargo", "build", "--features", "build-assets"])

        # Node.js: npm run build-assets
        if (self.repo_dir / "package.json").exists():
            rebuild_commands.append(["npm", "run", "build-assets"])
            rebuild_commands.append(["npm", "run", "build:assets"])

        # Make: make assets
        if (self.repo_dir / "Makefile").exists():
            rebuild_commands.append(["make", "assets"])

        for cmd in rebuild_commands:
            try:
                result = self.run_command(cmd, timeout=300)
                if result.returncode == 0:
                    print(f"      ✓ Assets rebuilt using: {' '.join(cmd)}")
                    return
            except:
                continue

        print(f"      ⚠ Could not rebuild assets automatically")

    def apply_patch(self, patch_content: str, patch_name: str):
        """Apply a git patch with multiple fallback strategies"""
        if not patch_content or not patch_content.strip():
            return

        patch_file = self.workspace / f"{patch_name}.patch"
        patch_file.write_text(patch_content)

        # Strategy 1: Standard git apply
        result = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            return  # Success!

        # Check if failure is due to binary patches
        if "binary patch" in result.stderr and "without full index" in result.stderr:
            print(f"      ⚠ Binary files in patch cannot be applied (missing full index)")

            # Try to fetch new binary files from GitHub using blob SHA from the patch
            self._fetch_binary_files_from_patch(patch_content)

            # Check if binary files are in assets/ directory (common pattern)
            if "assets/" in result.stderr or "/assets/" in patch_content:
                print(f"      → Binary assets detected - attempting to rebuild...")
                self._rebuild_assets()
            else:
                print(f"      → Attempting to apply non-binary changes only...")
                # Try with --reject to apply what we can
                result = subprocess.run(
                    ["git", "apply", "--reject", "--whitespace=fix", str(patch_file)],
                    cwd=self.repo_dir,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    print(f"      ⚠ Could not apply some changes: {result.stderr[:200]}")
                else:
                    print(f"      ✓ Applied non-binary changes successfully")
            return

        # Strategy 2: Try three-way merge
        print(f"      ⚠ Standard apply failed, trying three-way merge...")
        result = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            print(f"      ✓ Applied using three-way merge")
            return

        # Strategy 3: Try with reject (apply what we can)
        print(f"      ⚠ Three-way merge failed, trying partial application...")
        result = subprocess.run(
            ["git", "apply", "--reject", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        # Check if any .rej files were created
        rej_files = list(self.repo_dir.rglob("*.rej"))
        if rej_files:
            print(f"      ⚠ Partial application - {len(rej_files)} conflict(s) in .rej files")
            # Don't fail completely - some changes may have been applied
            return
        elif result.returncode == 0:
            print(f"      ✓ Applied with --reject")
            return

        # Strategy 4: Try standard patch command with fuzz factor
        print(f"      ⚠ Git apply failed, trying patch command with fuzz...")
        result = subprocess.run(
            ["patch", "-p1", "--fuzz=3", "-i", str(patch_file)],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        if result.returncode == 0 or "succeeded" in result.stdout.lower():
            print(f"      ✓ Applied using patch command with fuzz")
            return

        # All strategies failed
        raise RuntimeError(f"Failed to apply {patch_name}: {result.stderr}")

    def _fetch_binary_files_from_patch(self, patch_content: str) -> bool:
        """
        For new binary files in a patch that lack full index data and can't be applied
        via git apply, fetch them from the remote repo's default branch (the PR is merged
        so the file exists on main/master).

        Patch pattern for a new binary file:
            diff --git a/path/to/file b/path/to/file
            new file mode 100644
            index 0000000000..e63da0a32f
            Binary files /dev/null and b/path/to/file differ

        Returns True if at least one file was successfully fetched.
        """
        # Match new binary file blocks: capture filepath
        pattern = re.compile(
            r'diff --git a/(.+?) b/\1\nnew file mode[^\n]*\nindex 0+\.\.[0-9a-f]+\nBinary files /dev/null'
        )

        new_binary_files = [m.group(1) for m in pattern.finditer(patch_content)]
        if not new_binary_files:
            return False

        missing = [f for f in new_binary_files if not (self.repo_dir / f).exists()]
        if not missing:
            return True  # all already present

        print(f"      → Fetching {len(missing)} binary file(s) from remote...")

        # Fetch the default branch so we can checkout the file(s)
        fetched_any = False
        for branch in ["main", "master"]:
            fetch_result = subprocess.run(
                ["git", "fetch", "--depth=1", "origin", branch],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=60
            )
            if fetch_result.returncode != 0:
                continue

            for file_path in missing:
                target = self.repo_dir / file_path
                if target.exists():
                    continue
                co = subprocess.run(
                    ["git", "checkout", f"origin/{branch}", "--", file_path],
                    cwd=self.repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if co.returncode == 0:
                    print(f"      ✓ Fetched binary file from origin/{branch}: {file_path}")
                    fetched_any = True
                else:
                    print(f"      ⚠ Could not checkout {file_path} from origin/{branch}")
            break  # stop after first successful branch fetch

        return fetched_any

    def run_command(self, cmd: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with environment (uses isolated conda env if available)"""
        # Use parent's run_in_env if available (isolated conda environment)
        if self.parent and hasattr(self.parent, 'run_in_env'):
            return self.parent.run_in_env(cmd, cwd=cwd or self.repo_dir, timeout=timeout)

        # Fallback to direct execution
        try:
            return subprocess.run(
                cmd,
                cwd=cwd or self.repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.env_vars
            )
        except subprocess.TimeoutExpired as e:
            print(f"      ⚠ Command timed out after {timeout}s: {' '.join(cmd[:3])}...")
            # Return a failed result instead of crashing
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=124,  # Standard timeout exit code
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )


# ============================================================================
# C VALIDATOR
# ============================================================================

class CValidator(BaseValidator):
    """Validator for C projects"""

    def is_test_file(self, file_path: str) -> bool:
        if file_path == 'tests/support/util.tcl' or file_path.startswith('tests/support/'):
            return False
        return (
            file_path.endswith('_test.c') or
            file_path.endswith('.test') or  # jq test files
            file_path.startswith('test_') or
            file_path.startswith('tests/') or
            '/tests/' in file_path or
            '/test/' in file_path
        )

    def detect_required_version(self) -> str:
        """C doesn't have version requirements typically"""
        return "latest"

    def get_actual_version(self) -> str:
        """Get GCC/Clang version"""
        result = subprocess.run(["gcc", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return "gcc"
        result = subprocess.run(["clang", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return "clang"
        return "unknown"

    def setup_version(self, required_version: str):
        """No version setup needed for C"""
        pass

    def install_dependencies(self):
        """Build C project"""
        print(f"[4/7] Building project...")

        # Store build success state
        self.build_successful = False

        # Detect platform and architecture for compatibility fixes
        self.platform_system = platform.system()  # Darwin, Linux, Windows
        self.platform_machine = platform.machine()  # arm64, x86_64, etc.
        self.is_macos = self.platform_system == "Darwin"
        self.is_linux = self.platform_system == "Linux"
        self.is_arm64 = self.platform_machine in ('arm64', 'aarch64')
        self.is_x86_64 = self.platform_machine in ('x86_64', 'AMD64')

        # Initialize git submodules if .gitmodules exists
        # This is critical for projects like jq that use submodules for dependencies
        if (self.repo_dir / ".gitmodules").exists():
            print(f"      → Initializing git submodules...")
            result = self.run_command(["git", "submodule", "update", "--init", "--recursive"], timeout=300)
            if result.returncode != 0:
                print(f"      ⚠ Submodule initialization failed: {result.stderr[:200]}")
            else:
                print(f"      ✓ Submodules initialized")

                # Build submodules that have their own build systems
                # Common patterns: modules/*, vendor/*, lib/*, external/*
                submodule_patterns = ["modules/*", "vendor/*", "lib/*", "external/*"]
                for pattern in submodule_patterns:
                    for submodule_dir in self.repo_dir.glob(pattern):
                        if not submodule_dir.is_dir():
                            continue
                        if (submodule_dir / "configure.ac").exists() or (submodule_dir / "autogen.sh").exists():
                            print(f"      → Building submodule: {submodule_dir.name}...")

                            # Run autogen.sh or autoreconf if needed
                            if (submodule_dir / "autogen.sh").exists():
                                self.run_command(["./autogen.sh"], cwd=submodule_dir, timeout=180)
                            elif not (submodule_dir / "configure").exists():
                                self.run_command(["autoreconf", "-i"], cwd=submodule_dir, timeout=180)

                            # Configure submodule
                            if (submodule_dir / "configure").exists():
                                self.run_command(["./configure"], cwd=submodule_dir, timeout=180)

                            # Build submodule
                            if (submodule_dir / "Makefile").exists():
                                result = self.run_command(["make"], cwd=submodule_dir, timeout=300)
                                if result.returncode == 0:
                                    print(f"      ✓ Submodule {submodule_dir.name} built successfully")

        # Detect project type for special handling
        is_jq_project = (self.repo_dir / "modules" / "oniguruma").exists()
        is_redis_project = (self.repo_dir / "src" / "server.c").exists() or (self.repo_dir / "redis.conf").exists()

        # Handle Autotools projects (configure.ac/Makefile.am)
        if (self.repo_dir / "configure.ac").exists() or (self.repo_dir / "Makefile.am").exists():
            # Check if configure script exists, if not generate it
            if not (self.repo_dir / "configure").exists():
                result = self.run_command(["autoreconf", "-i"], timeout=300)
                if result.returncode != 0:
                    print(f"      ⚠ autoreconf failed: {result.stderr[:200]}")
                    return

            if not (self.repo_dir / "Makefile").exists():
                configure_opts = ["./configure", "--disable-maintainer-mode"]

                if is_jq_project:
                    configure_opts.append("--with-oniguruma=builtin")
                    configure_opts.append("--disable-docs")
                    if self.is_macos:
                        original_cflags = self.env_vars.get('CFLAGS', '')
                        self.env_vars['CFLAGS'] = original_cflags + ' -Wno-implicit-function-declaration -Wno-incompatible-function-pointer-types'

                result = self.run_command(configure_opts, timeout=300)
                if result.returncode != 0:
                    result = self.run_command(["./configure"], timeout=300)
                    if result.returncode != 0:
                        print(f"      ⚠ Configure failed: {result.stderr[:200]}")
                        return

            # On macOS with jq, patch the Makefile to add compiler flags
            # (Do this outside the configure block so it runs even if Makefile exists)
            if is_jq_project and self.is_macos:
                makefile_path = self.repo_dir / "Makefile"
                if makefile_path.exists():
                    # Read the Makefile
                    makefile_content = makefile_path.read_text()
                    # Add flags to CFLAGS line (only if not already added)
                    if 'CFLAGS = ' in makefile_content and 'Wno-error=implicit-function-declaration' not in makefile_content:
                        makefile_content = makefile_content.replace(
                            'CFLAGS = ',
                            'CFLAGS = -Wno-implicit-function-declaration -Wno-incompatible-function-pointer-types '
                        )
                        makefile_path.write_text(makefile_content)

        # Fix Redis stat64 issue on macOS
        # Modern macOS doesn't have stat64, use regular stat instead
        # Fix Redis compatibility issues on macOS
        if is_redis_project and self.is_macos:
            # Fix 1: stat64 doesn't exist on modern macOS
            redis_config = self.repo_dir / "src" / "config.h"
            if redis_config.exists():
                config_content = redis_config.read_text()
                if 'redis_stat stat64' in config_content:
                    # Patch config.h to use regular stat on macOS
                    config_content = config_content.replace(
                        '#if defined(__APPLE__) && !defined(MAC_OS_X_VERSION_10_6)\n#define redis_fstat fstat64\n#define redis_stat stat64',
                        '#if defined(__APPLE__) && !defined(MAC_OS_X_VERSION_10_6)\n#define redis_fstat fstat\n#define redis_stat stat'
                    )
                    redis_config.write_text(config_content)

            # Fix 2: ARM64 (Apple Silicon) compatibility
            # Older Redis has x86/PowerPC-specific debug code that doesn't compile on ARM64 macOS.
            # Two issues in debug.c:
            # (a) getMcontextEip: the old-macOS fallback uses __srr0 (PowerPC) with no ARM64 case
            # (b) register dump: #elif defined(__aarch64__) uses Linux struct layout (mcontext.regs[])
            #     which doesn't exist on macOS ARM64 (where mcontext is a pointer with __ss.__x[])
            if self.is_arm64:
                debug_c = self.repo_dir / "src" / "debug.c"
                if debug_c.exists():
                    debug_content = debug_c.read_text()
                    changed = False
                    # Fix (b) FIRST: Make top-level #elif defined(__aarch64__) blocks Linux-only
                    # so macOS ARM64 doesn't use Linux-specific mcontext.regs[] layout.
                    # Must run before fix (a) to avoid overwriting what fix (a) adds.
                    if '#elif defined(__aarch64__)' in debug_content:
                        debug_content = debug_content.replace(
                            '#elif defined(__aarch64__)',
                            '#elif defined(__aarch64__) && !defined(__APPLE__)'
                        )
                        changed = True
                    # Fix (a): Add macOS ARM64 case before the PPC (__srr0) fallback.
                    # The getMcontextEip old-macOS path has no ARM64 case; on Apple Silicon
                    # the #else branch would use __srr0 (PowerPC) which doesn't exist.
                    if '__srr0' in debug_content:
                        old_srr0 = (
                            '    #else\n'
                            '    return (void*) uc->uc_mcontext->__ss.__srr0;\n'
                            '    #endif\n'
                            '#elif defined(__APPLE__) && defined(MAC_OS_X_VERSION_10_6)'
                        )
                        # Use && defined(__APPLE__) so fix (b) won't touch it
                        new_srr0 = (
                            '    #elif defined(__aarch64__) && defined(__APPLE__)\n'
                            '    return (void*) uc->uc_mcontext->__ss.__pc;\n'
                            '    #else\n'
                            '    return (void*) uc->uc_mcontext->__ss.__srr0;\n'
                            '    #endif\n'
                            '#elif defined(__APPLE__) && defined(MAC_OS_X_VERSION_10_6)'
                        )
                        if old_srr0 in debug_content:
                            debug_content = debug_content.replace(old_srr0, new_srr0)
                            changed = True
                    if changed:
                        debug_c.write_text(debug_content)
                        print(f"      → Applied ARM64 macOS debug.c compatibility fixes")

        if (self.repo_dir / "Makefile").exists():
            make_cmd = ["make"]
            if is_jq_project and self.is_macos and 'CFLAGS' in self.env_vars:
                make_cmd.extend(["CFLAGS=" + self.env_vars['CFLAGS']])
            result = self.run_command(make_cmd, timeout=600)
            if result.returncode != 0:
                build_errors = result.stderr or result.stdout
                print(f"      ⚠ Make build failed:\n{build_errors[-3000:]}")
            else:
                print(f"      ✓ Build successful")
                self.build_successful = True
        else:
            print(f"      ⚠ No Makefile found - cannot build project")

    def _kill_stale_tcl_processes(self):
        """Kill stale tclsh/valkey-server processes from previous test runs"""
        try:
            subprocess.run(["pkill", "-f", "test_helper.tcl"], capture_output=True, timeout=10)
            subprocess.run(["pkill", "-f", "valkey-server"], capture_output=True, timeout=10)
            subprocess.run(["pkill", "-f", "redis-server"], capture_output=True, timeout=10)
            time.sleep(2)
        except Exception:
            pass

    def _extract_tcl_added_code(self, patch: str, tcl_file: str) -> str:
        """Extract the added lines (new test code) from a diff for a specific TCL file."""
        lines = []
        in_file = False
        for line in patch.split('\n'):
            if line.startswith('diff --git'):
                in_file = tcl_file in line
                continue
            if not in_file:
                continue
            if line.startswith('+++') or line.startswith('---') or line.startswith('@@'):
                continue
            if line.startswith('+'):
                lines.append(line[1:])  # Strip leading '+', keep indentation
        return '\n'.join(lines)

    def _extract_tcl_support_code(self, tcl_path: Path) -> str:
        """Extract support code needed by minimal TCL tests (sources, packages, procs)."""
        try:
            content = tcl_path.read_text()
        except Exception:
            return ""

        lines = content.splitlines()
        support_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("source ") or stripped.startswith("package require") or stripped.startswith("load "):
                support_lines.append(line)
                i += 1
                continue

            proc_match = re.match(r'^\s*proc\s+\w+', line)
            if proc_match:
                proc_lines = [line]
                brace_balance = line.count('{') - line.count('}')
                i += 1
                while i < len(lines):
                    proc_lines.append(lines[i])
                    brace_balance += lines[i].count('{') - lines[i].count('}')
                    if brace_balance <= 0:
                        break
                    i += 1
                support_lines.extend(proc_lines)
                i += 1
                continue

            i += 1

        return '\n'.join(support_lines).strip()

    def _extract_tcl_test_names(self, tcl_code: str) -> List[str]:
        """Extract test names from TCL code blocks."""
        names = []
        for line in tcl_code.splitlines():
            m = re.match(r'^\s*test\s+"([^"]+)"', line)
            if m:
                names.append(m.group(1).strip())
                continue
            m = re.match(r'^\s*test\s+\{([^}]+)\}', line)
            if m:
                names.append(m.group(1).strip())
        return names

    def _run_tcl_tests(self, tcl_files: List[str], test_patch: str = '',
                       debug: bool = False) -> Dict[str, str]:
        """Run Redis/Valkey-style Tcl tests using ./runtest.

        Creates a minimal TCL file with only the new tests from test_patch to avoid
        running the entire (slow) test file.
        """
        status_map = {}

        runtest_path = self.repo_dir / "runtest"
        if not runtest_path.exists():
            print(f"      [TCL] runtest not found at {runtest_path}")
            return status_map

        # Kill any stale processes from previous runs that might block ports
        self._kill_stale_tcl_processes()

        # Use a random baseport in a high range to avoid conflicts with the default 21079-21110
        import random
        baseport = random.randint(23000, 28000)

        def _run_single_tcl(run_unit: str, minimal_path: Optional[Path], added_test_names: List[str], display_name: str) -> Tuple[Dict[str, str], str, subprocess.CompletedProcess]:
            """Run a single Tcl unit and parse results."""
            cmd = ["./runtest", "--single", run_unit,
                   port_arg, str(baseport),
                   "--clients", "1",
                   "--timeout", "300"]

            result = self.run_command(cmd, timeout=420)
            output = result.stdout + result.stderr

            # Clean up minimal file
            if minimal_path and minimal_path.exists():
                minimal_path.unlink()

            # Strip ANSI escape codes for display and parsing
            clean_output = re.sub(r'\033\[[0-9;]*m', '', output)

            if debug:
                print(f"      [TCL] rc={result.returncode}")
                print(f"      [TCL] Output (first 3000 chars):\n{clean_output[:3000]}")
            else:
                # Show a brief summary of results
                result_lines = [l for l in clean_output.split('\n')
                               if re.search(r'\[(ok|err|exception)\]', l)]
                if result_lines:
                    print(f"      [TCL] rc={result.returncode}, {len(result_lines)} result(s)")
                    # Show error/exception lines with context so we can diagnose failures
                    for i, rl in enumerate(result_lines):
                        if re.search(r'\[(err|exception)\]', rl):
                            # Also grab next line for context (often shows "Expected X Got Y")
                            idx = clean_output.split('\n').index(rl) if rl in clean_output.split('\n') else -1
                            ctx = ''
                            if idx >= 0:
                                next_lines = clean_output.split('\n')[idx+1:idx+3]
                                ctx = ' | ' + ' '.join(l.strip() for l in next_lines if l.strip())
                            print(f"        {rl.strip()}{ctx}")
                else:
                    # On failure with no recognizable output, show first few lines
                    snippet = '\n'.join(clean_output.split('\n')[:6])
                    print(f"      [TCL] rc={result.returncode}: {snippet[:300]}")

            local_status = {}

            # Parse Redis/Valkey test output

            # Handle exceptions: when a server crashes, the framework outputs:
            #   [exception]: Executing test client: I/O error reading reply.
            #   <Tcl stack trace including>: test "Test name" { ...
            # Extract the test name from the exception's stack trace
            exc_blocks = re.split(r'\[exception\]', clean_output)
            for block in exc_blocks[1:]:  # Skip content before first exception
                # Look for test "name" pattern in the exception block
                test_in_exc = re.search(r'test\s+"([^"]+)"', block)
                if test_in_exc:
                    local_status[test_in_exc.group(1)] = 'FAILED'

            for line in clean_output.split('\n'):
                line = line.strip()

                ok_match = re.match(r'\[ok\]:?\s+(.+?)(?:\s+\(\d+\s*\w+\))?\s*$', line)
                if ok_match:
                    name = ok_match.group(1).strip()
                    # Skip "Check for memory leaks" and similar framework entries
                    if not name.startswith('Check for memory') and not name.startswith('Can\'t start'):
                        local_status[name] = 'PASSED'
                    continue

                err_match = re.match(r'\[err\]:?\s+(.+?)$', line)
                if err_match:
                    found = err_match.group(1).strip()
                    # Skip framework errors like "Can't start src/valkey-server"
                    if found.startswith("Can't start"):
                        continue
                    if ': ' in found:
                        found = found.split(': ')[0].strip()
                    local_status[found] = 'FAILED'
                    continue

            # Fallback: if minimal test produced no named results, map by extracted test names
            if added_test_names:
                has_named = any(name in local_status for name in added_test_names)
                if not has_named:
                    fallback_status = 'PASSED' if result.returncode == 0 else 'FAILED'
                    for name in added_test_names:
                        local_status[name] = fallback_status

            # If we have exceptions/errors but no named tests, mark file-level failure
            has_exception = "[exception]" in clean_output or "[err]" in clean_output
            lcs_unknown = "unknown command 'LCS'" in clean_output
            if not local_status and (has_exception or result.returncode != 0 or lcs_unknown):
                local_status[display_name] = 'FAILED'

            return local_status, clean_output, result

        for tcl_file in tcl_files:
            unit_name = tcl_file
            if unit_name.startswith('tests/'):
                unit_name = unit_name[len('tests/'):]
            if unit_name.endswith('.tcl'):
                unit_name = unit_name[:-4]

            # Determine which TCL file to actually run
            added_test_names = []
            if test_patch:
                # Extract only the new test code from the patch and write to a minimal file
                added_code = self._extract_tcl_added_code(test_patch, tcl_file)
                if added_code.strip():
                    minimal_unit = unit_name + '_swebench_minimal'
                    minimal_path = self.repo_dir / 'tests' / (minimal_unit + '.tcl')
                    support_code = self._extract_tcl_support_code(self.repo_dir / tcl_file)
                    if support_code:
                        minimal_path.write_text(support_code + '\n\n' + added_code + '\n')
                    else:
                        minimal_path.write_text(added_code + '\n')
                    added_test_names = self._extract_tcl_test_names(added_code)
                    run_unit = minimal_unit
                else:
                    run_unit = unit_name
                    minimal_path = None
            else:
                run_unit = unit_name
                minimal_path = None

            # Detect whether runtest supports --baseport (newer) or --port (older Redis)
            helper = self.repo_dir / "tests" / "test_helper.tcl"
            port_arg = "--baseport"
            if helper.exists() and "--baseport" not in helper.read_text():
                port_arg = "--port"
            local_status, clean_output, result = _run_single_tcl(run_unit, minimal_path, added_test_names, tcl_file)

            # If minimal extraction loses required context or breaks Tcl syntax, rerun the full file.
            if minimal_path and (not local_status
                                 or "key \"client\" not known in dictionary" in clean_output
                                 or "missing close-brace" in clean_output):
                print(f"      [TCL] Minimal test lacked context, rerunning full file...")
                local_status, clean_output, result = _run_single_tcl(unit_name, None, [], tcl_file)

            # Kill stale processes after each run
            self._kill_stale_tcl_processes()

            # Merge into overall status_map
            for k, v in local_status.items():
                status_map[k] = v

        return status_map

    def _find_relevant_tcl_files(self) -> List[str]:
        """Auto-discover relevant TCL test files from the solution patch.

        Used when test_patch is empty but the project has a TCL test suite (Redis/Valkey).
        Maps changed source files to likely test files, e.g.:
          src/t_set.c  -> tests/unit/type/set.tcl
          src/aof.c    -> tests/unit/aof.tcl
        """
        solution_patch = self.instance.get('patch', '')
        if not solution_patch:
            return []

        changed_files = re.findall(r'diff --git a/(.*?) b/', solution_patch)
        found = []
        for src_file in changed_files:
            # src/t_XXX.c -> tests/unit/type/XXX.tcl  (e.g. t_set.c -> set.tcl)
            m = re.match(r'src/t_(\w+)\.c', src_file)
            if m:
                candidate = f'tests/unit/type/{m.group(1)}.tcl'
                if (self.repo_dir / candidate).exists() and candidate not in found:
                    found.append(candidate)
                    continue

            # src/XXX.c -> tests/unit/XXX.tcl  (e.g. aof.c -> aof.tcl)
            m = re.match(r'src/(\w+)\.c', src_file)
            if m:
                name = m.group(1)
                for candidate in [f'tests/unit/{name}.tcl', f'tests/unit/type/{name}.tcl']:
                    if (self.repo_dir / candidate).exists() and candidate not in found:
                        found.append(candidate)
                        break

        if found:
            print(f"      [TCL] Auto-discovered {len(found)} test file(s) from solution patch: {found}")

        # Also generate protocol-level tests for response-type changes (nil ↔ empty array).
        # The Tcl client converts both to {}, so normal tests can't distinguish them.
        proto_tcl = self._generate_resp_type_tests()
        if proto_tcl:
            proto_path = self.repo_dir / 'tests' / 'unit' / 'swebench_proto_check.tcl'
            proto_path.write_text(proto_tcl)
            found.append('tests/unit/swebench_proto_check.tcl')
            print(f"      [TCL] Generated protocol-level test: swebench_proto_check.tcl")

        return found

    def _generate_resp_type_tests(self) -> str:
        """Generate a Tcl test that detects nil-vs-empty-array changes in Redis C patches.

        The Tcl redis client converts both *-1 (nil array) and *0 (empty array) to {},
        making them indistinguishable in normal tests.  This method generates a raw-socket
        Tcl test that reads RESP bytes directly to distinguish the two.

        Returns a Tcl script string, or '' if no such change is detected.
        """
        solution_patch = self.instance.get('patch', '')
        if not solution_patch:
            return ''

        # Detect direction of change in the patch lines
        # Look for - and + lines that swap shared.null <-> shared.emptyset (or nullbulk etc.)
        null_syms  = {'shared.null', 'shared.nullbulk', 'shared.nullarray'}
        empty_syms = {'shared.emptyset', 'shared.emptyarray', 'shared.emptymultibulk'}

        removed_null  = False   # was returning null, now something else
        added_empty   = False   # now returning empty set/array
        removed_empty = False
        added_null    = False

        for line in solution_patch.split('\n'):
            if not (line.startswith('+') or line.startswith('-')):
                continue
            sign = line[0]
            body = line[1:]
            for sym in null_syms:
                if sym in body:
                    if sign == '-':
                        removed_null = True
                    else:
                        added_null = True
            for sym in empty_syms:
                if sym in body:
                    if sign == '-':
                        removed_empty = True
                    else:
                        added_empty = True

        # null → empty: before=nil, after=empty-array  (expect FAIL→PASS)
        nil_to_empty  = removed_null and added_empty and not (removed_empty or added_null)
        # empty → null: before=empty-array, after=nil
        empty_to_nil  = removed_empty and added_null and not (removed_null or added_empty)

        if not (nil_to_empty or empty_to_nil):
            return ''

        # Derive the Redis command from the C function name in the diff context lines.
        # @@ ... @@ void spopWithCountCommand(client *c) → SPOP
        # Strip 'WithCount', 'Command', leading lowercase prefix to get root.
        cmd_candidates = []
        for m in re.finditer(r'@@[^\n]*?(\w+Command)\s*\(', solution_patch):
            func = m.group(1)  # e.g. "spopWithCountCommand"
            # Remove trailing "Command" and optional "WithCount" / "WithCount"
            root = re.sub(r'(?i)(withcount|command)$', '', func, count=2)
            root = re.sub(r'(?i)(withcount|command)$', '', root, count=2)
            if root:
                cmd_candidates.append(root.upper())
        # Fallback: scan changed lines for *Command mentions
        if not cmd_candidates:
            for m in re.finditer(r'\b(\w+Command)\b', solution_patch):
                func = m.group(1)
                root = re.sub(r'(?i)(withcount|command)$', '', func, count=2)
                root = re.sub(r'(?i)(withcount|command)$', '', root, count=2)
                if root and len(root) >= 3:
                    cmd_candidates.append(root.upper())

        if not cmd_candidates:
            return ''

        # Use the first (most likely) command; deduplicate
        seen = set()
        unique_cmds = [c for c in cmd_candidates if not (c in seen or seen.add(c))]
        cmd_upper = unique_cmds[0]

        # Determine expected RESP prefix and test direction
        if nil_to_empty:
            direction = "returns empty array (not nil) after fix"
            expected_prefix = '*0'   # after fix: empty array *0\r\n
        else:
            direction = "returns nil (not empty array) after fix"
            expected_prefix = '*-'   # after fix: nil array *-1\r\n

        # Use a short, predictable key name; count argument is always 100
        key = 'swebench_noexist'
        count = '100'

        # Build RESP2 multi-bulk request bytes at Python time.
        # *3\r\n$<cmdlen>\r\n<CMD>\r\n$<keylen>\r\n<key>\r\n$<countlen>\r\n<count>\r\n
        resp_req = (
            f'*3\r\n${len(cmd_upper)}\r\n{cmd_upper}\r\n'
            f'${len(key)}\r\n{key}\r\n'
            f'${len(count)}\r\n{count}\r\n'
        )
        # Escape for Tcl double-quoted string: backslash, quote, dollar, CR, LF.
        # $ must be escaped to prevent Tcl variable substitution ($4 → empty string).
        tcl_literal = (resp_req
            .replace('\\', '\\\\')
            .replace('"', '\\"')
            .replace('$', '\\$')
            .replace('\r', '\\r')
            .replace('\n', '\\n'))

        # Generate the Tcl test.
        # Strategy: open raw TCP socket, send RESP request, wait 100ms for response,
        # switch to non-blocking, read available bytes, check first 2 bytes of response.
        # Using after+non-blocking-read avoids gets blocking issues with binary channels.
        # Test returns the actual 2-byte prefix so [err] output shows what was received.
        tcl_test = (
            f'\n'
            f'    test "swebench-proto: {cmd_upper} COUNT on nonexisting key {direction}" {{\n'
            f'        r del {key}\n'
            f'        # Raw socket bypasses Tcl redis client (which maps both *-1 and *0 to {{}}).\n'
            f'        # We read raw RESP bytes to distinguish nil ($-1/\\*-1) from empty (\\*0).\n'
            f'        set fd [socket [srv host] [srv port]]\n'
            f'        fconfigure $fd -translation binary -buffering full\n'
            f'        puts -nonewline $fd "{tcl_literal}"\n'
            f'        flush $fd\n'
            f'        after 100\n'
            f'        fconfigure $fd -blocking 0\n'
            f'        set data [read $fd 8]\n'
            f'        fconfigure $fd -blocking 1\n'
            f'        close $fd\n'
            f'        # Return first 2 bytes of RESP response: *0=empty-array $-=nil-bulk *-=nil-array\n'
            f'        string range $data 0 1\n'
            f'    }} {{{expected_prefix}}}'
        )

        script = (
            '# Auto-generated by swebench validation: protocol-level nil-vs-empty-array check.\n'
            '# Uses raw TCP sockets to read RESP bytes directly, bypassing the Tcl redis\n'
            '# client which maps both *-1\\r\\n (nil) and *0\\r\\n (empty array) to {}.\n'
            '\n'
            'start_server {tags {"swebench-proto"}} {\n'
            + tcl_test +
            '\n}\n'
        )
        print(f"      [PROTO] Generated RESP test for {cmd_upper}: expect '{expected_prefix}' after fix")
        return script

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run C tests"""
        status_map = {}

        # Check if build was successful
        if not getattr(self, 'build_successful', False):
            print(f"      ⚠ Skipping tests - project did not build successfully")
            return status_map

        # Handle Tcl test files (Redis/Valkey-style Tcl test framework)
        tcl_files = [f for f in (test_files or []) if f.endswith('.tcl')]

        # If no TCL files given but this is a Redis/Valkey repo, auto-discover from solution patch
        if not tcl_files and (self.repo_dir / 'runtest').exists():
            tcl_files = self._find_relevant_tcl_files()

        if tcl_files:
            test_patch = self.instance.get('test_patch', '')
            return self._run_tcl_tests(tcl_files, test_patch=test_patch, debug=debug)

        # Try different test commands
        test_commands = [
            ["./tests/jqtest"],  # jq test runner (must be first to get detailed output)
            ["make", "test"],
            ["make", "check"],
            ["./run-tests"],
            ["./test/run-tests"],
            ["./tests/run-tests"]
        ]

        output = ""
        successful_cmd = None
        for cmd in test_commands:
            # Skip if script doesn't exist
            if cmd[0].startswith('./'):
                script_path = self.repo_dir / cmd[0][2:]
                if not script_path.exists():
                    continue

            result = self.run_command(cmd, timeout=600)
            combined_output = result.stdout + result.stderr

            # Use this command if:
            # 1. It succeeded (exit code 0), OR
            # 2. It produced test output (contains "Test #" which indicates jq test format)
            # This handles jq tests which return exit code 1 when tests fail
            has_test_output = 'Test #' in combined_output

            if result.returncode == 0 or has_test_output:
                output = combined_output
                successful_cmd = cmd
                if debug:
                    print(f"      [DEBUG] Using test command: {' '.join(cmd)}")
                    print(f"      [DEBUG] Output length: {len(output)} chars")
                    print(f"      [DEBUG] Return code: {result.returncode}")
                break

        if not output and debug:
            print(f"      [DEBUG] No test output from any command")
            return status_map

        # Parse test output (TAP format and other formats)
        for line in output.split('\n'):
            line = line.strip()

            # jq format: "Test #1: 'expression' at line number X"
            if line.startswith('Test #'):
                match = re.match(r'Test #(\d+): \'(.+?)\' at line number (\d+)', line)
                if match:
                    test_num = match.group(1)
                    test_expr = match.group(2)
                    test_name = f"Test #{test_num}: {test_expr}"
                    # Default to PASSED, will be overridden if we see error for this test
                    status_map[test_name] = 'PASSED'
            # jq failure format: "*** Expected ... for test at line number X: expression"
            # or "*** Test program failed to compile at line X: expression"
            elif line.startswith('***'):
                # Runtime failure: "*** Expected ... for test at line number X: expression"
                if 'for test at line number' in line:
                    match = re.search(r'for test at line number (\d+): (.+)', line)
                    if match:
                        line_num = match.group(1)
                        test_expr = match.group(2)
                        # Find the matching test by expression
                        for test_name in status_map:
                            if test_expr in test_name:
                                status_map[test_name] = 'FAILED'
                                break
                # Compile failure: "*** Test program failed to compile at line X: expression"
                elif 'failed to compile at line' in line:
                    match = re.search(r'at line (\d+): (.+)', line)
                    if match:
                        line_num = match.group(1)
                        test_expr = match.group(2)
                        # Find the matching test by expression
                        for test_name in status_map:
                            if test_expr in test_name:
                                status_map[test_name] = 'FAILED'
                                break
            # TAP format: "ok 1 - test name" or "not ok 1 - test name"
            elif line.startswith('ok ') or line.startswith('not ok '):
                match = re.match(r'(ok|not ok)\s+\d+\s+-\s+(.+)', line)
                if match:
                    status = match.group(1)
                    test_name = match.group(2)
                    status_map[test_name] = 'PASSED' if status == 'ok' else 'FAILED'
            # Generic PASS/FAIL format
            elif 'PASS' in line or 'FAIL' in line:
                if 'PASS' in line:
                    match = re.search(r'(\w+).*PASS', line)
                    if match:
                        status_map[match.group(1)] = 'PASSED'
                elif 'FAIL' in line:
                    match = re.search(r'(\w+).*FAIL', line)
                    if match:
                        status_map[match.group(1)] = 'FAILED'

        if debug and status_map:
            print(f"      [DEBUG] Parsed {len(status_map)} tests from output")

        return status_map


# ============================================================================
# MULTILINGUAL VALIDATOR (C only)
# ============================================================================

class MultilingualValidator:
    """Main validator that delegates to language-specific validators"""

    def __init__(self, instance: dict, workspace: Path, keep_env: bool = False):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.keep_env = keep_env
        self.language = None
        self.validator = None
        # Temporary conda environment for isolation
        self.env_name = f"swe_temp_{instance['instance_id'].replace('/', '_').replace('-', '_')}"
        self.required_version = None

    def setup_repo(self):
        """Clone repository at base_commit"""
        print(f"[1/7] Setting up repository...")
        repo_url = f"https://github.com/{self.instance['repo']}.git"

        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(self.repo_dir)],
            check=True, capture_output=True, timeout=300
        )
        subprocess.run(
            ["git", "fetch", "--depth=100", "origin", self.instance['base_commit']],
            cwd=self.repo_dir, check=True, capture_output=True, timeout=300
        )
        subprocess.run(
            ["git", "checkout", self.instance['base_commit']],
            cwd=self.repo_dir, check=True, capture_output=True, timeout=60
        )

        print(f"      ✓ Cloned at {self.instance['base_commit'][:8]}")

    def detect_language(self):
        """Detect programming language"""
        print(f"[2/7] Detecting language...")
        self.language = LanguageDetector.detect(self.repo_dir)
        print(f"      ✓ Detected: {self.language}")

        if self.language == "unknown":
            raise RuntimeError("Could not detect programming language")


    def run_in_env(self, command: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with language-specific environment variables"""
        try:
            # Merge environment variables from validator (e.g., NVM_DIR for JavaScript)
            env = os.environ.copy()
            if hasattr(self.validator, 'env_vars'):
                env.update(self.validator.env_vars)

            cmd = command
            return subprocess.run(
                cmd,
                cwd=cwd or self.repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
        except subprocess.TimeoutExpired as e:
            print(f"      ⚠ Command timed out after {timeout}s: {' '.join(command[:3])}...")
            return subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )


    def create_validator(self):
        """Create language-specific validator"""
        validators = {
            'c': CValidator,
        }

        validator_class = validators.get(self.language)
        if not validator_class:
            raise RuntimeError(f"No validator for language: {self.language} (this script only supports C)")

        # Pass self as parent so validator can use run_in_env
        self.validator = validator_class(self.instance, self.workspace, self.repo_dir, parent=self)

    def _clean_build_cache(self):
        """Clean build cache to force recompilation"""
        if (self.repo_dir / "Makefile").exists():
            subprocess.run(["make", "clean"], cwd=self.repo_dir, capture_output=True, timeout=60)
            result = subprocess.run(["make"], cwd=self.repo_dir, capture_output=True, timeout=600)
            if result.returncode != 0:
                print(f"      ⚠ Rebuild after patch failed: {result.stderr[:200]}")

    def validate(self) -> Tuple[List[str], List[str]]:
        """Run full validation workflow"""
        try:
            # Steps 1-2: Setup and language detection
            self.setup_repo()
            self.detect_language()
            self.create_validator()

            # Step 3-4: Setup environment and install dependencies
            self.validator.setup_environment()
            self.validator.install_dependencies()

            # Step 5: Extract test files
            test_patch = self.instance.get('test_patch', '')
            solution_patch = self.instance.get('patch', '')

            test_files = self.validator.extract_test_files_from_patch(test_patch)
            if not test_files:
                test_files = self.validator.extract_test_files_from_patch(solution_patch)

            print(f"[5/7] Found {len(test_files)} test file(s)")
            for tf in test_files[:10]:
                print(f"      - {tf}")
            if len(test_files) > 10:
                print(f"      ... and {len(test_files) - 10} more")

            # Step 6: Run tests before/after patches
            print(f"[6/7] Running tests...")

            filter_set = None
            baseline_for_pass_to_pass = None

            if not test_patch:
                # No test_patch, just apply solution and compare
                print(f"      → No test_patch, running baseline...")
                results_before = self.validator.run_tests(test_files=test_files, debug=False)

                if solution_patch:
                    print(f"      → Applying solution patch...")
                    self.validator.apply_patch(solution_patch, "solution_patch")

                    # Rebuild compiled projects after patching source files
                    self._clean_build_cache()

                    print(f"      → Running tests with solution...")

                    results_after = self.validator.run_tests(test_files=test_files, debug=False)
                else:
                    print(f"      → Running tests (baseline only)...")
                    results_after = self.validator.run_tests(test_files=test_files, debug=False)

            elif not solution_patch:
                # No solution_patch, just test_patch
                print(f"      → Running baseline...")
                results_before = self.validator.run_tests(test_files=test_files, debug=False)

                print(f"      → Applying test patch...")
                self.validator.apply_patch(test_patch, "test_patch")

                print(f"      → Running tests with test_patch...")
                results_after = self.validator.run_tests(test_files=test_files, debug=False)

            else:
                # Both patches exist - determine strategy

                # STRATEGY 1: Check if test_patch contains both tests AND fixes
                # This happens when test_patch wasn't cleanly separated from implementation changes
                has_fix_in_test_patch = False
                for match in re.finditer(r'diff --git a/(.*?) b/', test_patch):
                    file_path = match.group(1)
                    if not self.validator.is_test_file(file_path):
                        has_fix_in_test_patch = True
                        break

                if has_fix_in_test_patch:
                    # test_patch contains both test and fix - split them
                    print(f"      → test_patch contains fix, splitting...")
                    test_only, fix_only = self._split_test_patch(test_patch)

                    # Run baseline BEFORE applying test_only: test_only may reference new APIs
                    # that only exist after the solution is applied, causing the before-fix build
                    # to fail entirely and producing 0 results (losing all PASS_TO_PASS tests).
                    print(f"      → Running baseline tests (before test_patch)...")
                    _baseline_before_test_only = self.validator.run_tests(test_files=test_files, debug=False)

                    if test_only:
                        self.validator.apply_patch(test_only, "test_only")
                    print(f"      → Running tests (before fix)...")
                    results_before = self.validator.run_tests(test_files=test_files, debug=False)

                    # If test_only caused build failure, fall back to baseline for PASS_TO_PASS
                    if len(results_before) == 0 and len(_baseline_before_test_only) > 0:
                        print(f"      → Before-fix build failed (test_patch references new APIs); using baseline for PASS_TO_PASS")
                        baseline_for_pass_to_pass = _baseline_before_test_only

                    # Apply BOTH the fix from test_patch AND the separate solution_patch
                    if fix_only:
                        self.validator.apply_patch(fix_only, "fix_from_test_patch")
                    if solution_patch:
                        self.validator.apply_patch(solution_patch, "solution_patch")

                    # Rebuild project if solution_patch touched source files (dist/binary may be stale)
                    if solution_patch:
                        self._clean_build_cache()

                    print(f"      → Running tests (after fix)...")
                    results_after = self.validator.run_tests(test_files=test_files, debug=False)

                    filter_set = None  # No filtering needed

                # STRATEGY 2 & 3: Standard compatibility check
                else:
                    # Step 1: Get baseline (no patches)
                    print(f"      → Checking test_patch compatibility...")
                    print(f"      → Running baseline tests...")
                    baseline = self.validator.run_tests(test_files=test_files, debug=False)
                    _base_fail_count = sum(1 for s in baseline.values() if s in ['FAILED', 'ERROR'])
                    print(f"      → Baseline: {len(baseline)} tests ({_base_fail_count} failing)")

                    # Step 2: Apply test_patch and check
                    self.validator.apply_patch(test_patch, "test_patch")
                    print(f"      → Running tests with test_patch...")
                    with_test_patch = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → With test_patch: {len(with_test_patch)} tests")

                    # Check if any tests are failing (as expected)
                    _base_fail_count = sum(1 for s in baseline.values() if s in ['FAILED', 'ERROR'])
                    failing_tests = [name for name, status in with_test_patch.items()
                                    if status in ['FAILED', 'ERROR']]
                    passing_tests = [name for name, status in with_test_patch.items()
                                    if status == 'PASSED']
                    print(f"      → Status: {len(failing_tests)} failing, {len(passing_tests)} passing")

                    if failing_tests:
                        print(f"      → Failing tests (expected):")
                        for test in failing_tests[:5]:
                            print(f"        - {test}")
                        if len(failing_tests) > 5:
                            print(f"        ... and {len(failing_tests) - 5} more")
                    else:
                        print(f"      ⚠ WARNING: No tests failing with test_patch!")
                        print(f"      This may indicate:")
                        print(f"        - Test pattern extraction is too broad (running wrong tests)")
                        print(f"        - Baseline code already contains the fix")
                        print(f"        - test_patch doesn't properly add failing test cases")

                            # Step 3: Determine if test collection is broken
                    # If test_patch causes significant test count drop, it's incompatible
                    collection_broken = len(with_test_patch) < len(baseline) * 0.5 if baseline else len(with_test_patch) == 0
                    # Also treat as broken if count decreased with 0 failures: indicates
                    # an infinite loop / crash silently killed the test runner mid-run
                    crash_suspected = bool(baseline) and len(with_test_patch) < len(baseline) and len(failing_tests) == 0
                    collection_broken = collection_broken or crash_suspected

                    if collection_broken:
                        # FIX-FIRST STRATEGY: test_patch needs solution to work
                        print(f"      ⚠ test_patch incompatible with base_commit")
                        if crash_suspected and not (len(with_test_patch) < len(baseline) * 0.5):
                            print(f"      → Test count dropped: {len(baseline)} → {len(with_test_patch)} with 0 failures (crash/infinite-loop suspected)")
                        elif baseline and len(with_test_patch) < len(baseline) * 0.5:
                            print(f"      → Test count dropped: {len(baseline)} → {len(with_test_patch)} (>50% loss)")
                        else:
                            print(f"      → Tests failed to run with test_patch")
                        print(f"      → Using fix-first strategy")

                        # Reset everything
                        subprocess.run(["git", "checkout", "."], cwd=self.repo_dir, capture_output=True)
                        subprocess.run(["git", "clean", "-fd"], cwd=self.repo_dir, capture_output=True)

                        # If baseline was 0 (targeted test didn't exist in baseline),
                        # use empty broader_baseline for C (no workspace packages to run)
                        broader_baseline = {}

                        # Apply solution FIRST (so test_patch can compile)
                        print(f"      → Applying solution patch first...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        # Then apply test_patch
                        print(f"      → Applying test patch...")
                        self.validator.apply_patch(test_patch, "test_patch")

                        # BEFORE: Temporarily revert solution to see tests fail
                        print(f"      → Temporarily reverting solution...")
                        solution_file = self.workspace / "solution_reverse.patch"
                        solution_file.write_text(solution_patch)
                        _revert_result = subprocess.run(
                            ["git", "apply", "-R", "--whitespace=fix", str(solution_file)],
                            cwd=self.repo_dir,
                            capture_output=True
                        )
                        if _revert_result.returncode != 0:
                            print(f"      ⚠ git apply -R failed, using file-level revert...")
                            # Fallback: revert individual files from solution_patch
                            _sol_modified = []
                            _sol_added = []
                            for _line in solution_patch.split('\n'):
                                if _line.startswith('diff --git a/') and ' b/' in _line:
                                    _fpath = _line.split(' b/', 1)[1].strip()
                                    _cat = subprocess.run(
                                        ["git", "cat-file", "-e", f"HEAD:{_fpath}"],
                                        cwd=self.repo_dir, capture_output=True
                                    )
                                    if _cat.returncode == 0:
                                        _sol_modified.append(_fpath)
                                    else:
                                        _sol_added.append(_fpath)
                            if _sol_modified:
                                subprocess.run(
                                    ["git", "checkout", "HEAD", "--"] + _sol_modified,
                                    cwd=self.repo_dir, capture_output=True
                                )
                            for _new_f in _sol_added:
                                _np = self.repo_dir / _new_f
                                if _np.exists():
                                    _np.unlink()

                        # Clean build cache if needed
                        self._clean_build_cache()

                        print(f"      → Running tests (without solution)...")
                        results_before = self.validator.run_tests(test_files=test_files, debug=False)

                        # AFTER: Re-apply solution
                        print(f"      → Re-applying solution...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        # Clean build cache again
                        self._clean_build_cache()

                        print(f"      → Running tests (with solution)...")

                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                        # Store baseline for PASS_TO_PASS calculation.
                        baseline_for_pass_to_pass = baseline if baseline else None

                    else:
                        # STANDARD STRATEGY: test_patch is compatible with base_commit
                        print(f"      → Using standard strategy")

                        # results_before = with_test_patch (tests should fail)
                        results_before = with_test_patch

                        # Apply solution patch
                        print(f"      → Applying solution patch...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        # Clean build cache and rebuild if needed (for C/C++ projects)
                        self._clean_build_cache()

                        # results_after (tests should pass)
                        print(f"      → Running tests (with solution)...")

                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                        # Pass baseline so compare_results can filter pre-existing failures
                        baseline_for_pass_to_pass = baseline if baseline else None
                        filter_set = None

            print(f"      ✓ Tests completed ({len(results_before)} before, {len(results_after)} after)")

            # Check if we actually ran tests
            if len(results_before) == 0 and len(results_after) == 0:
                print(f"      ⚠ WARNING: No tests were detected or run!")
                if hasattr(self.validator, 'setup_failed') and self.validator.setup_failed:
                    print(f"      → Environment setup failed - unable to run tests")
                    print(f"      → Required: {self.validator.detected_version}, Actual: {self.validator.actual_version}")
                else:
                    print(f"      This may indicate a version mismatch or build failure.")
                    print(f"      Required: {self.validator.detected_version}, Actual: {self.validator.actual_version}")

            # Step 7: Compare results
            print(f"[7/7] Comparing results...")

            # Show status breakdown
            before_failed = sum(1 for s in results_before.values() if s in ['FAILED', 'ERROR'])
            before_passed = sum(1 for s in results_before.values() if s == 'PASSED')
            after_failed = sum(1 for s in results_after.values() if s in ['FAILED', 'ERROR'])
            after_passed = sum(1 for s in results_after.values() if s == 'PASSED')

            print(f"      → Before: {before_failed} failed, {before_passed} passed")
            print(f"      → After: {after_failed} failed, {after_passed} passed")

            # Store for later use
            self.snapshot_validation_passed = None

            fail_to_pass, pass_to_pass = self.compare_results(results_before, results_after, filter_set, test_files, baseline_for_pass_to_pass)

            print(f"      ✓ FAIL_TO_PASS: {len(fail_to_pass)} tests")
            for test in fail_to_pass[:10]:
                print(f"        - {test}")
            if len(fail_to_pass) > 10:
                print(f"        ... and {len(fail_to_pass) - 10} more")

            print(f"      ✓ PASS_TO_PASS: {len(pass_to_pass)} tests")

            # Show which tests changed status for debugging
            if len(fail_to_pass) == 0 and before_failed > 0:
                print(f"      ⚠ WARNING: {before_failed} tests failed before, but none transitioned to PASS")
                print(f"      Tests that failed before and after:")
                still_failing = [name for name in results_before.keys()
                               if results_before.get(name) in ['FAILED', 'ERROR']
                               and results_after.get(name) in ['FAILED', 'ERROR']]
                for test in still_failing[:5]:
                    print(f"        - {test}")

            return fail_to_pass, pass_to_pass

        finally:
            # No cleanup needed - using native version managers
            pass

    def compare_results(
        self,
        before: Dict[str, str],
        after: Dict[str, str],
        filter_to_modified: set = None,
        test_files: List[str] = None,
        baseline: Dict[str, str] = None
    ) -> Tuple[List[str], List[str]]:
        """
        Compare test results to find FAIL_TO_PASS and PASS_TO_PASS

        Args:
            before: Test results before solution patch
            after: Test results after solution patch
            filter_to_modified: Optional set of modified test names to filter FAIL_TO_PASS
            test_files: Optional list of test file paths to help with smart filtering
            baseline: Optional baseline results (used when before is empty due to compilation failures)

        Returns: (FAIL_TO_PASS, PASS_TO_PASS)

        Note: New tests (not in before) that pass are considered FAIL_TO_PASS
        """
        fail_to_pass = []
        pass_to_pass = []

        all_tests = set(before.keys()) | set(after.keys())

        # If baseline provided, also include tests from baseline
        if baseline:
            all_tests |= set(baseline.keys())

        # Smart filtering: If no explicit filter but we have test_files,
        # create a filter based on test file names/identifiers
        # Note: Skip for JavaScript/TypeScript since test names don't include describe block hierarchy
        smart_filter = None
        language = getattr(self, 'language', None)
        if filter_to_modified is None and test_files and language not in ['javascript', 'typescript']:
            smart_filter = self._create_smart_filter(test_files)
            if smart_filter:
                print(f"      → Filtering FAIL_TO_PASS to tests matching: {', '.join(sorted(smart_filter)[:5])}")

        tcl_added_tests: Set[str] = set()
        if test_files and any(tf.endswith('.tcl') for tf in test_files):
            tcl_added_tests = self._extract_tcl_test_names_from_patch(self.instance.get('test_patch', ''))

        for test in all_tests:
            before_status = before.get(test)
            after_status = after.get(test)

            # If baseline provided and before is empty, use baseline for PASS_TO_PASS
            baseline_status = baseline.get(test) if baseline else None

            if after_status == 'PASSED':
                if before_status is None or before_status in ('FAILED', 'ERROR'):
                    # Test was failing/missing and now passes
                    # But check if it was passing in baseline (fix-first strategy edge case)
                    if baseline_status == 'PASSED' and before_status is None:
                        # Test existed in baseline (before test_patch was applied), so it is
                        # NOT a new test regardless of the smart filter. The "before" run
                        # crashed (e.g. OOM from infinite recursion introduced by test_patch),
                        # but this test was passing before. → PASS_TO_PASS.
                        #
                        # Exception: if the test is explicitly in filter_to_modified, it was
                        # MODIFIED by test_patch (not just a new test). The modification changes
                        # what the test asserts — so even though the old version passed in
                        # baseline, the new version should fail without the solution. → FAIL_TO_PASS.
                        if filter_to_modified and self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                        else:
                            pass_to_pass.append(test)
                    else:
                        # Normal FAIL_TO_PASS logic
                        # Skip tests that were already failing at baseline (pre-existing failures,
                        # not introduced by test_patch). These should not be FAIL_TO_PASS candidates.
                        if baseline_status in ('FAILED', 'ERROR'):
                            # For TCL tests, baseline may include new tests due to minimal extraction.
                            # If the test name appears in the test_patch additions, treat it as new.
                            if tcl_added_tests and test in tcl_added_tests:
                                if filter_to_modified is None:
                                    if smart_filter is None or self._test_matches_smart_filter(test, smart_filter):
                                        fail_to_pass.append(test)
                                elif self._test_in_filter(test, filter_to_modified):
                                    fail_to_pass.append(test)
                            else:
                                pass  # Pre-existing failure — not a test_patch-introduced regression
                        elif filter_to_modified is None:
                            if smart_filter is None or self._test_matches_smart_filter(test, smart_filter):
                                fail_to_pass.append(test)
                        elif self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                else:
                    # Test was passing in before and still passes
                    pass_to_pass.append(test)

        # Tcl tests (Redis/Valkey): sometimes ok output omits the test name,
        # so the passed test won't map back to the failed name. If before had
        # failures, after has zero failures, and counts are comparable, treat
        # missing failed names as FAIL_TO_PASS.
        if test_files and any(tf.endswith('.tcl') for tf in test_files):
            before_failed_names = [n for n, s in before.items() if s in ('FAILED', 'ERROR')]
            after_failed_count = sum(1 for s in after.values() if s in ('FAILED', 'ERROR'))
            missing_failed = [n for n in before_failed_names if n not in after]
            if missing_failed and not fail_to_pass and after_failed_count == 0:
                if len(after) >= max(1, int(len(before) * 0.9)):
                    print(f"      → Treating {len(missing_failed)} missing failed test(s) as FAIL_TO_PASS (TCL name mismatch)")
                    fail_to_pass = sorted(missing_failed)

        return sorted(fail_to_pass), sorted(pass_to_pass)

    def _test_in_filter(self, test_name: str, filter_set: set) -> bool:
        """Check if test matches any pattern in filter set.

        Handles multiple naming conventions:
          Java:  org.apache.lucene.TestFoo.testBar          (. separator)
          PHP:   ClassName (Namespace\\ClassName)::testBar  (:: separator)
          Ruby:  ./spec/foo_spec.rb[1:2:3]                 (path-based)
        """
        if test_name in filter_set:
            return True

        # Extract the method name using both . and :: separators
        # PHP: "Issue3679Img (PhpOffice\...\Issue3679Img)::testCroppedPicture"
        # Java: "org.apache.lucene.TestFoo.testBar"
        if '::' in test_name:
            test_method = test_name.split('::')[-1]
        else:
            test_parts = test_name.split('.')
            test_method = test_parts[-1] if test_parts else test_name

        # Direct method-name match in filter set
        if test_method in filter_set:
            return True

        # Fuzzy matching: compare method names across patterns
        for pattern in filter_set:
            pattern_parts = pattern.split('.')
            pattern_method = pattern_parts[-1] if pattern_parts else pattern

            if test_method == pattern_method:
                # For Java: also check class name (second-to-last .part)
                test_parts_dot = test_name.split('.')
                if len(test_parts_dot) >= 2 and len(pattern_parts) >= 2:
                    if test_parts_dot[-2] == pattern_parts[-2]:
                        return True
                else:
                    return True

        return False

    def _extract_tcl_test_names_from_patch(self, test_patch: str) -> Set[str]:
        """Extract TCL test names added by test_patch."""
        names: Set[str] = set()
        if not test_patch:
            return names
        for line in test_patch.splitlines():
            if not line.startswith('+') or line.startswith('+++'):
                continue
            m = re.match(r'^\+\s*test\s+"([^"]+)"', line)
            if m:
                names.add(m.group(1).strip())
                continue
            m = re.match(r'^\+\s*test\s+\{([^}]+)\}', line)
            if m:
                names.add(m.group(1).strip())
        return names

    def _extract_describe_blocks_from_patch(self, test_patch: str) -> Set[str]:
        """
        Extract describe/suite block names from test_patch diff content.
        This helps match tests when describe block names differ from filenames.
        Looks at context around additions to capture parent describe blocks.

        Args:
            test_patch: The test patch content

        Returns: Set of describe block names found
        """
        describe_names = set()

        try:
            # Look at all lines in hunks that have additions
            # This captures both new describe blocks and existing parent describe blocks
            in_hunk_with_additions = False
            hunk_lines = []

            for line in test_patch.split('\n'):
                # Track if we're in a hunk
                if line.startswith('@@'):
                    # Process previous hunk
                    if in_hunk_with_additions:
                        self._extract_patterns_from_lines(hunk_lines, describe_names)
                    # Start new hunk
                    in_hunk_with_additions = False
                    hunk_lines = []
                elif line.startswith('+') and not line.startswith('+++'):
                    # This hunk has additions
                    in_hunk_with_additions = True
                    hunk_lines.append(line)
                elif not line.startswith('-') and not line.startswith('+++') and not line.startswith('---'):
                    # Context line (could be a parent describe block)
                    hunk_lines.append(line)

            # Process last hunk
            if in_hunk_with_additions:
                self._extract_patterns_from_lines(hunk_lines, describe_names)

        except Exception:
            # If we can't parse the patch, just return empty set
            pass

        return describe_names

    def _extract_patterns_from_lines(self, lines: List[str], describe_names: Set[str]):
        """Extract test patterns from lines of code"""
        for line in lines:
            # Strip diff markers and whitespace
            clean_line = line.lstrip('+ \t')

            # JavaScript/TypeScript: describe('name', ...) or describe("name", ...)
            for match in re.finditer(r'describe\s*\(\s*[\'"]([^\'"]+)[\'"]', clean_line):
                name = match.group(1)
                # Keep all names except generic ones
                if name and name not in ['test', 'tests']:
                    describe_names.add(name)

            # Python: class TestName
            for match in re.finditer(r'class\s+(Test\w+)', clean_line):
                name = match.group(1).replace('Test', '', 1)
                if name:
                    describe_names.add(name)

            # Java: class names with Test
            for match in re.finditer(r'class\s+(\w+Test|\w+Tests?)', clean_line):
                name = match.group(1).replace('Test', '').replace('Tests', '')
                if name:
                    describe_names.add(name)

            # Go: func TestName
            for match in re.finditer(r'func\s+Test(\w+)', clean_line):
                name = match.group(1)
                if name:
                    describe_names.add(name)

            # Rust: fn test_name or #[test]
            for match in re.finditer(r'fn\s+(test_)?(\w+)\s*\(', clean_line):
                name = match.group(2)
                if name and not name.startswith('test_'):
                    describe_names.add(name)

    def _create_smart_filter(self, test_files: List[str]) -> Optional[Set[str]]:
        """
        Create smart filter patterns from test files to identify which tests are relevant.
        This works across all languages by extracting identifiable patterns.

        Args:
            test_files: List of test file paths modified in test_patch

        Returns: Set of patterns to match against test names, or None if can't create
        """
        if not test_files:
            return None

        patterns = set()

        for file_path in test_files:
            # Extract identifying information from file path
            file_name = Path(file_path).stem  # e.g., "PIE800" from "PIE800.py"

            # Skip generic names
            if file_name in ['test', 'tests', '__init__', 'mod']:
                continue

            # Language-specific extraction
            if file_path.endswith('.snap'):
                # Rust snapshot: extract test ID from filename
                # e.g., ruff_linter__rules__flake8_pie__tests__PIE800_PIE800.py.snap
                parts = file_name.split('__')
                if 'tests' in parts:
                    test_idx = parts.index('tests')
                    # Get parts after 'tests'
                    test_specific = parts[test_idx+1:]
                    if test_specific:
                        patterns.add(test_specific[0])  # e.g., "PIE800"

            elif file_path.endswith(('.py', '.rs', '.go', '.java', '.js', '.ts', '.php', '.rb', '.c', '.cpp')):
                # Test file: use filename as identifier
                # Remove common test prefixes/suffixes (test_, _test, _spec)
                identifier = file_name.replace('test_', '').replace('_test', '').replace('_spec', '')
                if identifier and identifier not in ['test', 'tests']:
                    patterns.add(identifier)
                # Also keep the full stem (with _spec) so the file-path-embedded RSpec
                # JSON ids (e.g. "./spec/.../block_delimiters_spec.rb[1:2:3]") match too
                if file_name != identifier and file_name not in ['test', 'tests']:
                    patterns.add(file_name)

                # For paths with category/fixture structure
                if '/fixtures/' in file_path or '/test/' in file_path:
                    # Extract category
                    path_parts = file_path.split('/')
                    for i, part in enumerate(path_parts):
                        if part in ['fixtures', 'test'] and i + 1 < len(path_parts):
                            category = path_parts[i + 1]
                            if category:
                                patterns.add(category)

        # Also extract describe block names from test_patch content
        # This handles cases where describe blocks have different names than the file
        test_patch = self.instance.get('test_patch', '')
        if test_patch:
            describe_blocks = self._extract_describe_blocks_from_patch(test_patch)
            patterns.update(describe_blocks)

        return patterns if patterns else None

    def _test_matches_smart_filter(self, test_name: str, smart_filter: Set[str]) -> bool:
        """
        Check if test name matches any pattern in smart filter.
        Uses fuzzy matching to work across different test naming conventions.

        Args:
            test_name: Full test name (e.g., "rules::flake8_pie::tests::PIE800::case1")
            smart_filter: Set of patterns (e.g., {"PIE800", "flake8_pie"})

        Returns: True if test matches any pattern
        """
        test_lower = test_name.lower()
        test_norm = re.sub(r'[^a-z0-9]+', ' ', test_lower).strip()

        for pattern in smart_filter:
            pattern_lower = pattern.lower()
            pattern_norm = re.sub(r'[^a-z0-9]+', ' ', pattern_lower).strip()

            # Direct substring match
            if pattern_lower in test_lower:
                return True
            if pattern_norm and pattern_norm in test_norm:
                return True

            # For patterns ending with 'test', also try without it (e.g., RoundTest -> Round)
            # This handles PHP classes where file is RoundTest.php but class is Round in namespace
            if pattern_lower.endswith('test'):
                pattern_without_test = pattern_lower[:-4]
                if pattern_without_test and pattern_without_test in test_lower:
                    return True

            # For patterns starting with 'test', also try without it
            if pattern_lower.startswith('test'):
                pattern_without_test = pattern_lower[4:]
                if pattern_without_test and pattern_without_test in test_lower:
                    return True

            # Match with common separators (::, ., __, /)
            for sep in ['::', '.', '__', '/', '_']:
                if f"{sep}{pattern_lower}{sep}" in test_lower:
                    return True
                if test_lower.startswith(f"{pattern_lower}{sep}"):
                    return True
                if test_lower.endswith(f"{sep}{pattern_lower}"):
                    return True

            # Word-split fallback: split the pattern on underscores/hyphens into
            # individual words and check that ALL words appear in the test name.
            # This handles Go's naming convention where a file like
            # "context_apply_test.go" produces pattern "context_apply", but the
            # test function is named "TestContext2Apply_*" — the digit and missing
            # underscore prevent a direct substring match, yet both "context" and
            # "apply" are present as substrings of the test name.
            # Guard: require at least 2 words of length > 2 to avoid false positives
            # from short/generic tokens.
            words = [w for w in re.split(r'[_\-]', pattern_lower) if len(w) > 2]
            if len(words) >= 2 and all(w in test_lower for w in words):
                return True

        return False

    def _split_test_patch(self, test_patch: str) -> Tuple[str, str]:
        """
        Split patch into test-only and fix-only parts

        This handles cases where test_patch contains both test code AND implementation fixes.
        Common in Python, rare but possible in other languages.

        Returns: (test_only_patch, fix_only_patch)
        """
        test_hunks = []
        fix_hunks = []

        # Split patch by file headers
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

                # Use language-specific validator to determine if this is a test file
                if self.validator.is_test_file(file_path):
                    test_hunks.append(full_patch)
                else:
                    fix_hunks.append(full_patch)

            i += 2

        return ''.join(test_hunks), ''.join(fix_hunks)

    def _prefer_results(self, current: Dict[str, str], candidate: Dict[str, str], label: str) -> Dict[str, str]:
        """Pick the more trustworthy results set.

        Prefer runs with a higher test count; only prefer fewer failures if counts
        are comparable. This avoids accepting truncated runs that show fewer tests.
        """
        if not candidate:
            return current
        if not current:
            print(f"      → Using {label} results (no current results)")
            return candidate

        cur_len, cand_len = len(current), len(candidate)
        cur_fail = sum(1 for s in current.values() if s in ['FAILED', 'ERROR'])
        cand_fail = sum(1 for s in candidate.values() if s in ['FAILED', 'ERROR'])

        # If candidate has significantly more tests, prefer it
        if cand_len >= cur_len * 1.1:
            print(f"      → {label} results have more tests: {cur_len} → {cand_len}")
            return candidate

        # If candidate has significantly fewer tests, reject it
        if cand_len <= cur_len * 0.7:
            print(f"      → {label} results have fewer tests: {cur_len} → {cand_len} (keeping current)")
            return current

        # Counts are comparable: prefer fewer failures
        if cand_fail < cur_fail:
            print(f"      → {label} results have fewer failures: {cur_fail} → {cand_fail}")
            return candidate

        return current

    def cleanup(self):
        """Cleanup"""
        pass


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def validate_instance(instance_path: str, output_path: Optional[str] = None, keep_env: bool = False):
    """Main validation function"""
    if output_path is None:
        input_path = Path(instance_path)
        output_filename = input_path.name.replace('_part1.json', '_part2.json')
        if output_filename == input_path.name:
            output_filename = input_path.stem + '_part2.json'
        output_path = str(input_path.parent / output_filename)

    with open(instance_path) as f:
        data = json.load(f)
        instance = data[0] if isinstance(data, list) else data

    # Use /tmp as the base directory to get shorter paths.
    # This is critical for projects like Valkey/Redis that use Unix domain sockets,
    # which have a 104-character path limit on macOS. Long temp paths (like
    # /var/folders/...) cause "unix socket path too long" errors.
    with tempfile.TemporaryDirectory(dir='/tmp') as tmpdir:
        workspace = Path(tmpdir)
        validator = MultilingualValidator(instance, workspace, keep_env)

        try:
            fail_to_pass, pass_to_pass = validator.validate()

            instance['FAIL_TO_PASS'] = fail_to_pass
            instance['PASS_TO_PASS'] = pass_to_pass
            instance['environment_setup_commit'] = instance['base_commit']

            # Add version info
            if hasattr(validator.validator, 'detected_version'):
                instance['detected_version'] = validator.validator.detected_version
                instance['actual_version'] = validator.validator.actual_version

            with open(output_path, 'w') as f:
                json.dump(instance, f, indent=2)

            print(f"\n{'='*80}")
            print(f"✓ Validation complete!")
            print(f"  Language: {validator.language}")
            if hasattr(validator.validator, 'detected_version'):
                print(f"  Required version: {validator.validator.detected_version}")
                print(f"  Actual version: {validator.validator.actual_version}")
            print(f"  Output: {output_path}")
            print(f"  FAIL_TO_PASS: {len(fail_to_pass)}")
            print(f"  PASS_TO_PASS: {len(pass_to_pass)}")
            print(f"{'='*80}")

        except Exception as e:
            print(f"\n{'='*80}")
            print(f"✗ Validation failed: {e}")
            traceback.print_exc()
            print(f"{'='*80}")
            raise
        finally:
            validator.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench multilingual instance validation with version detection"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
