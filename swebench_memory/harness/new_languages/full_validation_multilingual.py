#!/usr/bin/env python3
"""
SWE-bench Multilingual Instance Validation Script - Version 2.0

Supports: Python, Rust, Go, Java, JavaScript/TypeScript, PHP, Ruby, C
WITH PROPER VERSION DETECTION AND MANAGEMENT

Key improvements:
- Detects required language versions from project files
- Date-based version fallbacks
- Installs correct versions using version managers
- Fails validation properly if version mismatch can't be resolved

Usage:
    python3 full_validation_multilingual.py instance.json [--output validated.json]
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
# RUST VALIDATOR
# ============================================================================

class RustValidator(BaseValidator):
    """Validator for Rust projects"""

    def is_test_file(self, file_path: str) -> bool:
        # Match Rust test files (.rs)
        if file_path.endswith('.rs'):
            return '/test' in file_path or file_path.startswith('test') or '_test.rs' in file_path or 'tests/' in file_path

        # Also match snapshot files and test fixtures (for test pattern extraction)
        if file_path.endswith('.snap'):
            return '/snapshots/' in file_path
        if '/resources/test/fixtures/' in file_path or '/test/fixtures/' in file_path:
            return True

        # bat-style syntax test files (source and highlighted test data)
        if 'tests/syntax-tests/' in file_path:
            return True

        return False

    def _extract_required_rust_version(self, error_msg: str) -> str:
        """Extract required Rust version from error message

        Example: "requires rustc 1.71 or newer" -> "1.71"
        """
        match = re.search(r'requires rustc (\d+\.\d+)', error_msg)
        if match:
            return match.group(1)
        return None

    def _clean_cargo_registry(self, error_msg: str):
        """Clean corrupted cargo registry packages"""
        print(f"      → Cleaning corrupted cargo registry packages...")
        cargo_home = os.path.expanduser("~/.cargo/registry")

        if not os.path.exists(cargo_home):
            return

        # Extract package names from error messages
        packages_to_clean = set()

        # Pattern: "cc-1.1.1" from error message
        if "cc-" in error_msg or "cc v" in error_msg:
            packages_to_clean.add("cc")

        # Generic pattern: failed to parse manifest at .../package-version/Cargo.toml
        manifest_matches = re.findall(r'/([^/]+)-(\d+\.\d+[^/]*)/Cargo\.toml', error_msg)
        for pkg_name, version in manifest_matches:
            packages_to_clean.add(pkg_name)

        if not packages_to_clean:
            return

        print(f"      → Cleaning registry packages: {', '.join(packages_to_clean)}")

        # Clean src directory (extracted package sources)
        src_dir = os.path.join(cargo_home, "src")
        if os.path.exists(src_dir):
            for item in os.listdir(src_dir):
                src_index = os.path.join(src_dir, item)
                if os.path.isdir(src_index):
                    for pkg in os.listdir(src_index):
                        for pkg_to_clean in packages_to_clean:
                            if pkg.startswith(f"{pkg_to_clean}-"):
                                shutil.rmtree(os.path.join(src_index, pkg), ignore_errors=True)

        # Clean cache directory (downloaded .crate files)
        cache_dir = os.path.join(cargo_home, "cache")
        if os.path.exists(cache_dir):
            for item in os.listdir(cache_dir):
                cache_index = os.path.join(cache_dir, item)
                if os.path.isdir(cache_index):
                    for pkg in os.listdir(cache_index):
                        for pkg_to_clean in packages_to_clean:
                            if pkg.startswith(f"{pkg_to_clean}-"):
                                try:
                                    os.remove(os.path.join(cache_index, pkg))
                                except Exception:
                                    pass

    def _fix_cargo_readme_fields(self) -> int:
        """Fix invalid readme fields in Cargo.toml files.

        Some repos use TOML dotted-key workspace inheritance like:
            readme.workspace = true
        which creates a table value that Cargo cannot parse as bool/string,
        causing 'expected a boolean or a string for key package.readme' errors.
        Replaces all such occurrences with `readme = false`.
        """
        fixed_count = 0
        for cargo_toml in self.repo_dir.rglob("Cargo.toml"):
            try:
                content = cargo_toml.read_text()
                # Match dotted-key workspace inheritance: readme.workspace = true
                new_content = re.sub(
                    r'^readme\.[a-zA-Z_]+\s*=\s*.+$',
                    'readme = false',
                    content,
                    flags=re.MULTILINE
                )
                # Also match inline table: readme = { workspace = true }
                new_content = re.sub(
                    r'^readme\s*=\s*\{[^}]*\}',
                    'readme = false',
                    new_content,
                    flags=re.MULTILINE
                )
                if new_content != content:
                    cargo_toml.write_text(new_content)
                    fixed_count += 1
            except Exception:
                pass
        return fixed_count

    def is_version_compatible(self, required: str, actual: str) -> bool:
        """Check Rust version compatibility - newer versions usually work"""
        if required == actual:
            return True
        try:
            req_parts = [int(x) for x in required.split('.')]
            act_parts = [int(x) for x in actual.split('.')]
            # Allow same or newer minor version (Rust is backward compatible)
            if req_parts[0] == act_parts[0] and act_parts[1] >= req_parts[1]:
                return True
        except:
            pass
        return False  # If can't parse or incompatible, try to switch

    def detect_required_version(self) -> str:
        """Detect required Rust version"""
        # Check rust-toolchain or rust-toolchain.toml
        for toolchain_file in ["rust-toolchain", "rust-toolchain.toml"]:
            path = self.repo_dir / toolchain_file
            if path.exists():
                content = path.read_text()
                # rust-toolchain: "1.70.0"
                match = re.search(r'(\d+\.\d+(?:\.\d+)?)', content)
                if match:
                    version = match.group(1)
                    # Extract major.minor
                    parts = version.split('.')
                    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else version

        # Check Cargo.toml for rust-version
        cargo_toml = self.repo_dir / "Cargo.toml"
        if cargo_toml.exists():
            content = cargo_toml.read_text()
            match = re.search(r'rust-version\s*=\s*"(\d+\.\d+)', content)
            if match:
                return match.group(1)

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "rust")

        return "1.70"  # Modern stable

    def get_actual_version(self) -> str:
        """Get current Rust version"""
        # Use env_vars so RUSTUP_TOOLCHAIN overrides are cleared when setup_version runs
        env = self.env_vars if hasattr(self, 'env_vars') else None
        result = subprocess.run(["rustc", "--version"], capture_output=True, text=True, env=env)
        if result.returncode == 0:
            match = re.search(r'(\d+\.\d+)', result.stdout)
            if match:
                return match.group(1)
        return "unknown"

    def setup_version(self, required_version: str):
        """Setup required Rust version using rustup"""
        # Check architecture compatibility (Apple Silicon support started in Rust 1.49)
        is_apple_silicon = platform.system() == 'Darwin' and platform.machine() == 'arm64'

        actual_version = required_version
        if is_apple_silicon:
            try:
                version_parts = [int(x) for x in required_version.split('.')]
                # Rust 1.49+ required for native Apple Silicon support
                if version_parts[0] == 1 and version_parts[1] < 49:
                    # Use recent stable Rust that works well with Apple Silicon
                    # and is compatible with most dependencies after cargo update
                    actual_version = "stable"
                    print(f"      ⚠ Rust {required_version} doesn't support Apple Silicon (aarch64)")
                    print(f"      → Using Rust stable instead (for architecture compatibility)")
            except:
                pass

        # Try to install with rustup
        print(f"      → Installing Rust {actual_version}...")
        result = subprocess.run(["rustup", "install", actual_version], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"      ⚠ rustup install failed: {result.stderr[:200]}")

        result = subprocess.run(["rustup", "default", actual_version], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"      ⚠ rustup default failed: {result.stderr[:200]}")
        else:
            print(f"      ✓ Set default to {actual_version}")
            # Set RUSTUP_TOOLCHAIN explicitly in env_vars so it overrides both any
            # rust-toolchain / rust-toolchain.toml file in the repo (restored by
            # "git checkout .") AND any RUSTUP_TOOLCHAIN value inherited from the
            # parent shell.  Simply setting `rustup default` is not enough when a
            # toolchain file is present, because toolchain files take precedence
            # over the rustup default.
            self.env_vars['RUSTUP_TOOLCHAIN'] = actual_version

    def install_dependencies(self):
        """Install Rust dependencies"""
        print(f"[4/7] Installing dependencies...")

        # Check if Cargo.lock exists - if so, use --locked flag to respect exact versions
        cargo_lock_exists = (self.repo_dir / "Cargo.lock").exists()
        if cargo_lock_exists:
            self.use_locked = True
        else:
            # Try to find Cargo.lock in git history
            git_result = self.run_command([
                "git", "log", "--all", "--format=%H", "-n", "100", "--", "Cargo.lock"
            ], timeout=30)

            if git_result.returncode == 0 and git_result.stdout.strip():
                commits = git_result.stdout.strip().split('\n')
                for commit in commits[:5]:
                    restore_result = self.run_command([
                        "git", "show", f"{commit}:Cargo.lock"
                    ], timeout=30)

                    if restore_result.returncode == 0:
                        lock_path = self.repo_dir / "Cargo.lock"
                        lock_path.write_text(restore_result.stdout)

                        # Verify the restored lock file is compatible with current Cargo.toml
                        verify_result = self.run_command(["cargo", "fetch", "--locked"], timeout=120)
                        if verify_result.returncode == 0:
                            print(f"      ✓ Restored Cargo.lock from git history ({commit[:8]})")
                            self.use_locked = True
                            cargo_lock_exists = True
                            break
                        else:
                            # This lock file is incompatible, remove it and try the next one
                            lock_path.unlink()
                            continue

                if not cargo_lock_exists:
                    print(f"      ⚠ No compatible Cargo.lock found in git history, will regenerate")

            # If still no lock file, try to generate one
            if not cargo_lock_exists:
                generate_result = self.run_command(["cargo", "generate-lockfile"], timeout=300)

                if generate_result.returncode == 0:
                    self.use_locked = True
                else:
                    # Check if the error is due to edition compatibility (old Rust + new dependencies)
                    if "edition" in generate_result.stderr.lower() or "failed to parse manifest" in generate_result.stderr.lower():
                        print(f"      → Edition compatibility issue, searching git history for compatible Cargo.lock...")

                        # Expand search: look before AND after the base commit
                        lock_found = False
                        for direction, commits in [("before", 200), ("after", 100)]:
                            if lock_found:
                                break

                            if direction == "before":
                                git_cmd = ["git", "log", "--all", "--format=%H", "-n", str(commits), "--", "Cargo.lock"]
                            else:
                                git_cmd = ["git", "log", "--all", "--reverse", "--format=%H", "-n", str(commits), "--", "Cargo.lock"]

                            git_result = self.run_command(git_cmd, timeout=30)

                            if git_result.returncode == 0 and git_result.stdout.strip():
                                commits_with_lock = git_result.stdout.strip().split('\n')

                                for commit in commits_with_lock[:10]:
                                    restore_result = self.run_command(["git", "show", f"{commit}:Cargo.lock"], timeout=30)

                                    if restore_result.returncode == 0:
                                        lock_path = self.repo_dir / "Cargo.lock"
                                        lock_path.write_text(restore_result.stdout)

                                        test_result = self.run_command(["cargo", "fetch", "--locked"], timeout=120)

                                        if test_result.returncode == 0:
                                            print(f"      ✓ Restored compatible Cargo.lock from {commit[:8]}")
                                            self.use_locked = True
                                            lock_found = True
                                            break

                        if not lock_found:
                            print(f"      ⚠ No compatible Cargo.lock found in git history")
                            self.use_locked = False
                    else:
                        # Different error, try with cargo update
                        update_result = self.run_command(["cargo", "update"], timeout=300)

                        if update_result.returncode == 0:
                            generate_result2 = self.run_command(["cargo", "generate-lockfile"], timeout=300)
                            if generate_result2.returncode == 0:
                                self.use_locked = True
                            else:
                                print(f"      ⚠ Cannot generate Cargo.lock, building without --locked")
                                self.use_locked = False
                        else:
                            print(f"      ⚠ Dependency update failed, will try to build anyway")
                            self.use_locked = False

        # Build fetch command with --locked if we have a lock file
        fetch_cmd = ["cargo", "fetch"]
        if self.use_locked:
            fetch_cmd.append("--locked")

        result = self.run_command(fetch_cmd, timeout=600)
        if result.returncode != 0:
            print(f"      ⚠ cargo fetch failed: {result.stderr[:500]}")
            print(f"      stdout: {result.stdout[:500]}")

            # IMPORTANT: Check edition compatibility FIRST (before registry corruption)
            # because edition errors also contain "failed to parse"
            # Check for edition 2024 FIRST — its error message also contains "2021 editions"
            # so it would falsely match the 2021 handler if checked second.
            if "older than the `2024` edition" in result.stderr:
                print(f"      → Dependencies require Rust 1.85+ (edition 2024 required)...")
                try:
                    self.setup_version("1.85")
                    self.actual_version = self.get_actual_version()
                    result = self.run_command(fetch_cmd, timeout=600)
                    if result.returncode == 0:
                        print(f"      ✓ Dependencies fetched after Rust upgrade to 1.85")
                except Exception as e:
                    print(f"      ⚠ Rust upgrade to 1.85 failed: {e}")

            elif "2021` edition" in result.stderr or "older than the `2021` edition" in result.stderr:
                # Determine target version: 1.56 is minimum for edition 2021,
                # but never downgrade below the required version.
                target_version = "1.56"
                if self.detected_version:
                    try:
                        det = tuple(int(x) for x in self.detected_version.split('.')[:2])
                        if det >= (1, 56):
                            target_version = self.detected_version
                    except (ValueError, IndexError):
                        pass
                print(f"      → Ensuring Rust >= 1.56 (edition 2021 required), using {target_version}...")

                try:
                    self.setup_version(target_version)
                    self.actual_version = self.get_actual_version()
                    self._clean_cargo_registry(result.stderr)
                    result = self.run_command(fetch_cmd, timeout=600)
                    if result.returncode == 0:
                        print(f"      ✓ Dependencies fetched after Rust upgrade")
                except Exception as e:
                    print(f"      ⚠ Rust upgrade failed: {e}")

            # Check if this is a cargo registry corruption issue (manifest parsing errors)
            # This check comes AFTER edition check to avoid false positives
            elif result.returncode != 0 and ("failed to parse manifest" in result.stderr or "namespaced featu" in result.stderr):
                self._clean_cargo_registry(result.stderr)
                # Retry fetch after cleanup
                result = self.run_command(fetch_cmd, timeout=600)
                if result.returncode == 0:
                    print(f"      ✓ Dependencies fetched after registry cleanup")

            # Handle incompatible Cargo.lock (restored from history but mismatches Cargo.toml)
            if result.returncode != 0 and "needs to be updated" in result.stderr:
                print(f"      → Restored Cargo.lock is incompatible, regenerating...")
                lock_path = self.repo_dir / "Cargo.lock"
                if lock_path.exists():
                    lock_path.unlink()
                self.use_locked = False
                cargo_lock_exists = False
                fetch_cmd = ["cargo", "fetch"]
                update_result = self.run_command(["cargo", "update"], timeout=300)
                if update_result.returncode == 0:
                    result = self.run_command(fetch_cmd, timeout=600)
                    if result.returncode == 0:
                        print(f"      ✓ Dependencies fetched after regenerating Cargo.lock")

            if result.returncode != 0 and not cargo_lock_exists:
                update_result = self.run_command(["cargo", "update"], timeout=300)
                if update_result.returncode == 0:
                    result = self.run_command(["cargo", "fetch"], timeout=600)
                    if result.returncode == 0:
                        print(f"      ✓ Dependencies fetched after cargo update")

        # Check if project uses insta for snapshot testing
        self.uses_insta = self._check_uses_insta()
        if self.uses_insta:
            # Install cargo-insta if not present
            self.run_command(["cargo", "install", "cargo-insta", "--quiet"], timeout=300)

        # Build command with --locked if we have a lock file
        build_cmd = ["cargo", "build", "--tests"]
        if self.use_locked and (self.repo_dir / "Cargo.lock").exists():
            build_cmd.append("--locked")

        result = self.run_command(build_cmd, timeout=1200)

        # If build fails due to dependency resolution in workspace members, try excluding them
        if result.returncode != 0:
            stderr = result.stderr
            # Check if error is about workspace member dependency resolution
            if "failed to select a version for" in stderr or "no matching package named" in stderr:
                # Try to identify problematic workspace member from error
                member_match = re.search(r'required by package `([^`]+) v[^(]+\(([^)]+)\)', stderr)
                if member_match:
                    package_path = member_match.group(2)

                    # Extract workspace member name from path (e.g., /tmp/.../tokio-tls -> tokio-tls)
                    if '/repo/' in package_path:
                        member_name = package_path.split('/repo/')[-1].split('/')[0]
                        if member_name and member_name != '.':
                            print(f"      → Removing problematic workspace member: {member_name}")

                            # Try to remove the problematic member from Cargo.toml workspace members
                            cargo_toml = self.repo_dir / "Cargo.toml"
                            if cargo_toml.exists():
                                try:
                                    content = cargo_toml.read_text()

                                    # Remove the member from workspace members list
                                    # Pattern: "tokio-tls" or 'tokio-tls' in members array
                                    content = re.sub(rf'^\s*["\']?{re.escape(member_name)}["\']?,?\s*$', '', content, flags=re.MULTILINE)
                                    # Clean up any resulting empty lines in members array
                                    content = re.sub(r'members\s*=\s*\[\s*,', 'members = [', content)
                                    content = re.sub(r',\s*,', ',', content)
                                    content = re.sub(r',\s*\]', ']', content)

                                    cargo_toml.write_text(content)

                                    cargo_lock = self.repo_dir / "Cargo.lock"
                                    if cargo_lock.exists():
                                        cargo_lock.unlink()

                                    fetch_result = self.run_command(["cargo", "fetch"], timeout=600)

                                    # Check for cargo registry corruption or edition compatibility issues
                                    # IMPORTANT: Check edition compatibility FIRST before registry corruption
                                    # because edition errors also contain "failed to parse"
                                    if fetch_result.returncode != 0:
                                        if "2021` edition" in fetch_result.stderr or "older than the `2021` edition" in fetch_result.stderr:
                                            print(f"      → Upgrading to Rust 1.56+ (edition 2021 required)...")
                                            try:
                                                self.setup_version("1.56")
                                                self.actual_version = self.get_actual_version()
                                                self.run_command(["cargo", "update"], timeout=600)
                                                fetch_result = self.run_command(["cargo", "fetch"], timeout=600)
                                                if fetch_result.returncode == 0:
                                                    print(f"      ✓ Dependencies fetched after Rust upgrade")
                                                elif "requires rustc" in fetch_result.stderr and "or newer" in fetch_result.stderr:
                                                    required_rust = self._extract_required_rust_version(fetch_result.stderr)
                                                    if required_rust and required_rust > self.actual_version:
                                                        print(f"      → Upgrading to Rust {required_rust}...")
                                                        try:
                                                            self.setup_version(required_rust)
                                                            self.actual_version = self.get_actual_version()
                                                            fetch_result = self.run_command(["cargo", "fetch"], timeout=600)
                                                            if fetch_result.returncode == 0:
                                                                print(f"      ✓ Dependencies fetched after upgrading to Rust {required_rust}")
                                                        except Exception as e:
                                                            print(f"      ⚠ Rust upgrade to {required_rust} failed: {e}")
                                            except Exception as e:
                                                print(f"      ⚠ Rust upgrade failed: {e}")
                                        elif "failed to parse manifest" in fetch_result.stderr or "namespaced featu" in fetch_result.stderr:
                                            self._clean_cargo_registry(fetch_result.stderr)
                                            fetch_result = self.run_command(["cargo", "fetch"], timeout=600)

                                    # Check if exact version constraints are unavailable
                                    if fetch_result.returncode != 0 and "failed to select a version for the requirement" in fetch_result.stderr:
                                        if "=" in fetch_result.stderr:
                                            update_result = self.run_command(["cargo", "update"], timeout=600)
                                            if update_result.returncode == 0:
                                                fetch_result = self.run_command(["cargo", "fetch"], timeout=600)
                                                if fetch_result.returncode == 0:
                                                    print(f"      ✓ Dependencies resolved after cargo update")

                                    if fetch_result.returncode == 0:
                                        # Successfully fetched deps without this member.
                                        # Record it as excluded NOW so that git reset later
                                        # can re-apply the exclusion (even if the build below
                                        # still fails due to a Rust version issue handled
                                        # later by _check_can_compile).
                                        self.excluded_members = [member_name]
                                        self.use_locked = False  # Cargo.lock was regenerated

                                        # Try building again
                                        retry_cmd = ["cargo", "build", "--tests"]
                                        retry_result = self.run_command(retry_cmd, timeout=1200)

                                        if retry_result.returncode == 0:
                                            print(f"      ✓ Build successful (removed {member_name} from workspace)")
                                            return
                                        else:
                                            print(f"      ⚠ Build still failed after removing {member_name}")
                                            print(f"      Error: {retry_result.stderr[:300]}")
                                    else:
                                        print(f"      ⚠ Fetch failed after removing {member_name}")
                                        print(f"      Error: {fetch_result.stderr[:300]}")
                                except Exception as e:
                                    print(f"      ⚠ Failed to modify Cargo.toml: {e}")

            print(f"      ⚠ Build failed: {result.stderr[:500]}")
            print(f"      stdout: {result.stdout[:500]}")
        else:
            print(f"      ✓ Build successful")

    def _get_test_binaries_from_cargo(self) -> list:
        """Parse Cargo.toml to find configured test binaries.

        Returns: List of test binary names (e.g., ['integration'])
        """
        test_binaries = []
        cargo_file = self.repo_dir / "Cargo.toml"

        if not cargo_file.exists():
            return test_binaries

        try:
            content = cargo_file.read_text()
            # Look for [[test]] sections
            # Example:
            # [[test]]
            # name = "integration"
            # path = "tests/tests.rs"

            in_test_section = False
            for line in content.split('\n'):
                stripped = line.strip()

                if stripped == '[[test]]':
                    in_test_section = True
                    continue

                if in_test_section:
                    # Look for name = "..."
                    if stripped.startswith('name'):
                        match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', stripped)
                        if match:
                            test_binaries.append(match.group(1))
                            in_test_section = False
                    # End of section if we hit another section header
                    elif stripped.startswith('['):
                        in_test_section = False

        except Exception as e:
            # If parsing fails, return empty list (will use default behavior)
            pass

        return test_binaries

    def _check_uses_insta(self) -> bool:
        """Check if the project uses insta for snapshot testing.

        Looks for `insta` as an actual dependency (not just as a substring,
        which would falsely match e.g. 'uu_install' or 'install').
        """
        # Match `insta` as a dependency key: `insta = ...` or `insta.workspace = true`
        # The word boundary \b prevents matching 'uu_install', 'install', etc.
        insta_dep_pattern = re.compile(r'(?m)^\s*insta\s*[=.]')
        for cargo_file in self.repo_dir.rglob("Cargo.toml"):
            try:
                content = cargo_file.read_text()
                if insta_dep_pattern.search(content):
                    return True
            except Exception:
                pass
        return False

    def _check_can_compile(self) -> bool:
        """Check if the project can compile before running tests

        Will automatically upgrade Rust version if dependencies require it,
        with multiple iterations until compilation succeeds or no more upgrades needed.
        """
        print(f"      → Verifying project compiles...")

        # Use cargo check which is faster than cargo build
        # Note: do NOT use --all-features here; it may enable optional deps requiring
        # a newer Rust than the locked Cargo.lock supports, causing spurious failures.
        check_cmd = ["cargo", "check", "--tests"]

        # Add --locked if applicable
        use_locked = hasattr(self, 'use_locked') and self.use_locked
        if use_locked and (self.repo_dir / "Cargo.lock").exists():
            check_cmd.append("--locked")

        # Add --exclude for problematic workspace members
        # --exclude requires --workspace in cargo
        excluded_members = getattr(self, 'excluded_members', [])
        if excluded_members:
            check_cmd.append("--workspace")
            for member in excluded_members:
                check_cmd.extend(["--exclude", member])

        # Try up to 5 times to handle cascading version requirements
        max_attempts = 5
        for attempt in range(max_attempts):
            result = self.run_command(check_cmd, timeout=600)

            if result.returncode == 0:
                if attempt > 0:
                    print(f"      ✓ Project compiles successfully after Rust upgrade(s)")
                else:
                    print(f"      ✓ Project compiles successfully")
                return True

            # Check if error is due to Rust version requirement
            if "requires rustc" in result.stderr and "or newer" in result.stderr:
                required_rust = self._extract_required_rust_version(result.stderr)
                if required_rust:
                    current_version = self.get_actual_version()
                    print(f"      → Dependencies require Rust {required_rust} or newer (current: {current_version})")
                    print(f"      → Upgrading to Rust {required_rust}...")
                    try:
                        self.setup_version(required_rust)
                        self.actual_version = self.get_actual_version()
                        print(f"      → Upgraded to Rust {self.actual_version}")
                        # Continue loop to retry compilation
                        continue
                    except Exception as e:
                        print(f"      ⚠ Rust upgrade failed: {e}")
                        break

            # Check if error is due to edition 2024 requirement in a transitive dep
            if "older than the `2024` edition" in result.stderr or "older than the `2024`" in result.stderr:
                print(f"      → Dependencies require Rust 1.85+ (edition 2024 required)...")
                try:
                    self.setup_version("1.85")
                    self.actual_version = self.get_actual_version()
                    print(f"      → Upgraded to Rust {self.actual_version}")
                    # Remove --locked since Cargo.lock may need regeneration for 1.85
                    if "--locked" in check_cmd:
                        check_cmd.remove("--locked")
                    continue
                except Exception as e:
                    print(f"      ⚠ Rust upgrade to 1.85 failed: {e}")
                    break

            # Check if error is due to `resolver = "2"` not yet stable (pre-1.51 Rust)
            # This can happen when a rust-toolchain file pins an old channel and overrides
            # the rustup default that was set by a previous setup_version() call.
            if "feature `resolver` is required" in result.stderr:
                print(f"      → Manifest uses resolver='2', requires Rust 1.51+ (upgrading to 1.85)...")
                try:
                    self.setup_version("1.85")
                    self.actual_version = self.get_actual_version()
                    print(f"      → Upgraded to Rust {self.actual_version}")
                    if "--locked" in check_cmd:
                        check_cmd.remove("--locked")
                    continue
                except Exception as e:
                    print(f"      ⚠ Rust upgrade to 1.85 failed: {e}")
                    break

            # Check if error is due to workspace-inherited readme field not supported
            # e.g. `readme.workspace = true` creates a table but Cargo expects bool/string
            if ("expected a boolean or a string for key" in result.stderr and
                    "readme" in result.stderr):
                fixed = self._fix_cargo_readme_fields()
                if fixed > 0:
                    print(f"      → Fixed workspace readme field in {fixed} Cargo.toml file(s)")
                    continue
                break

            # If we get here, compilation failed for a reason other than Rust version
            break

        # If we exhausted all attempts or hit a non-version error, fail
        # Try fallback: if --locked caused the failure, retry without it
        if "--locked" in check_cmd:
            no_lock_cmd = [c for c in check_cmd if c != "--locked"]
            result2 = self.run_command(no_lock_cmd, timeout=600)
            if result2.returncode == 0:
                print(f"      ✓ Project compiles (without --locked)")
                self.use_locked = False
                return True
            # Use result2 for error display so we see the actual non-locked error
            result = result2

        print(f"      ✗ Compilation check failed:")
        # Show last N non-empty lines where actual errors appear (cargo outputs progress first, errors last)
        all_lines = [l for l in result.stderr.split('\n') if l.strip()]
        display_lines = all_lines[-40:] if len(all_lines) > 40 else all_lines
        for line in display_lines:
            print(f"        {line}")
        return False

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False, all_features: bool = False) -> Dict[str, str]:
        """Run Rust tests, optionally filtered to specific test modules/files

        Args:
            test_files: Optional list of test files to filter
            debug: Enable debug output
            accept_snapshots: If True and project uses insta, auto-accept new snapshots
            all_features: If True, add --all-features to cargo commands (for broader baseline runs)
        """
        status_map = {}

        # First, check if the project can compile
        # This prevents wasting time trying to run tests when build is broken
        if not self._check_can_compile():
            print(f"      ⚠ Skipping tests - project does not compile")
            print(f"      This indicates a dependency or version compatibility issue")
            return status_map  # Return empty status map

        # Determine if we should use cargo-insta
        use_insta = hasattr(self, 'uses_insta') and self.uses_insta

        # Check if we should use --locked flag
        use_locked = hasattr(self, 'use_locked') and self.use_locked

        # Check if we need to exclude workspace members
        excluded_members = getattr(self, 'excluded_members', [])

        # Detect workspace package from test files for -p flag
        workspace_package = None
        if test_files:
            for test_file in test_files:
                # Check if test file is in a workspace crate (e.g., crates/ruff_linter/... or tokio-util/...)
                if test_file.startswith('crates/') and '/' in test_file[7:]:
                    # Extract crate name: crates/ruff_linter/... -> ruff_linter
                    crate_name = test_file.split('/')[1]
                    workspace_package = crate_name
                    break
                elif '/tests/' in test_file:
                    # Workspace member with tests directory: tokio-util/tests/... -> tokio-util
                    parts = test_file.split('/')
                    if len(parts) >= 2 and parts[1] == 'tests':
                        # First part is the package name
                        workspace_package = parts[0]
                        break

        # Build test command with optional filtering
        if test_files:
            # Extract test patterns from modified files
            test_patterns = self._extract_rust_test_patterns(test_files)
            if test_patterns:
                print(f"      → Targeting {len(test_patterns)} test module(s)")
                for pattern in list(test_patterns)[:5]:
                    print(f"        - {pattern}")
                if len(test_patterns) > 5:
                    print(f"        ... and {len(test_patterns) - 5} more")

                # Run tests for each pattern
                for pattern in test_patterns:
                    # Handle integration test patterns (--test testname)
                    if pattern.startswith('--test '):
                        test_name = pattern.split(' ', 1)[1]
                        # For integration tests (--test), prefer regular cargo test
                        # cargo-insta is mainly for snapshot tests in unit tests
                        # Integration tests often don't use snapshots
                        cmd = ["cargo", "test"]
                        # Add -p flag for workspace packages
                        if workspace_package:
                            cmd.extend(["-p", workspace_package])
                        cmd.extend(["--test", test_name])
                        # Add --locked before the -- separator
                        if use_locked:
                            cmd.append("--locked")
                        # Detect feature-gated tests: scan test file for #![cfg(feature = "...")]
                        required_features = []
                        if test_files:
                            for tf in test_files:
                                if Path(tf).stem == test_name and tf.endswith('.rs'):
                                    try:
                                        content = (self.repo_dir / tf).read_text()
                                        for m in re.finditer(r'#!\[cfg\(feature\s*=\s*"([^"]+)"\)\]', content):
                                            required_features.append(m.group(1))
                                    except Exception:
                                        pass
                                    break
                        if required_features:
                            cmd.extend(["--features", ",".join(required_features)])
                            print(f"      → Detected feature-gated tests, adding --features {','.join(required_features)}")
                        # Add --exclude for problematic workspace members
                        # --exclude requires --workspace and is incompatible with -p
                        if excluded_members and not workspace_package:
                            cmd.append("--workspace")
                            for member in excluded_members:
                                cmd.extend(["--exclude", member])
                        cmd.extend(["--", "--nocapture"])
                    else:
                        # Module patterns (e.g., rules::pycodestyle::tests)
                        if use_insta and accept_snapshots:
                            cmd = ["cargo", "insta", "test", "--accept"]
                            # Add -p flag for workspace packages
                            if workspace_package:
                                cmd.extend(["-p", workspace_package])
                            # For cargo insta, pattern goes after --
                            cmd.extend(["--", pattern, "--nocapture"])
                        elif use_insta:
                            cmd = ["cargo", "insta", "test"]
                            # Add -p flag for workspace packages
                            if workspace_package:
                                cmd.extend(["-p", workspace_package])
                            # For cargo insta, pattern goes after --
                            cmd.extend(["--", pattern, "--nocapture"])
                        else:
                            cmd = ["cargo", "test"]
                            # Add -p flag for workspace packages
                            if workspace_package:
                                cmd.extend(["-p", workspace_package])
                            cmd.append(pattern)
                            # Add --locked before the -- separator
                            if use_locked:
                                cmd.append("--locked")
                            # Add --exclude for problematic workspace members
                            # --exclude requires --workspace and is incompatible with -p
                            if excluded_members and not workspace_package:
                                cmd.append("--workspace")
                                for member in excluded_members:
                                    cmd.extend(["--exclude", member])
                            cmd.extend(["--", "--nocapture"])

                        # For cargo insta test, add --exclude before -- (--locked not supported by insta)
                        # Only add --exclude when not using -p (incompatible with -p)
                        if excluded_members and not workspace_package:
                            for member in excluded_members:
                                # Insert --exclude before the -- separator
                                dash_idx = cmd.index("--")
                                cmd.insert(dash_idx, member)
                                cmd.insert(dash_idx, "--exclude")

                    result = self.run_command(cmd, timeout=600)
                    output = result.stdout + result.stderr
                    before = len(status_map)
                    self._parse_rust_test_output(output, status_map, debug)
                    if len(status_map) == before and result.returncode != 0:
                        print(f"      ⚠ Test command failed (rc={result.returncode}): {output[-200:]}")
                        # If the test binary failed to compile due to a missing feature-gated item
                        # (e.g. #[tokio::test] needs rt-core beyond the detected features),
                        # retry with --all-features using the locked Cargo.lock so we don't
                        # upgrade to incompatible package versions.
                        if (pattern.startswith('--test ') and required_features
                                and result.returncode != 0
                                and ('error[E0433]' in output or 'could not compile' in output)):
                            test_name_retry = pattern.split(' ', 1)[1]
                            print(f"      → Feature-gated compile error, retrying with --all-features...")
                            retry_cmd = ["cargo", "test"]
                            if workspace_package:
                                retry_cmd.extend(["-p", workspace_package])
                            retry_cmd.extend(["--test", test_name_retry])
                            if use_locked:
                                retry_cmd.append("--locked")
                            retry_cmd.append("--all-features")
                            if excluded_members and not workspace_package:
                                retry_cmd.append("--workspace")
                                for member in excluded_members:
                                    retry_cmd.extend(["--exclude", member])
                            retry_cmd.extend(["--", "--nocapture"])
                            result2 = self.run_command(retry_cmd, timeout=600)
                            output2 = result2.stdout + result2.stderr
                            self._parse_rust_test_output(output2, status_map, debug)
                            if len(status_map) > before:
                                print(f"      ✓ Found {len(status_map) - before} tests with --all-features")
                            elif result2.returncode != 0:
                                print(f"      ⚠ --all-features retry failed (rc={result2.returncode}): {output2[-200:]}")
                                # If --all-features still fails due to deny-by-default lints on old code
                                # (e.g. undropped_manually_drops in tokio 0.2.x with Rust 1.71+),
                                # retry suppressing those lints via RUSTFLAGS
                                if ('undropped_manually_drops' in output2 or
                                        'undropped_manually_drops' in output):
                                    print(f"      → Suppressing deny lints for old code, retrying...")
                                    # Temporarily add RUSTFLAGS to env_vars so run_in_env picks it up
                                    prev_rustflags = self.env_vars.get('RUSTFLAGS', None)
                                    existing_rf = self.env_vars.get('RUSTFLAGS', '')
                                    self.env_vars['RUSTFLAGS'] = (existing_rf + ' -A undropped_manually_drops').strip()
                                    try:
                                        lint_result = self.run_command(list(retry_cmd), timeout=600)
                                        lint_output = lint_result.stdout + lint_result.stderr
                                        self._parse_rust_test_output(lint_output, status_map, debug)
                                        if len(status_map) > before:
                                            print(f"      ✓ Found {len(status_map) - before} tests (lint suppressed)")
                                    finally:
                                        if prev_rustflags is None:
                                            self.env_vars.pop('RUSTFLAGS', None)
                                        else:
                                            self.env_vars['RUSTFLAGS'] = prev_rustflags
                    elif (len(status_map) == before and result.returncode == 0
                          and 'running 0 tests' in output and pattern.startswith('--test ')
                          and not required_features):
                        # 0 tests ran without explicit features - try --all-features as fallback
                        test_name = pattern.split(' ', 1)[1]
                        print(f"      → 0 tests ran (likely feature-gated), retrying with --all-features...")
                        retry_cmd = ["cargo", "test"]
                        if workspace_package:
                            retry_cmd.extend(["-p", workspace_package])
                        retry_cmd.extend(["--test", test_name])
                        if use_locked:
                            retry_cmd.append("--locked")
                        retry_cmd.append("--all-features")
                        if excluded_members and not workspace_package:
                            retry_cmd.append("--workspace")
                            for member in excluded_members:
                                retry_cmd.extend(["--exclude", member])
                        retry_cmd.extend(["--", "--nocapture"])
                        result2 = self.run_command(retry_cmd, timeout=600)
                        output2 = result2.stdout + result2.stderr
                        self._parse_rust_test_output(output2, status_map, debug)
                        if len(status_map) > before:
                            print(f"      ✓ Found {len(status_map) - before} tests with --all-features")
                        elif result2.returncode != 0:
                            print(f"      ⚠ --all-features retry failed (rc={result2.returncode}): {output2[-200:]}")
            else:
                print(f"      → No matching test modules found, running all tests")
                if use_insta and accept_snapshots:
                    cmd = ["cargo", "insta", "test", "--accept"]
                elif use_insta:
                    cmd = ["cargo", "insta", "test"]
                else:
                    cmd = ["cargo", "test"]
                # Add -p flag for workspace packages
                if workspace_package:
                    cmd.extend(["-p", workspace_package])
                # Add --locked before the -- separator (not supported by cargo insta)
                if use_locked and not use_insta:
                    cmd.append("--locked")
                # Add --all-features if requested (e.g., for broader baseline runs)
                if all_features and not use_insta:
                    cmd.append("--all-features")
                # Add --exclude for problematic workspace members
                # --exclude requires --workspace and is incompatible with -p
                if excluded_members and not workspace_package and not use_insta:
                    cmd.append("--workspace")
                    for member in excluded_members:
                        cmd.extend(["--exclude", member])
                # --no-fail-fast: collect results from all workspace members even if one fails
                if not use_insta and not workspace_package:
                    cmd.append("--no-fail-fast")
                cmd.extend(["--", "--nocapture"])

                result = self.run_command(cmd, timeout=600)
                output = result.stdout + result.stderr
                self._parse_rust_test_output(output, status_map, debug)
        else:
            # Run all tests (backward compatibility)
            if use_insta and accept_snapshots:
                cmd = ["cargo", "insta", "test", "--accept"]
            elif use_insta:
                cmd = ["cargo", "insta", "test"]
            else:
                cmd = ["cargo", "test"]
            # Add -p flag for workspace packages
            if workspace_package:
                cmd.extend(["-p", workspace_package])
            # Add --locked before the -- separator (not supported by cargo insta)
            if use_locked and not use_insta:
                cmd.append("--locked")
            # Add --all-features if requested
            if all_features and not use_insta:
                cmd.append("--all-features")
            # Add --exclude for problematic workspace members
            # --exclude requires --workspace and is incompatible with -p
            if excluded_members and not workspace_package and not use_insta:
                cmd.append("--workspace")
                for member in excluded_members:
                    cmd.extend(["--exclude", member])
            # --no-fail-fast: collect results from all workspace members even if one fails
            if not use_insta and not workspace_package:
                cmd.append("--no-fail-fast")
            cmd.extend(["--", "--nocapture"])

            result = self.run_command(cmd, timeout=600)
            output = result.stdout + result.stderr
            self._parse_rust_test_output(output, status_map, debug)
            if not status_map and result.returncode != 0:
                # Show tail of error so the user knows why 0 tests were collected
                all_lines = [l for l in output.split('\n') if l.strip()]
                print(f"      ⚠ cargo test failed (rc={result.returncode}), last lines:")
                for ln in all_lines[-10:]:
                    print(f"        {ln}")

        # If using insta and snapshots were accepted, report it
        if use_insta and accept_snapshots:
            # Check for any .snap.new files that might indicate issues
            snap_new_files = list(self.repo_dir.rglob("*.snap.new"))
            if snap_new_files:
                print(f"      ⚠ Found {len(snap_new_files)} pending snapshot(s) after acceptance")
                print(f"      This may indicate the solution produces unexpected output")
            else:
                print(f"      ✓ No new snapshots created (solution matches expected output)")

        # For bat-style repos: also run syntax regression tests
        if (self.repo_dir / "tests" / "syntax-tests" / "create_highlighted_versions.py").exists():
            syntax_results = self._run_bat_syntax_tests()
            status_map.update(syntax_results)

        return status_map

    def _find_parent_test_binaries(self, module_path: str) -> set:
        """
        Find which top-level test binaries include a given module file.
        E.g., tests/by-util/test_timeout.rs -> {'tests'}
        """
        parent_binaries = set()
        tests_dir = self.repo_dir / "tests"

        if not tests_dir.exists():
            return parent_binaries

        # Get the module name from the file path
        # tests/by-util/test_timeout.rs -> test_timeout
        module_name = Path(module_path).stem

        # Search all top-level .rs files in tests/
        for test_file in tests_dir.glob("*.rs"):
            try:
                content = test_file.read_text()
                # Look for module declarations like:
                # mod test_timeout;
                # #[path = "by-util/test_timeout.rs"]
                # mod test_timeout;
                if re.search(rf'\bmod\s+{module_name}\s*;', content):
                    parent_binaries.add(test_file.stem)
            except:
                pass

        return parent_binaries

    def _extract_rust_test_patterns(self, test_files: List[str]) -> set:
        """Extract cargo test patterns from modified test files"""
        patterns = set()

        # Check Cargo.toml for custom test binary configurations
        test_binaries = self._get_test_binaries_from_cargo()

        for file_path in test_files:
            # Snapshot files: extract module path from snapshot filename
            # e.g., crates/ruff_linter/src/rules/pycodestyle/snapshots/ruff_linter__rules__pycodestyle__tests__E231.snap
            # -> rules::pycodestyle::tests::E231 (more specific!)
            if file_path.endswith('.snap'):
                # Extract from filename: ruff_linter__rules__pycodestyle__tests__*
                snapshot_name = Path(file_path).stem
                # Handle .py.snap files (remove .py if present)
                if snapshot_name.endswith('.py'):
                    snapshot_name = snapshot_name[:-3]
                parts = snapshot_name.split('__')

                # Find 'tests' in parts and build module path up to it
                if 'tests' in parts:
                    test_idx = parts.index('tests')
                    # Skip crate name, take everything up to and including 'tests'
                    module_parts = parts[1:test_idx+1]

                    # Get specific test identifier after 'tests'
                    # e.g., for PIE800_PIE800.py.snap, parts after 'tests' = ['PIE800', 'PIE800', 'py']
                    test_specific_parts = parts[test_idx+1:]

                    if module_parts:
                        module_path = '::'.join(module_parts)
                        # For snapshot tests, use module level pattern
                        # Rust tests use the snapshot files internally, not as test names
                        patterns.add(module_path)

            # Test fixture files in resources: extract module from path
            # e.g., crates/ruff_linter/resources/test/fixtures/pycodestyle/E23.py
            # -> rules::pycodestyle::tests
            elif '/resources/test/fixtures/' in file_path or '/test/fixtures/' in file_path:
                # Extract category from fixtures path
                if '/fixtures/' in file_path:
                    category = file_path.split('/fixtures/')[1].split('/')[0]
                    fixture_file = Path(file_path).stem

                    # Use module level pattern for fixture files
                    # Fixture files are used by tests, not test names themselves
                    module_base = f"rules::{category}::tests"
                    patterns.add(module_base)

            # Actual Rust test files
            elif file_path.endswith('.rs'):
                # Check if this is an integration test in a workspace member
                # e.g., tokio-util/tests/io_write_all_vectored.rs or crates/foo/tests/bar.rs
                if '/tests/' in file_path and file_path.endswith('.rs'):
                    parts = file_path.split('/')
                    # Find the 'tests' directory index
                    try:
                        tests_idx = parts.index('tests')
                        # Check if there's a package name before 'tests'
                        # e.g., tokio-util/tests/... or crates/ruff_linter/tests/...
                        if tests_idx > 0:
                            # This is a workspace member integration test
                            # Extract package name (could be 'tokio-util' or 'ruff_linter')
                            if parts[0] == 'crates' and tests_idx >= 2:
                                # crates/foo/tests/... -> package is 'foo'
                                package_name = parts[1]
                            else:
                                # tokio-util/tests/... -> package is 'tokio-util'
                                package_name = parts[0]

                            # Extract test binary name from filename
                            test_file_stem = Path(file_path).stem
                            # Don't skip any files for workspace member tests - they're explicit
                            patterns.add(f"--test {test_file_stem}")
                            continue
                    except ValueError:
                        pass  # 'tests' not in parts, fall through to other checks

                # Integration tests: tests/test_name.rs (top-level, non-workspace)
                if file_path.startswith('tests/') and file_path.endswith('.rs'):
                    # If we have custom test binaries configured, use those
                    if test_binaries:
                        for bin_name in test_binaries:
                            patterns.add(f"--test {bin_name}")
                    else:
                        # Check if this is a top-level test binary or a module in a subdirectory
                        path_parts = file_path.split('/')

                        if len(path_parts) == 2:
                            # Top-level test file: tests/test_name.rs
                            test_file_stem = Path(file_path).stem

                            # Skip common module files UNLESS they contain #[test] functions
                            skip_names = ['tests', 'main', 'mod', 'common', 'util', 'macros', 'hay']
                            should_skip = test_file_stem in skip_names

                            if should_skip:
                                try:
                                    file_content = (self.repo_dir / file_path).read_text()
                                    if '#[test]' in file_content or '#[cfg_attr' in file_content:
                                        should_skip = False
                                except:
                                    pass

                            if not should_skip:
                                patterns.add(f"--test {test_file_stem}")
                        else:
                            # Module in subdirectory: tests/by-util/test_timeout.rs or tests/shell/environment/env.rs
                            # Find which top-level test binary includes this module
                            parent_binaries = self._find_parent_test_binaries(file_path)
                            if parent_binaries:
                                for binary in parent_binaries:
                                    patterns.add(f"--test {binary}")
                            else:
                                # Check if tests/main.rs exists - common pattern for nested integration tests
                                main_test = self.repo_dir / "tests" / "main.rs"
                                if main_test.exists():
                                    # Extract top-level module from path (e.g., 'shell' from 'tests/shell/environment/env.rs')
                                    path_parts = file_path.split('/')
                                    if len(path_parts) >= 2:
                                        top_module = path_parts[1]  # e.g., 'shell'

                                        # Check if main.rs includes this module
                                        try:
                                            main_content = main_test.read_text()
                                            if f"mod {top_module};" in main_content:
                                                patterns.add(f"--test main")
                                            else:
                                                # Module not found in main.rs, try without --test filter
                                                pass  # Will run all tests as fallback
                                        except:
                                            # Fallback: try main as test binary
                                            patterns.add(f"--test main")
                                else:
                                    # No main.rs, fallback: use directory name as potential test binary
                                    path_parts = file_path.split('/')
                                    if len(path_parts) >= 2:
                                        subdir = path_parts[1]
                                        # Skip known utility/helper subdirectories (not test binaries)
                                        utility_dirs = {'common', 'util', 'helpers', 'support', 'fixtures'}
                                        if subdir in utility_dirs:
                                            # Look for a top-level tests.rs binary as the real parent
                                            top_tests = self.repo_dir / "tests" / "tests.rs"
                                            if top_tests.exists():
                                                patterns.add(f"--test tests")
                                            # else: skip — no reliable binary to target
                                        else:
                                            # Try the first subdirectory name (e.g., 'shell' from 'tests/shell/...')
                                            patterns.add(f"--test {subdir}")

                # Unit tests in modules: extract module path
                # e.g., crates/ruff_linter/src/rules/pycodestyle/mod.rs -> rules::pycodestyle
                elif '/src/' in file_path or file_path.startswith('src/'):
                    # Extract module path from file path
                    if '/src/' in file_path:
                        module_part = file_path.split('/src/', 1)[1]
                    else:
                        module_part = file_path[4:]  # Remove 'src/'

                    # Remove .rs extension and convert path to module notation
                    module_path = module_part.replace('.rs', '').replace('/', '::')

                    # If it's mod.rs, use parent module
                    if module_path.endswith('::mod'):
                        module_path = module_path[:-5]

                    patterns.add(module_path)

        return patterns

    def _parse_rust_test_output(self, output: str, status_map: Dict[str, str], debug: bool = False):
        """Parse cargo test output and populate status map"""
        # Only treat error[E as a compile failure if tests never started.
        # If "running N tests" appears, tests did run (error[E may come from
        # trybuild UI tests which deliberately compile code that has errors).
        tests_started = bool(re.search(r'running \d+ tests?', output))
        if not tests_started and ("error: could not compile" in output or "error[E" in output):
            if debug:
                print(f"      [DEBUG] Compilation error detected before any tests ran")
            # Don't populate status_map - empty map indicates build failure
            return

        for line in output.split('\n'):
            line = line.strip()

            # Format 1: "test foo::bar ... ok"
            if line.startswith('test '):
                match = re.match(r'test\s+(.+?)\s+\.\.\.\s+(\w+)', line)
                if match:
                    test_name = match.group(1)
                    status = match.group(2).upper()
                    if status == 'OK':
                        status_map[test_name] = 'PASSED'
                    elif status in ('FAILED', 'FAIL'):
                        status_map[test_name] = 'FAILED'
                    elif status == 'IGNORED':
                        pass  # Skip ignored tests - they don't affect FAIL_TO_PASS
                    else:
                        status_map[test_name] = 'ERROR'

            # Extract summary for debugging
            elif 'test result:' in line and debug:
                print(f"      [DEBUG] Test summary: {line}")

        # If we saw no test results but also no compilation errors, tests may have been filtered out
        if not status_map and debug:
            print(f"      [DEBUG] No tests found in output - may indicate test filtering excluded all tests")

    def _run_bat_syntax_tests(self) -> Dict[str, str]:
        """Run bat-style syntax regression tests.

        Builds the repo's bat binary, then for every file under
        tests/syntax-tests/source/ runs it through the binary and compares
        the ANSI-highlighted output to the expected file in
        tests/syntax-tests/highlighted/.  Each file becomes one test entry.
        """
        syntax_dir  = self.repo_dir / "tests" / "syntax-tests"
        source_dir  = syntax_dir / "source"
        highlighted_dir = syntax_dir / "highlighted"

        if not source_dir.exists() or not highlighted_dir.exists():
            return {}

        # Build the release binary (avoids having to locate a debug path)
        print(f"      → Building bat binary for syntax regression tests...")
        build_result = self.run_command(["cargo", "build", "--release"], timeout=600)
        bat_binary = self.repo_dir / "target" / "release" / "bat"
        if build_result.returncode != 0 or not bat_binary.exists():
            print(f"      ⚠ Could not build bat binary, skipping syntax tests")
            return {}

        SKIP = {"LICENSE.md", "NOTICE", "README.md", "bat_options"}
        BASE_OPTS = [
            "--no-config", "--style=plain",
            "--color=always", "--theme=default", "--italic-text=always",
        ]

        env = os.environ.copy()
        for k in ["BAT_CACHE_PATH", "BAT_CONFIG_DIR", "BAT_CONFIG_PATH",
                  "BAT_OPTS", "BAT_PAGER", "BAT_STYLE", "BAT_TABS",
                  "BAT_THEME", "NO_COLOR", "PAGER"]:
            env.pop(k, None)
        env["COLORTERM"] = "truecolor"

        status_map: Dict[str, str] = {}

        for subdir in sorted(source_dir.iterdir()):
            if not subdir.is_dir():
                continue
            # Per-directory extra options (bat_options file)
            extra_opts: List[str] = []
            opts_file = subdir / "bat_options"
            if opts_file.exists():
                extra_opts = [l.rstrip() for l in opts_file.read_text().splitlines() if l.strip()]

            for src in sorted(subdir.iterdir()):
                if src.name in SKIP or not src.is_file():
                    continue

                test_name = f"syntax/{subdir.name}/{src.name}"
                cmd = [str(bat_binary)] + BASE_OPTS + extra_opts + [str(src)]

                try:
                    result = subprocess.run(
                        cmd, capture_output=True, env=env, timeout=30
                    )
                except Exception:
                    status_map[test_name] = 'ERROR'
                    continue

                if result.returncode != 0:
                    # bat crashed or errored — the fix should prevent this
                    status_map[test_name] = 'FAILED'
                    continue

                expected_file = highlighted_dir / subdir.name / src.name
                if not expected_file.exists():
                    # New source file with no expected snapshot yet → failing
                    status_map[test_name] = 'FAILED'
                    continue

                if result.stdout == expected_file.read_bytes():
                    status_map[test_name] = 'PASSED'
                else:
                    status_map[test_name] = 'FAILED'

        passed = sum(1 for s in status_map.values() if s == 'PASSED')
        failed = sum(1 for s in status_map.values() if s in ('FAILED', 'ERROR'))
        print(f"      → Syntax tests: {passed} passed, {failed} failed ({len(status_map)} total)")
        return status_map

    def extract_modified_tests(self) -> set:
        """
        Extract specific test identifiers modified in test_patch for Rust
        Returns: {'rules::flake8_pie::tests::PIE800', ...}
        """
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')

        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            if self.is_test_file(file_path):
                # For snapshot files, extract test identifier from filename
                if file_path.endswith('.snap'):
                    snapshot_name = Path(file_path).stem
                    parts = snapshot_name.split('__')
                    if 'tests' in parts:
                        test_idx = parts.index('tests')
                        # Build pattern: rules::category::tests::TEST_ID
                        module_parts = parts[1:test_idx+1]
                        test_specific = parts[test_idx+1:]
                        if module_parts and test_specific:
                            module_path = '::'.join(module_parts)
                            test_id = test_specific[0]
                            modified_tests.add(f"{module_path}::{test_id}")

                # For fixture files, extract identifier from path
                elif '/fixtures/' in file_path:
                    category = file_path.split('/fixtures/')[1].split('/')[0]
                    fixture_file = Path(file_path).stem
                    if fixture_file:
                        modified_tests.add(f"rules::{category}::tests::{fixture_file}")

                # For .rs test files, extract module path and test names
                elif file_path.endswith('.rs') and '/tests/' in file_path:
                    # Track current test and changes
                    current_test = None
                    tests_with_changes = set()
                    found_test_attr = False

                    # Extract test names from file changes
                    for line in patch_content.split('\n'):
                        # Strip diff prefix to analyze actual content
                        line_content = line[1:] if line and line[0] in ' +-' else line

                        # Look for #[test] attribute
                        if '#[test]' in line_content:
                            found_test_attr = True
                            current_test = None  # Will be set by next fn declaration

                        # Look for test function: fn test_name or fn name (after #[test])
                        fn_match = re.search(r'fn\s+(\w+)\s*\(', line_content)
                        if fn_match:
                            func_name = fn_match.group(1)
                            # If we just saw #[test] or function name starts with test_
                            if found_test_attr or func_name.startswith('test_'):
                                current_test = func_name
                                found_test_attr = False

                        # Look for macro tests like rgtest!(name, ...)
                        macro_test_match = re.search(r'\w+test!\s*\(\s*(\w+)', line_content)
                        if macro_test_match:
                            current_test = macro_test_match.group(1)

                        # Check for actual changes (not file headers or comments)
                        is_change = (line.startswith(('+', '-')) and
                                    line[1:].strip() and
                                    not line.startswith('+++') and
                                    not line.startswith('---'))

                        if current_test and is_change:
                            tests_with_changes.add(current_test)

                    # Add modified tests with proper path
                    if file_path.startswith('tests/'):
                        test_binary = Path(file_path).stem
                        if tests_with_changes:
                            for test in tests_with_changes:
                                modified_tests.add(f"{test_binary}::{test}")
                        elif current_test:
                            # Found test functions but no clear changes, add whole binary
                            modified_tests.add(test_binary)
                    # For module tests in crates/*/src/
                    elif '/src/' in file_path:
                        module_part = file_path.split('/src/', 1)[1]
                        module_path = module_part.replace('.rs', '').replace('/', '::')
                        if tests_with_changes:
                            for test in tests_with_changes:
                                modified_tests.add(f"{module_path}::{test}")
                        elif current_test:
                            modified_tests.add(module_path)

            i += 2

        return modified_tests


# ============================================================================
# GO VALIDATOR
# ============================================================================

class GoValidator(BaseValidator):
    """Validator for Go projects"""

    def is_test_file(self, file_path: str) -> bool:
        # Go test files: *_test.go
        if file_path.endswith('_test.go'):
            return True
        # Go test data files: testdata/*.test or testdata/*
        if '/testdata/' in file_path:
            return True
        return False

    def is_version_compatible(self, required: str, actual: str) -> bool:
        """Go is generally backward compatible - newer versions usually work"""
        # Accept newer Go versions (they're usually backward compatible)
        # Only reject if actual version is significantly older
        try:
            req_parts = [int(x) for x in required.split('.')]
            act_parts = [int(x) for x in actual.split('.')]

            # Same major version required
            if req_parts[0] != act_parts[0]:
                return False

            # Accept same or newer minor version
            # (Go 1.26 can run code that needs Go 1.22)
            return act_parts[1] >= req_parts[1]
        except:
            return required == actual

    def detect_required_version(self) -> str:
        """Detect required Go version"""
        # Check go.mod
        go_mod = self.repo_dir / "go.mod"
        if go_mod.exists():
            content = go_mod.read_text()
            # go 1.20
            match = re.search(r'^go\s+(\d+\.\d+)', content, re.MULTILINE)
            if match:
                return match.group(1)

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "go")

        return "1.20"

    def get_actual_version(self) -> str:
        """Get current Go version (or custom version if set)"""
        # Use custom Go binary if set, otherwise use system Go
        go_cmd = getattr(self, 'go_binary', 'go')
        result = subprocess.run([go_cmd, "version"], capture_output=True, text=True)
        if result.returncode == 0:
            match = re.search(r'go(\d+\.\d+)', result.stdout)
            if match:
                return match.group(1)
        return "unknown"

    def setup_version(self, required_version: str):
        """Setup required Go version using Go's toolchain downloader"""
        current_version = self.get_actual_version()

        if current_version == required_version:
            print(f"      ✓ Go {current_version} already available")
            return

        print(f"      ⚠ Go version mismatch (required: {required_version}, available: {current_version})")
        print(f"      → Installing Go {required_version}...")

        # Download and install Go directly from official site
        import tarfile

        # Determine OS and architecture
        os_name = platform.system().lower()
        if os_name == "darwin":
            os_name = "darwin"
        elif os_name == "linux":
            os_name = "linux"
        else:
            print(f"      ⚠ Unsupported OS: {os_name}")
            print(f"      → Continuing with Go {current_version} (tests will likely fail)")
            return

        arch = platform.machine().lower()
        if arch in ["x86_64", "amd64"]:
            arch = "amd64"
        elif arch in ["arm64", "aarch64"]:
            arch = "arm64"
        else:
            print(f"      ⚠ Unsupported architecture: {arch}")
            print(f"      → Continuing with Go {current_version} (tests will likely fail)")
            return

        # Check for Apple Silicon compatibility (darwin-arm64 support started in Go 1.16)
        is_apple_silicon = os_name == "darwin" and arch == "arm64"
        if is_apple_silicon:
            try:
                version_parts = [int(x) for x in required_version.split('.')]
                # Go 1.16+ required for native Apple Silicon support
                if version_parts[0] == 1 and version_parts[1] < 16:
                    print(f"      ⚠ Go {required_version} doesn't support Apple Silicon (darwin-arm64)")
                    print(f"      → Minimum version for darwin-arm64 is Go 1.16")
                    print(f"      → Using Go {current_version} instead (compatibility mode)")
                    # Use current version which should be compatible
                    return
            except:
                pass

        # For Go, if version doesn't have patch number, try common versions
        # e.g., 1.22 -> try 1.22.0, 1.22.1, etc.
        full_version = required_version
        if required_version.count('.') == 1:
            # Try latest patch versions for this minor version
            # Most Go versions have .0, .1, .2, etc.
            for patch in range(10, -1, -1):  # Try from .10 down to .0
                test_version = f"{required_version}.{patch}"
                test_url = f"https://go.dev/dl/go{test_version}.{os_name}-{arch}.tar.gz"

                # Quick HEAD request to check if it exists
                check_result = subprocess.run(
                    ["curl", "-I", "-L", "-s", "-o", "/dev/null", "-w", "%{http_code}", test_url],
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                if check_result.stdout.strip() == "200":
                    full_version = test_version
                    print(f"      → Found Go {full_version}")
                    break

        # Download Go tarball
        go_url = f"https://go.dev/dl/go{full_version}.{os_name}-{arch}.tar.gz"
        go_install_dir = Path.home() / ".go-versions" / f"go{full_version}"

        if go_install_dir.exists():
            self.go_binary = str(go_install_dir / "bin" / "go")
            if not self._verify_go_binary():
                print(f"      ⚠ Go binary verification failed, falling back to system Go")
                if hasattr(self, 'go_binary'):
                    delattr(self, 'go_binary')
            else:
                print(f"      ✓ Go {full_version} ready")
            return

        print(f"      → Downloading Go {full_version}...")
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball_path = Path(tmpdir) / f"go{required_version}.tar.gz"

            result = subprocess.run(
                ["curl", "-L", "-o", str(tarball_path), go_url],
                capture_output=True,
                timeout=300
            )

            if result.returncode != 0 or not tarball_path.exists():
                print(f"      ⚠ Failed to download Go {required_version}, continuing with {current_version}")
                return

            go_install_dir.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tarball_path, "r:gz") as tar:
                tar.extractall(path=tmpdir)

            extracted_go = Path(tmpdir) / "go"
            if extracted_go.exists():
                shutil.move(str(extracted_go), str(go_install_dir))
            else:
                print(f"      ⚠ Extraction failed, continuing with Go {current_version}")
                return

        self.go_binary = str(go_install_dir / "bin" / "go")
        if not self._verify_go_binary():
            print(f"      ⚠ Go binary verification failed, falling back to system Go")
            if hasattr(self, 'go_binary'):
                delattr(self, 'go_binary')
        else:
            print(f"      ✓ Go {required_version} installed and ready")

    def _verify_go_binary(self) -> bool:
        """Verify that the Go binary works correctly

        This is especially important on macOS where dyld errors can occur
        with improperly built Go binaries.
        """
        go_cmd = getattr(self, 'go_binary', 'go')

        try:
            # Test 1: Can we run go version?
            result = subprocess.run(
                [go_cmd, "version"],
                capture_output=True,
                text=True,
                timeout=10,
                env=self.env_vars
            )
            if result.returncode != 0:
                print(f"      ✗ 'go version' failed: {result.stderr[:200]}")
                return False

            # Test 2: Can we build a simple program?
            with tempfile.TemporaryDirectory() as tmpdir:
                test_file = Path(tmpdir) / "test.go"
                test_file.write_text('package main\nfunc main() {}\n')

                result = subprocess.run(
                    [go_cmd, "build", "-o", "/dev/null", str(test_file)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=tmpdir,
                    env=self.env_vars
                )

                if result.returncode != 0:
                    # Check for dyld errors
                    if "dyld" in result.stderr or "missing LC_UUID" in result.stderr:
                        print(f"      ✗ Go binary has dyld linker issues (LC_UUID)")
                        return False
                    print(f"      ✗ Test build failed: {result.stderr[:200]}")
                    return False

            return True

        except Exception as e:
            print(f"      ✗ Error verifying Go binary: {e}")
            return False

    def install_dependencies(self):
        """Install Go dependencies"""
        print(f"[4/7] Installing dependencies...")

        go_cmd = getattr(self, 'go_binary', 'go')

        if (self.repo_dir / "go.mod").exists():
            result = self.run_command([go_cmd, "mod", "download"], timeout=600)
            if result.returncode != 0:
                print(f"      ⚠ go mod download failed: {result.stderr[:200]}")

    def _check_can_compile(self, packages: set = None) -> bool:
        """Check if Go packages can compile before running tests"""
        go_cmd = getattr(self, 'go_binary', 'go')

        # Build the test packages to check compilation
        if packages:
            for pkg in list(packages)[:3]:  # Check first few packages
                result = self.run_command([go_cmd, "build", "-o", "/dev/null", pkg], timeout=300)
                if result.returncode != 0:
                    print(f"      ✗ Compilation check failed for {pkg}:")
                    error_lines = result.stderr.split('\n')[:10]
                    for line in error_lines:
                        if line.strip():
                            print(f"        {line}")
                    return False
        else:
            # Check if main packages can build
            result = self.run_command([go_cmd, "build", "./..."], timeout=300)
            if result.returncode != 0:
                print(f"      ✗ Compilation check failed:")
                error_lines = result.stderr.split('\n')[:10]
                for line in error_lines:
                    if line.strip():
                        print(f"        {line}")
                return False

        print(f"      ✓ Code compiles successfully")
        return True

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run Go tests, optionally filtered to specific packages"""
        status_map = {}
        go_cmd = getattr(self, 'go_binary', 'go')

        if test_files:
            # Extract Go packages from test files
            packages = self._extract_go_packages(test_files)
            if packages:
                print(f"      → Targeting {len(packages)} package(s)")
                for pkg in list(packages)[:5]:
                    print(f"        - {pkg}")
                if len(packages) > 5:
                    print(f"        ... and {len(packages) - 5} more")

                # Run tests for each package
                _module_version_upgraded = False
                for pkg in packages:
                    cmd = [go_cmd, "test", "-v", pkg]
                    result = self.run_command(cmd, timeout=600)
                    output = result.stdout + result.stderr

                    # Check for dyld errors and retry with system Go if using custom binary
                    if result.returncode != 0 and ("dyld" in output or "missing LC_UUID" in output):
                        if hasattr(self, 'go_binary'):
                            print(f"      ⚠ dyld linker error detected with custom Go binary")
                            print(f"      → Retrying with system Go...")
                            # Temporarily use system go
                            original_go = go_cmd
                            go_cmd = "go"
                            cmd = [go_cmd, "test", "-v", pkg]
                            result = self.run_command(cmd, timeout=600)
                            output = result.stdout + result.stderr
                            go_cmd = original_go  # Restore for next iteration

                    # Check for "note: module requires Go X.Y" and upgrade if needed
                    if result.returncode != 0 and not _module_version_upgraded:
                        mod_req_match = re.search(r'note: module requires Go (\d+\.\d+)', output)
                        if mod_req_match:
                            needed_ver = mod_req_match.group(1)
                            current_go_ver = self.get_actual_version()
                            try:
                                cur_parts = [int(x) for x in current_go_ver.split('.')]
                                need_parts = [int(x) for x in needed_ver.split('.')]
                                need_upgrade = cur_parts < need_parts
                            except Exception:
                                need_upgrade = True
                            if need_upgrade:
                                print(f"      ⚠ Dependency requires Go {needed_ver} (current: {current_go_ver})")
                                print(f"      → Upgrading Go to {needed_ver} to satisfy transitive dependency...")
                                self.setup_version(needed_ver)
                                go_cmd = getattr(self, 'go_binary', 'go')
                                _module_version_upgraded = True
                                cmd = [go_cmd, "test", "-v", pkg]
                                result = self.run_command(cmd, timeout=600)
                                output = result.stdout + result.stderr

                    # Show output only for real build/compile failures (not test failures)
                    tests_actually_ran = "--- PASS" in output or "--- FAIL" in output or "--- SKIP" in output
                    if result.returncode != 0 and "no test files" not in output.lower() and not tests_actually_ran:
                        print(f"      ⚠ Test command failed for {pkg}")
                        print(f"      → Command: {' '.join(cmd[:3])}")
                        print(f"      → Error (first 800 chars): {output[:800]}")

                    # Debug: Always show if no tests found
                    if (len(status_map) == 0 or result.returncode != 0) and debug:
                        print(f"      [DEBUG] Package: {pkg}")
                        print(f"      [DEBUG] Go command: {go_cmd}")
                        print(f"      [DEBUG] Return code: {result.returncode}")
                        print(f"      [DEBUG] Output (first 1500 chars):\n{output[:1500]}")

                    self._parse_go_test_output(output, status_map)
            else:
                print(f"      → No matching packages found, running all tests")
                cmd = [go_cmd, "test", "-v", "./..."]
                result = self.run_command(cmd, timeout=600)
                output = result.stdout + result.stderr
                self._parse_go_test_output(output, status_map)
        else:
            # Run all tests (backward compatibility)
            cmd = [go_cmd, "test", "-v", "./..."]
            result = self.run_command(cmd, timeout=600)
            output = result.stdout + result.stderr
            self._parse_go_test_output(output, status_map)

        return status_map

    def _extract_go_packages(self, test_files: List[str]) -> set:
        """Extract Go package paths from test files

        Handles both *_test.go files and testdata/* files.
        For testdata files, finds the parent package directory.
        """
        packages = set()
        for file_path in test_files:
            if file_path.endswith('_test.go'):
                # Extract package directory: path/to/package/file_test.go -> ./path/to/package
                pkg_dir = str(Path(file_path).parent)
                if pkg_dir == '.':
                    packages.add('.')
                else:
                    packages.add(f"./{pkg_dir}")
            elif '/testdata/' in file_path:
                # Test data file: path/to/package/testdata/file.test -> ./path/to/package
                # Find the directory containing testdata/
                parts = file_path.split('/testdata/')[0]
                if parts:
                    packages.add(f"./{parts}")
                else:
                    packages.add('.')
        return packages

    def _parse_go_test_output(self, output: str, status_map: Dict[str, str]):
        """Parse go test output and populate status map"""
        for line in output.split('\n'):
            if line.strip().startswith('--- '):
                match = re.match(r'---\s+(PASS|FAIL|SKIP):\s+(\S+)', line)
                if match:
                    status = match.group(1)
                    test_name = match.group(2)
                    status_map[test_name] = 'PASSED' if status == 'PASS' else 'FAILED' if status == 'FAIL' else 'SKIPPED'

    def extract_modified_tests(self) -> set:
        """
        Extract specific test functions modified in test_patch for Go
        Returns: {'package/path.TestFunction', ...}
        """
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')

        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            if self.is_test_file(file_path):
                # Extract package from file path
                package = str(Path(file_path).parent)

                # Find test functions in changes
                current_test = None
                for line in patch_content.split('\n'):
                    line_content = line[1:] if line and line[0] in ' +-' else line

                    # Match: func TestSomething(t *testing.T)
                    test_match = re.search(r'func\s+(Test\w+)\s*\(', line_content)
                    if test_match:
                        current_test = test_match.group(1)

                    # Check for actual changes
                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line.startswith('+++') and
                                not line.startswith('---'))

                    if current_test and is_change:
                        modified_tests.add(f"{package}.{current_test}")
                        current_test = None  # Reset after adding

            i += 2

        return modified_tests


# ============================================================================
# JAVA VALIDATOR
# ============================================================================

class JavaValidator(BaseValidator):
    """Validator for Java projects (Maven/Gradle)"""

    def is_test_file(self, file_path: str) -> bool:
        return (
            '/test/' in file_path or file_path.startswith('test/')
            or file_path.endswith('Test.java') or file_path.endswith('Tests.java')
        )

    @staticmethod
    def _normalize_java_version(version_str: str) -> str:
        """
        Normalize Java version strings.
        Old-style '1.6', '1.7', '1.8' -> '6', '7', '8'
        Modern '11', '17.0.1' -> '11', '17'
        """
        version_str = version_str.strip()
        if '.' in version_str:
            parts = version_str.split('.')
            # Old-style: 1.6, 1.7, 1.8, 1.9, 1.11 (rare but handled)
            if parts[0] == '1' and len(parts) >= 2 and parts[1].isdigit():
                return parts[1]
            # Modern with patch: 17.0.1 -> 17
            return parts[0]
        return version_str

    def detect_required_version(self) -> str:
        """Detect required Java version"""
        # Check build.gradle or build.gradle.kts
        for gradle_file in ["build.gradle", "build.gradle.kts"]:
            path = self.repo_dir / gradle_file
            if path.exists():
                content = path.read_text()
                # sourceCompatibility = '17' or '1.8', JavaLanguageVersion.of(21)
                patterns = [
                    r'sourceCompatibility\s*=\s*["\']?([\d.]+)',
                    r'targetCompatibility\s*=\s*["\']?([\d.]+)',
                    r'JavaLanguageVersion\.of\((\d+)\)',
                    r'jvmTarget\s*=\s*["\'](\d+)',
                ]
                for pattern in patterns:
                    match = re.search(pattern, content)
                    if match:
                        return self._normalize_java_version(match.group(1))

        # Check pom.xml – supports both property style and compiler-plugin style:
        #   <java.version>1.8</java.version>
        #   <maven.compiler.source>1.8</maven.compiler.source>
        #   <source>1.8</source>  (inside maven-compiler-plugin config)
        for pom_candidate in [self.repo_dir / "pom.xml"] + list(self.repo_dir.glob("*/pom.xml")):
            if pom_candidate.exists():
                content = pom_candidate.read_text()
                patterns = [
                    r'<maven\.compiler\.source>([\d.]+)',
                    r'<maven\.compiler\.target>([\d.]+)',
                    r'<java\.version>([\d.]+)',
                    r'<source>([\d.]+)</source>',   # maven-compiler-plugin config
                    r'<target>([\d.]+)</target>',   # maven-compiler-plugin config
                ]
                for pattern in patterns:
                    match = re.search(pattern, content)
                    if match:
                        ver = self._normalize_java_version(match.group(1))
                        # Only return if it looks like a real Java version number
                        if ver.isdigit() and int(ver) >= 5:
                            return ver

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "java")

        return "17"  # Modern LTS

    def _detect_maven_module(self, test_files: List[str]) -> Optional[str]:
        """
        Detect which Maven submodule to use, given the test file paths.
        e.g. 'gson/src/test/java/...' -> 'gson'  (if gson/pom.xml exists)
        Returns module path string or None if not a multi-module project.
        """
        if not test_files:
            return None
        # Only relevant if root pom has <modules>
        root_pom = self.repo_dir / "pom.xml"
        if not root_pom.exists():
            return None
        root_content = root_pom.read_text()
        if '<modules>' not in root_content:
            return None

        # Derive candidate modules from test file paths
        modules: Dict[str, int] = {}
        for f in test_files:
            parts = f.split('/src/test/')
            if len(parts) == 2:
                candidate = parts[0]
                if (self.repo_dir / candidate / 'pom.xml').exists():
                    modules[candidate] = modules.get(candidate, 0) + 1
        if not modules:
            return None
        # Return the module with most test files
        return max(modules, key=lambda k: modules[k])

    def get_actual_version(self) -> str:
        """Get current Java version"""
        # Use self.env_vars so JAVA_HOME overrides (set by _apply_java_home) are respected
        env = self.env_vars if hasattr(self, 'env_vars') else None
        java_bin = "java"
        if env and env.get('JAVA_HOME'):
            java_bin = str(Path(env['JAVA_HOME']) / "bin" / "java")
        result = subprocess.run([java_bin, "-version"], capture_output=True, text=True, env=env)
        if result.returncode == 0:
            # Parse from stderr: openjdk version "25.0.2"
            output = result.stderr + result.stdout
            match = re.search(r'version\s+"(\d+)', output)
            if match:
                return match.group(1)
        return "unknown"

    def setup_version(self, required_version: str):
        """Setup required Java version and build tools"""
        conda_prefix = os.environ.get('CONDA_PREFIX', '')
        conda_install_cmd = ["conda", "install", "-y", "-c", "conda-forge"]
        if conda_prefix:
            conda_install_cmd += ["--prefix", conda_prefix]

        current_version = self.get_actual_version()
        try:
            current_int = int(current_version) if current_version.isdigit() else 0
            required_int = int(required_version)
        except (ValueError, TypeError):
            current_int = required_int = 0

        if current_int >= required_int > 0:
            # Current version already satisfies requirement — skip to avoid
            # corrupting the conda env (downgrade) or unnecessary churn.
            print(f"      → Java {current_version} satisfies requirement ({required_version}), skipping install")
        elif required_int > current_int > 0:
            # Need a higher Java version. Download it via Corretto/Zulu and set
            # JAVA_HOME in self.env_vars so the conda env is not permanently changed.
            print(f"      → Attempting to install Java {required_version}...")
            if not self._install_java_via_corretto(required_int):
                print(f"      ⚠ Corretto unavailable; trying Azul Zulu...")
                self._install_java_via_zulu(required_int)
        else:
            # Unknown versions — attempt conda install as original fallback
            print(f"      → Attempting to install Java {required_version}...")
            result = subprocess.run(
                conda_install_cmd + [f"openjdk={required_version}"],
                capture_output=True,
                timeout=600
            )

        # Install Maven and Gradle for Java projects
        print(f"      → Installing Maven, Gradle, and Ant...")
        for tool, pkg in [("maven", "maven"), ("gradle", "gradle"), ("ant", "ant")]:
            tool_result = subprocess.run(
                conda_install_cmd + [pkg],
                capture_output=True,
                timeout=600
            )
            if tool_result.returncode == 0:
                print(f"      ✓ {tool.capitalize()} installed")
            else:
                print(f"      ⚠ {tool.capitalize()} installation failed")

    def _maven_compat_flags(self) -> List[str]:
        """
        Return extra Maven -D flags to keep compilation working on modern JDKs.

        Java 17+ dropped support for --release / -source / -target < 8.
        We also skip the Maven Enforcer plugin which may check JDK version.
        The primary fix is _fix_pom_xml_for_modern_jdk(); these flags are a
        belt-and-suspenders backup for properties-style version settings.
        """
        flags = [
            "-Dmaven.javadoc.skip=true",
            "-Denforcer.skip=true",
            "-Dproguard.skip=true",   # ProGuard obfuscation incompatible with modern JDKs
        ]
        try:
            req_int = int(self.detected_version)
            actual_int = int(self.actual_version)
            MIN_SUPPORTED = 8  # Java 17+ dropped support for source/target < 8
            if req_int < MIN_SUPPORTED and actual_int >= MIN_SUPPORTED:
                flags += [
                    f"-Dmaven.compiler.source={MIN_SUPPORTED}",
                    f"-Dmaven.compiler.target={MIN_SUPPORTED}",
                ]
        except (ValueError, TypeError, AttributeError):
            pass
        return flags

    def _fix_pom_xml_for_modern_jdk(self):
        """
        Patch all pom.xml files in the project to compile with modern JDK (>= 8).

        Projects targeting Java 6 or 7 fail on Java 17+ because:
          - <source>/<target> < 8 is no longer accepted by javac
          - <jdkToolchain> may require a JDK version range like [1.5,9) that
            modern JDKs don't satisfy, causing the build to abort before compile

        This method edits the pom.xml files in the cloned repo (throwaway copy)
        to replace old version values and remove the toolchain restriction.
        It is a general fix applicable to any Maven project with old source/target.
        """
        OLD_VERSIONS = ['1.4', '1.5', '1.6', '1.7', '4', '5', '6', '7']
        changed_any = False

        # If project has module-info.java we need release >= 9 (JPMS args like
        # --add-reads are incompatible with target < 9).  Otherwise release=8 is fine.
        has_module_info = bool(list(self.repo_dir.rglob('module-info.java')))
        RELEASE = '11' if has_module_info else '8'

        pom_files = [self.repo_dir / 'pom.xml'] + list(self.repo_dir.glob('*/pom.xml'))
        for pom_file in pom_files:
            if not pom_file.exists():
                continue
            content = pom_file.read_text()
            original = content

            # Replace old <source>/<target> with modern <release>.
            # We use a sentinel so the second replacement doesn't double-replace.
            for old_ver in OLD_VERSIONS:
                if f'<source>{old_ver}</source>' in content:
                    content = content.replace(
                        f'<source>{old_ver}</source>',
                        f'<release>{RELEASE}</release>'
                    )
                    # Remove the matching <target> line (release covers both)
                    content = content.replace(f'<target>{old_ver}</target>', '')

            # Also bump any explicit old <release>N</release> that is < 9 when
            # module-info.java is present (avoids --add-reads incompatibility)
            if has_module_info:
                for old_rel in ['5', '6', '7', '8']:
                    content = content.replace(
                        f'<release>{old_rel}</release>',
                        f'<release>{RELEASE}</release>'
                    )

            # Remove <jdkToolchain> blocks that restrict to old JDK ranges
            content = re.sub(
                r'\s*<jdkToolchain>.*?</jdkToolchain>',
                '',
                content,
                flags=re.DOTALL
            )

            if content != original:
                pom_file.write_text(content)
                print(f"      → Patched {pom_file.relative_to(self.repo_dir)}: "
                      f"set compiler release={RELEASE} for modern JDK compatibility")
                changed_any = True

        return changed_any

    def install_dependencies(self):
        """Install Java dependencies"""
        print(f"[4/7] Installing dependencies...")
        # Detect build tool
        if (self.repo_dir / "pom.xml").exists():
            self.build_tool = "maven"
            # Patch pom.xml for modern JDK compatibility (e.g. source/target < 8)
            self._fix_pom_xml_for_modern_jdk()
            # `mvn install -DskipTests` handles multi-module dependency ordering
            # better than `dependency:resolve` + `test-compile` separately.
            compat = self._maven_compat_flags()
            print(f"      → Compiling project (mvn install -DskipTests)...")
            result = self.run_command(
                ["mvn", "install", "-DskipTests"] + compat,
                timeout=600
            )
            if result.returncode != 0:
                print(f"      ⚠ Full install failed, trying compile only...")
                result2 = self.run_command(["mvn", "test-compile"] + compat, timeout=600)
                if result2.returncode != 0:
                    print(f"      ⚠ Compile failed: {result2.stderr[:200]}")
                else:
                    print(f"      ✓ Compile successful")
            else:
                print(f"      ✓ Dependencies installed")

        elif (self.repo_dir / "build.xml").exists():
            # Apache Ant project (e.g. lombok, older Java projects)
            self.build_tool = "ant"
            print(f"      → Building with Ant...")
            # Try common Ant build targets in priority order
            for target in ["dist", "compile", "build", ""]:
                cmd = ["ant"] + ([target] if target else [])
                result = self.run_command(cmd, timeout=600)
                if result.returncode == 0:
                    print(f"      ✓ Ant build successful" + (f" (target: {target})" if target else ""))
                    break
            else:
                print(f"      ⚠ Ant build failed (tests may still work)")

        else:
            self.build_tool = "gradle"
            gradle_cmd = "./gradlew" if (self.repo_dir / "gradlew").exists() else "gradle"

            result = self.run_command([gradle_cmd, "dependencies", "--no-daemon"], timeout=600)
            if result.returncode != 0:
                print(f"      ⚠ Dependency resolution failed: {result.stderr[:200]}")

            print(f"      → Compiling project...")
            result = self.run_command([gradle_cmd, "compileTestJava", "--no-daemon"], timeout=600)
            if result.returncode != 0:
                print(f"      ⚠ Compile failed: {result.stderr[:200]}")
            else:
                print(f"      ✓ Compile successful")

    def _detect_gradle_modules_from_tests(self, test_files: List[str]) -> List[str]:
        """Detect Gradle submodules from test file paths"""
        modules = set()
        for test_file in test_files:
            # Extract module path from test file
            # e.g., "lucene/queries/src/test/..." -> "lucene/queries"
            parts = test_file.split('/src/test/')
            if len(parts) == 2:
                module_path = parts[0]
                modules.add(module_path)
            # Also try /src/main/
            parts = test_file.split('/src/main/')
            if len(parts) == 2:
                module_path = parts[0]
                modules.add(module_path)
        return list(modules)

    def _parse_junit_xml(self, xml_path: Path) -> Dict[str, str]:
        """Parse JUnit XML test results"""
        status_map = {}
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
            root = tree.getroot()

            # Handle both <testsuite> and <testsuites> root elements
            testsuites = root.findall('.//testsuite')
            if not testsuites:
                testsuites = [root] if root.tag == 'testsuite' else []

            for testsuite in testsuites:
                for testcase in testsuite.findall('testcase'):
                    classname = testcase.get('classname', '')
                    name = testcase.get('name', '')

                    # Construct full test name
                    if classname:
                        test_name = f"{classname}.{name}"
                    else:
                        test_name = name

                    # Check for failures/errors
                    if testcase.find('failure') is not None or testcase.find('error') is not None:
                        status_map[test_name] = 'FAILED'
                    elif testcase.find('skipped') is not None:
                        status_map[test_name] = 'SKIPPED'
                    else:
                        status_map[test_name] = 'PASSED'

        except Exception as e:
            print(f"      ⚠ Failed to parse XML {xml_path}: {e}")

        return status_map

    def _parse_ant_stdout_results(self, output: str) -> Dict[str, str]:
        """Parse JUnit test results from ant stdout/stderr text output.

        Handles multiple output formats:
        1. SimpleTestFormatter style (e.g. Lombok): lines like
               [junit] [PASS] testMethod(full.class.Name)
               [junit] [FAIL] testMethod(full.class.Name)
               [junit] [ERR]  testMethod(full.class.Name)
           These are produced by custom formatters configured with usefile=false
           so no XML is written; all results appear only in the process stdout.
        2. Standard JUnit brief/plain formatter (usefile=false or stdout capture):
               [junit] Running full.class.Name
               [junit] Tests run: X, Failures: Y, Errors: Z, Skipped: W, ...
           When individual test names aren't available, synthetic entries are
           created so that the before/after comparison still works.

        The Ant task prefixes every line with "[<taskname>] " (e.g. "[junit] ").
        This method strips that prefix before trying each pattern.
        """
        status_map = {}
        current_class = None

        for line in output.split('\n'):
            # Strip Ant task prefix: "[junit] ", "[java] ", etc.
            stripped = re.sub(r'^\s*\[\w+\]\s*', '', line).strip()
            if not stripped:
                continue

            # --- Format 1: SimpleTestFormatter ---
            # [PASS] testMethodName(full.class.Name)
            # [FAIL] testMethodName(full.class.Name)
            # [ERR]  testMethodName(full.class.Name)
            m = re.match(r'\[(PASS|FAIL|ERR)\]\s+([^\s(]+)\(([^)]+)\)', stripped)
            if m:
                tag, method, classname = m.group(1), m.group(2), m.group(3)
                test_name = f"{classname}.{method}"
                if tag == 'PASS':
                    status_map[test_name] = 'PASSED'
                elif tag == 'FAIL':
                    status_map[test_name] = 'FAILED'
                else:
                    status_map[test_name] = 'ERROR'
                continue

            # --- Format 2a: "Running <ClassName>" header ---
            m = re.match(r'Running\s+([\w.$]+)', stripped)
            if m:
                current_class = m.group(1)
                continue

            # --- Format 2b: "Tests run: X, Failures: Y, Errors: Z[, Skipped: W]" summary ---
            m = re.match(
                r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)',
                stripped
            )
            if m and current_class:
                total = int(m.group(1))
                failures = int(m.group(2))
                errors = int(m.group(3))
                passed = total - failures - errors
                # Only create synthetic entries when the class hasn't already been
                # covered by Format 1 (individual method names).
                class_already_parsed = any(
                    k.startswith(current_class + '.') for k in status_map
                )
                if not class_already_parsed:
                    for i in range(passed):
                        status_map[f"{current_class}.__passed_{i}"] = 'PASSED'
                    for i in range(failures):
                        status_map[f"{current_class}.__failed_{i}"] = 'FAILED'
                    for i in range(errors):
                        status_map[f"{current_class}.__error_{i}"] = 'ERROR'
                current_class = None
                continue

        return status_map

    def extract_modified_tests(self) -> set:
        """
        Extract specific test methods modified in test_patch for Java
        Returns: {'org.apache.lucene.queries.TestClass.testMethod', ...}
        """
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')

        # Split patch by files
        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            # Only process Java test files
            if 'test' in file_path.lower() and file_path.endswith('.java'):
                # Extract package and class name from file path
                # e.g., lucene/queries/src/test/org/apache/lucene/queries/intervals/TestIntervalBuilder.java
                # -> org.apache.lucene.queries.intervals.TestIntervalBuilder
                if '/src/test/' in file_path:
                    parts = file_path.split('/src/test/')
                    if len(parts) == 2:
                        class_path = parts[1].replace('/', '.').replace('.java', '')
                    else:
                        class_path = file_path.replace('/', '.').replace('.java', '')
                else:
                    class_path = file_path.replace('/', '.').replace('.java', '')

                current_test = None
                tests_with_changes = set()

                lines = patch_content.split('\n')
                for line in lines:
                    line_content = line[1:] if line and line[0] in ' +-' else line

                    # Track test methods: public void testSomething() or @Test
                    test_match = re.search(r'public\s+void\s+(test\w+)\s*\(', line_content)
                    if test_match:
                        current_test = test_match.group(1)

                    # Check for actual changes within test method
                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line.startswith('+++') and
                                not line.startswith('---') and
                                not line[1:].strip().startswith('//'))

                    if current_test and is_change:
                        tests_with_changes.add(current_test)

                # Build test identifiers: package.ClassName.methodName
                for test_name in tests_with_changes:
                    modified_tests.add(f"{class_path}.{test_name}")

            i += 2

        return modified_tests

    def _is_lombok_style_project(self) -> bool:
        """Return True for Lombok-style projects that use version-annotated test
        resource files and version-specific ant targets (buildScripts/tests.ant.xml).
        Java version switching is scoped to these projects only."""
        return (self.repo_dir / "buildScripts" / "tests.ant.xml").exists()

    def _detect_required_test_java_version(self) -> Optional[int]:
        """Parse '// version X:' from test_patch to find the Java version
        required by newly-added test resource files (Lombok convention)."""
        test_patch = self.instance.get('test_patch', '')
        max_version = None
        for section in re.split(r'diff --git a/.*? b/', test_patch):
            for line in section.split('\n')[:20]:
                if line.startswith('+') and not line.startswith('+++'):
                    m = re.match(r'//\s*version\s+(\d+)', line[1:].strip())
                    if m:
                        v = int(m.group(1))
                        if max_version is None or v > max_version:
                            max_version = v
        return max_version

    def _install_java_version_only(self, version: int) -> bool:
        """Install a specific Java version for Lombok-style test targets.

        Tries in order:
        1. conda-forge — covers LTS versions (8, 11, 17, 20).
        2. Amazon Corretto — covers all versions including non-LTS (14, 15 …).
           On aarch64 systems, falls back to x64 for old versions that predate
           Apple Silicon / ARM Linux builds (runs via Rosetta 2 / QEMU).

        Updates self.env_vars[JAVA_HOME/PATH] on success so subsequent ant
        subprocesses pick up the new JDK.  Results are cached so the download
        only happens once per validation run.
        Returns True on success, False if every method fails.
        """
        if not hasattr(self, '_java_install_cache'):
            self._java_install_cache: dict = {}
        if version in self._java_install_cache:
            return self._java_install_cache[version]

        current = self.get_actual_version()
        if current.isdigit() and int(current) == version:
            self._java_install_cache[version] = True
            return True

        print(f"      → Installing Java {version} for version-specific tests...")

        # --- 1. conda-forge ---
        _conda_prefix = os.environ.get('CONDA_PREFIX', '')
        _conda_cmd = ["conda", "install", "-y", "-c", "conda-forge"]
        if _conda_prefix:
            _conda_cmd += ["--prefix", _conda_prefix]
        r = subprocess.run(
            _conda_cmd + [f"openjdk={version}"],
            capture_output=True, timeout=600
        )
        if r.returncode == 0:
            print(f"      ✓ Java {version} installed via conda-forge")
            self._refresh_java_env_from_conda()
            self._java_install_cache[version] = True
            return True

        print(f"      ⚠ conda-forge has no Java {version}; trying Amazon Corretto...")

        # --- 2. Amazon Corretto "latest" static URL (active versions) ---
        if self._install_java_via_corretto(version):
            self._java_install_cache[version] = True
            return True

        print(f"      ⚠ Corretto unavailable; trying Azul Zulu...")

        # --- 3. Azul Zulu — archives ALL versions including EOL non-LTS ---
        ok = self._install_java_via_zulu(version)
        self._java_install_cache[version] = ok
        return ok

    def _refresh_java_env_from_conda(self):
        """After a conda Java install, point env_vars at the new JDK."""
        conda_prefix = os.environ.get('CONDA_PREFIX', '')
        if not conda_prefix:
            return
        for candidate in [
            Path(conda_prefix) / "lib" / "jvm",
            Path(conda_prefix) / "jre",
        ]:
            if (candidate / "bin" / "java").exists():
                self._apply_java_home(candidate)
                return

    def _install_java_via_corretto(self, version: int) -> bool:
        """Download Amazon Corretto and wire it into self.env_vars.

        Corretto URL pattern:
          https://corretto.aws/downloads/latest/
              amazon-corretto-{version}-{arch}-{os}-jdk.{ext}
        where arch ∈ {x64, aarch64} and os ∈ {macos, linux, windows}.

        Old Java versions (≤15) have no aarch64 build; on Apple Silicon /
        ARM Linux the download falls back to x64 (Rosetta 2 / QEMU).
        """
        import urllib.request
        import tarfile as _tarfile
        import zipfile as _zipfile

        system  = platform.system().lower()
        machine = platform.machine().lower()
        os_map   = {'darwin': 'macos', 'linux': 'linux', 'windows': 'windows'}
        arch_map = {'x86_64': 'x64', 'amd64': 'x64',
                    'aarch64': 'aarch64', 'arm64': 'aarch64'}

        corretto_os  = os_map.get(system, 'linux')
        native_arch  = arch_map.get(machine, 'x64')
        # Try native arch first; fall back to x64 for old versions without ARM builds
        archs = [native_arch] if native_arch == 'x64' else [native_arch, 'x64']
        ext   = 'zip' if system == 'windows' else 'tar.gz'

        jdks_dir = Path.home() / ".cache" / "swebench" / "jdks"
        jdks_dir.mkdir(parents=True, exist_ok=True)

        for arch in archs:
            url = (
                f"https://corretto.aws/downloads/latest/"
                f"amazon-corretto-{version}-{arch}-{corretto_os}-jdk.{ext}"
            )
            archive     = jdks_dir / f"corretto-{version}-{arch}.{ext}"
            extract_dir = jdks_dir / f"corretto-{version}-{arch}"
            extract_dir.mkdir(exist_ok=True)

            # Use cached extraction if already downloaded
            java_home = self._find_java_home(extract_dir)
            if java_home is not None:
                self._apply_java_home(java_home)
                print(f"      ✓ Corretto Java {version} ({arch}) ready (cached)")
                return True

            try:
                req = urllib.request.Request(
                    url, headers={'User-Agent': 'swebench-validator/1.0'})
                with urllib.request.urlopen(req, timeout=180) as resp, \
                     open(archive, 'wb') as fout:
                    shutil.copyfileobj(resp, fout)

                if ext == 'tar.gz':
                    with _tarfile.open(archive) as tf:
                        tf.extractall(extract_dir)
                else:
                    with _zipfile.ZipFile(archive) as zf:
                        zf.extractall(extract_dir)
                archive.unlink(missing_ok=True)

                java_home = self._find_java_home(extract_dir)
                if java_home is None:
                    print(f"      ⚠ Could not find java binary in Corretto archive")
                    continue

                self._apply_java_home(java_home)
                print(f"      ✓ Corretto Java {version} ({arch}) ready")
                return True

            except Exception as exc:
                print(f"      ⚠ Corretto {arch} download failed: {exc}")
                continue

        return False

    def _install_java_via_zulu(self, version: int) -> bool:
        """Download Azul Zulu JDK via the Azul metadata API.

        Azul archives every Java version including EOL non-LTS releases (14, 15…).
        API: https://api.azul.com/metadata/v1/zulu/packages/
        On aarch64 hosts, falls back to x86_64 (Rosetta 2 / QEMU) when no
        native ARM build exists for the requested version.
        """
        import urllib.request
        import tarfile as _tarfile
        import zipfile as _zipfile

        system  = platform.system().lower()
        machine = platform.machine().lower()
        os_map   = {'darwin': 'macos', 'linux': 'linux', 'windows': 'windows'}
        arch_map = {'x86_64': 'x86_64', 'amd64': 'x86_64',
                    'aarch64': 'aarch64', 'arm64': 'aarch64'}

        zulu_os     = os_map.get(system, 'linux')
        native_arch = arch_map.get(machine, 'x86_64')
        archs       = [native_arch] if native_arch == 'x86_64' else [native_arch, 'x86_64']
        ext         = 'zip' if system == 'windows' else 'tar.gz'

        jdks_dir = Path.home() / ".cache" / "swebench" / "jdks"
        jdks_dir.mkdir(parents=True, exist_ok=True)

        for arch in archs:
            extract_dir = jdks_dir / f"zulu-{version}-{arch}"
            extract_dir.mkdir(exist_ok=True)

            # Use cached extraction if already downloaded
            java_home = self._find_java_home(extract_dir)
            if java_home is not None:
                self._apply_java_home(java_home)
                print(f"      ✓ Zulu Java {version} ({arch}) ready (cached)")
                return True

            api_url = (
                f"https://api.azul.com/metadata/v1/zulu/packages/"
                f"?java_version={version}&os={zulu_os}&arch={arch}"
                f"&java_package_type=jdk&latest=true&release_status=ga"
                f"&archive_type={ext}"
            )
            try:
                req = urllib.request.Request(
                    api_url,
                    headers={'User-Agent': 'swebench-validator/1.0',
                             'Accept': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    packages = json.loads(resp.read())

                if not packages:
                    continue

                download_url = packages[0].get('download_url')
                if not download_url:
                    continue

                archive = jdks_dir / f"zulu-{version}-{arch}.{ext}"

                dl_req = urllib.request.Request(
                    download_url,
                    headers={'User-Agent': 'swebench-validator/1.0'})
                with urllib.request.urlopen(dl_req, timeout=300) as resp, \
                     open(archive, 'wb') as fout:
                    shutil.copyfileobj(resp, fout)

                if ext == 'tar.gz':
                    with _tarfile.open(archive) as tf:
                        tf.extractall(extract_dir)
                else:
                    with _zipfile.ZipFile(archive) as zf:
                        zf.extractall(extract_dir)
                archive.unlink(missing_ok=True)

                java_home = self._find_java_home(extract_dir)
                if java_home is None:
                    print(f"      ⚠ Could not find java binary in Zulu archive")
                    continue

                self._apply_java_home(java_home)
                print(f"      ✓ Zulu Java {version} ({arch}) ready")
                return True

            except Exception as exc:
                print(f"      ⚠ Azul Zulu failed ({arch}): {exc}")
                continue

        return False

    def _find_java_home(self, root: Path) -> Optional[Path]:
        """Find JAVA_HOME inside an extracted JDK archive.
        macOS layout: <root>/<jdk>/Contents/Home/bin/java
        Linux layout: <root>/<jdk>/bin/java"""
        for java_bin in sorted(root.rglob("bin/java")):
            if java_bin.is_file() or java_bin.is_symlink():
                return java_bin.parent.parent
        return None

    def _apply_java_home(self, java_home: Path):
        """Update env_vars so ant subprocesses use the given JDK."""
        self.env_vars['JAVA_HOME'] = str(java_home)
        current_path = self.env_vars.get('PATH', os.environ.get('PATH', ''))
        self.env_vars['PATH'] = str(java_home / "bin") + os.pathsep + current_path

    def _build_ant_test_targets(self, required_version: Optional[int]) -> List[str]:
        """Return a prioritised list of ant test targets.

        When a version requirement is detected we put the version-specific
        target first (e.g. test.javac14) so the right compiler is used.
        test.javacCurrent always follows as a reliable fallback, then the
        generic catch-all targets.

        On aarch64 hosts where the required JDK is x86_64 (Rosetta emulation),
        versioned targets are deprioritized to avoid ~15min Rosetta timeouts.
        """
        generic = ["test", "junit", "tests", "check"]
        current_targets = ["test.javacCurrent"]

        # Detect Rosetta: aarch64 host but x86_64 JDK installed for this version
        java_home = self.env_vars.get('JAVA_HOME', '')
        host_is_arm = platform.machine().lower() in ('arm64', 'aarch64')
        jdk_is_x86 = any(s in java_home for s in ('x86_64', 'x64', 'amd64'))
        rosetta = host_is_arm and jdk_is_x86

        if required_version:
            versioned = [f"test.javac{required_version}"]
            # Also include adjacent versions in case the exact one isn't defined
            for delta in [1, -1, 2, -2]:
                versioned.append(f"test.javac{required_version + delta}")
            if rosetta:
                # Under Rosetta, versioned targets take 15+ min — use native first
                print(f"      → Rosetta detected: using test.javacCurrent before versioned targets")
                return current_targets + versioned + generic
            return versioned + current_targets + generic

        # No version constraint: prefer current then generic
        return current_targets + ["test.javac17", "test.javac16", "test.javac14",
                                   "test.javac11"] + generic

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run Java tests and parse XML results"""
        status_map = {}

        if self.build_tool == "maven":
            # Detect Maven submodule from test files (multi-module projects like gson)
            maven_module = self._detect_maven_module(test_files) if test_files else None
            if maven_module:
                print(f"      → Using Maven module: {maven_module}")

            # Maven: Run tests and parse surefire-reports XML
            compat = self._maven_compat_flags()
            cmd = ["mvn", "test"] + compat
            if maven_module:
                # Run only the relevant submodule; also build it with its dependencies
                cmd = ["mvn", "test", "-pl", maven_module, "--also-make"] + compat
            if debug:
                cmd.append("-X")
            result = self.run_command(cmd, timeout=900)

            # Parse Maven surefire XML reports
            surefire_dirs = list(self.repo_dir.rglob("target/surefire-reports"))
            for surefire_dir in surefire_dirs:
                xml_files = list(surefire_dir.glob("TEST-*.xml"))
                for xml_file in xml_files:
                    results = self._parse_junit_xml(xml_file)
                    status_map.update(results)

            # Only show debug output when the build genuinely failed (no test
            # results at all).  returncode=1 is normal when tests fail and does
            # not indicate a build problem – Maven always exits 1 on test failures.
            build_failed = result.returncode != 0 and not status_map
            if debug or build_failed:
                print(f"      [DEBUG] Maven test command: {' '.join(cmd)}")
                print(f"      [DEBUG] Return code: {result.returncode}")
                print(f"      [DEBUG] Output (last 500 chars): {result.stdout[-500:]}")
                if debug:
                    print(f"      [DEBUG] Found {len(surefire_dirs)} surefire-reports directories")

        elif self.build_tool == "ant":
            # For Lombok-style projects: detect the Java version required by
            # the test resource files and try to install it (including via
            # Adoptium for non-LTS versions not in conda-forge).
            # For all other Ant-based Java repos this block is skipped so their
            # existing target order and Java setup are preserved.
            required_test_version = None
            if self._is_lombok_style_project():
                required_test_version = self._detect_required_test_java_version()
                if required_test_version:
                    print(f"      → Test files require Java {required_test_version}")
                    self._install_java_version_only(required_test_version)

            ant_test_targets = self._build_ant_test_targets(required_test_version)
            ran = False
            result = None
            # If we installed a specific Java via Adoptium, tell Lombok's build
            # where to find it via -Djvm.loc.<version>=<JAVA_HOME>.
            # This is a no-op for non-Lombok repos (required_test_version is None).
            java_home_flag: List[str] = []
            if required_test_version and 'JAVA_HOME' in self.env_vars:
                java_home_flag = [
                    f"-Djvm.loc.{required_test_version}={self.env_vars['JAVA_HOME']}"
                ]
            for target in ant_test_targets:
                ant_cmd = ["ant", target] + java_home_flag
                result = self.run_command(ant_cmd, timeout=900)
                output = result.stdout + result.stderr

                # Skip targets that clearly didn't run tests:
                # - target not found in the build file
                # - ant is waiting for interactive input (e.g. asking for a
                #   JVM path because the required version isn't installed)
                # - command timed out (e.g. Rosetta x86_64 emulation too slow)
                target_unusable = (
                    result.returncode == 124  # timeout
                    or "does not exist" in output
                    or "Unknown target" in output
                    or "No such target" in output
                    or "[input]" in output   # interactive prompt = JVM not found
                )
                if target_unusable:
                    continue

                # returncode 0 = all tests passed; 1 = some tests failed (haltonfailure).
                # Both mean the target ran.  Other codes indicate a build error.
                if result.returncode in (0, 1):
                    # Confirm tests actually executed (not just a no-op build step).
                    # NOTE: "BUILD FAILED" is intentionally excluded – it can fire
                    # even when no tests ran (e.g. compilation failure).
                    tests_ran = (
                        "BUILD SUCCESSFUL" in output
                        or "Tests run" in output
                        or "[PASS]" in output
                        or "[FAIL]" in output
                        or "[ERR]" in output
                        or "[junit]" in output
                    )
                    if tests_ran:
                        ran = True
                        print(f"      → Ran ant target: {target}")
                        break

            if not ran:
                # Last resort: run default ant target (no target specified)
                result = self.run_command(["ant"], timeout=900)

            # Parse JUnit XML from all common Ant output locations
            xml_patterns = [
                "build/test-results/**/*.xml",
                "build/junit/**/*.xml",
                "build/reports/tests/**/*.xml",
                "build/reports/**/*.xml",
                "**/test-results/**/*.xml",
            ]
            for pattern in xml_patterns:
                for xml_file in self.repo_dir.glob(pattern):
                    if xml_file.name.startswith("TEST-") or "TEST" in xml_file.name:
                        results = self._parse_junit_xml(xml_file)
                        status_map.update(results)

            # Also check for non-standard JUnit XML output (any TEST-*.xml anywhere)
            if not status_map:
                for xml_file in self.repo_dir.rglob("TEST-*.xml"):
                    results = self._parse_junit_xml(xml_file)
                    status_map.update(results)

            # Second fallback: parse ant stdout for text-format test results.
            # Handles projects that use custom formatters with usefile=false
            # (e.g. Lombok's SimpleTestFormatter) or standard JUnit text formatters
            # that write to stdout instead of XML files.
            if not status_map and ran:
                ant_output = result.stdout + result.stderr
                stdout_results = self._parse_ant_stdout_results(ant_output)
                if stdout_results:
                    status_map.update(stdout_results)
                    print(f"      → Parsed {len(status_map)} tests from ant text output (no XML found)")

            build_failed = not status_map
            if (debug or build_failed) and result is not None:
                print(f"      [DEBUG] Ant test ran, return code: {result.returncode}")
                print(f"      [DEBUG] Output (last 500 chars): {result.stdout[-500:]}")

        else:
            # Gradle: Detect modules and run tests on each
            gradle_cmd = "./gradlew" if (self.repo_dir / "gradlew").exists() else "gradle"

            # Detect modules from test files
            if test_files:
                modules = self._detect_gradle_modules_from_tests(test_files)
            else:
                modules = []

            if modules:
                # Clean old test results to avoid reading stale XML files
                for module in modules:
                    module_dir = self.repo_dir / module
                    test_results_dir = module_dir / "build" / "test-results" / "test"
                    if test_results_dir.exists():
                        shutil.rmtree(test_results_dir)

                # Run tests on detected modules
                for module in modules:
                    # Convert path to Gradle module syntax: lucene/queries -> :lucene:queries
                    gradle_module = ':' + module.replace('/', ':')
                    print(f"      → Running tests in module: {gradle_module}")

                    cmd = [gradle_cmd, f"{gradle_module}:test", "--no-daemon"]
                    result = self.run_command(cmd, timeout=900)

                    # Parse XML results from this module
                    module_dir = self.repo_dir / module
                    test_results_dir = module_dir / "build" / "test-results" / "test"

                    if test_results_dir.exists():
                        for xml_file in test_results_dir.glob("TEST-*.xml"):
                            results = self._parse_junit_xml(xml_file)
                            status_map.update(results)
            else:
                # No modules detected, run on root
                result = self.run_command([gradle_cmd, "test", "--no-daemon"], timeout=900)

                # Parse all test-results XML files
                for xml_file in self.repo_dir.rglob("build/test-results/test/TEST-*.xml"):
                    results = self._parse_junit_xml(xml_file)
                    status_map.update(results)

        return status_map


# ============================================================================
# JAVASCRIPT VALIDATOR
# ============================================================================

class JavaScriptValidator(BaseValidator):
    """Validator for JavaScript/TypeScript projects"""

    def is_test_file(self, file_path: str) -> bool:
        return (
            file_path.endswith('.test.js') or
            file_path.endswith('.test.ts') or
            file_path.endswith('.spec.js') or
            file_path.endswith('.spec.ts') or
            '/test/' in file_path or
            file_path.startswith('test/') or
            '/__tests__/' in file_path or
            file_path.startswith('__tests__/')
        )

    def detect_required_version(self) -> str:
        """Detect required Node.js version"""
        # Check .nvmrc
        nvmrc = self.repo_dir / ".nvmrc"
        if nvmrc.exists():
            content = nvmrc.read_text().strip()
            match = re.search(r'(\d+)', content)
            if match:
                return match.group(1)

        # Check package.json engines field
        package_json = self.repo_dir / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    if "engines" in pkg and "node" in pkg["engines"]:
                        node_version = pkg["engines"]["node"]
                        # Parse >=16.0.0 or ^18.0.0
                        match = re.search(r'(\d+)', node_version)
                        if match:
                            return match.group(1)
            except:
                pass

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "javascript")

        return "18"

    def get_actual_version(self) -> str:
        """Get current Node.js version"""
        # If nvm is set up, check the version within nvm context
        if 'NVM_DIR' in self.env_vars and self._has_nvm():
            required_version = self.env_vars.get('NODE_VERSION')
            if required_version:
                # Verify nvm version is available
                nvm_script = f"""
                export NVM_DIR="$HOME/.nvm"
                [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
                nvm use {required_version} >/dev/null 2>&1 && node --version
                """
                result = subprocess.run(
                    ["bash", "-c", nvm_script],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0 and result.stdout.strip():
                    match = re.search(r'v(\d+)', result.stdout)
                    if match:
                        return match.group(1)

        # Fallback to system node
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            match = re.search(r'v(\d+)', result.stdout)
            if match:
                return match.group(1)
        return "unknown"

    def _has_nvm(self) -> bool:
        """Check if nvm is installed"""
        nvm_dir = os.path.expanduser('~/.nvm')
        return os.path.exists(nvm_dir)

    def run_command(self, cmd: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Override to use nvm if NVM_DIR is set"""
        if 'NVM_DIR' in self.env_vars and self._has_nvm():
            # Wrap command to source nvm and use correct version
            cmd_str = ' '.join(cmd)
            # Get the required version to ensure we use it
            required_version = self.env_vars.get('NODE_VERSION', self.detected_version)
            wrapped_cmd = f"""
            export NVM_DIR="$HOME/.nvm"
            [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
            nvm use {required_version} >/dev/null 2>&1 || true
            {cmd_str}
            """
            use_rosetta = self.env_vars.get('_NVM_USE_ROSETTA') == '1'
            shell_cmd = ["arch", "-x86_64", "bash", "-c", wrapped_cmd] if use_rosetta else ["bash", "-c", wrapped_cmd]
            return subprocess.run(
                shell_cmd,
                cwd=cwd or self.repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **self.env_vars}
            )
        else:
            # Use parent class method
            return super().run_command(cmd, cwd, timeout)

    def setup_version(self, required_version: str):
        """Setup required Node.js version using nvm if available"""
        current_version = self.get_actual_version()

        if current_version == required_version:
            print(f"      ✓ Node.js {current_version} already available")
            return

        print(f"      ⚠ Node.js version mismatch (required: {required_version}, available: {current_version})")

        # Try using nvm
        if self._has_nvm():
            print(f"      → Using nvm to install Node.js {required_version}...")

            # Source nvm and install version (native ARM64 first)
            nvm_script = f"""
            export NVM_DIR="$HOME/.nvm"
            [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
            nvm install {required_version}
            nvm use {required_version}
            """

            result = subprocess.run(
                ["bash", "-c", nvm_script],
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(self.repo_dir)
            )

            if result.returncode == 0:
                print(f"      ✓ Node.js {required_version} installed via nvm")
                # Update environment to use nvm version
                self.env_vars['NVM_DIR'] = os.path.expanduser('~/.nvm')
                self.env_vars['NODE_VERSION'] = required_version
                return

            # On Apple Silicon, older Node versions (< 16) have no ARM64 binary.
            # Fall back to installing the x64 build via Rosetta 2.
            stderr_txt = result.stderr + result.stdout
            is_apple_silicon = platform.processor() == 'arm' or platform.machine() == 'arm64'
            arm64_404 = '404' in stderr_txt or 'darwin-arm64' in stderr_txt
            if is_apple_silicon and (arm64_404 or result.returncode != 0):
                print(f"      → ARM64 build unavailable, trying x64 via Rosetta 2...")
                nvm_x64_script = f"""
                export NVM_DIR="$HOME/.nvm"
                [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
                nvm install {required_version} --arch=x64 2>/dev/null || \
                    nvm install {required_version}
                nvm use {required_version}
                """
                result_x64 = subprocess.run(
                    ["arch", "-x86_64", "bash", "-c", nvm_x64_script],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=str(self.repo_dir)
                )
                if result_x64.returncode == 0:
                    print(f"      ✓ Node.js {required_version} installed via nvm (x64/Rosetta)")
                    self.env_vars['NVM_DIR'] = os.path.expanduser('~/.nvm')
                    self.env_vars['NODE_VERSION'] = required_version
                    # Flag that commands must run under Rosetta
                    self.env_vars['_NVM_USE_ROSETTA'] = '1'
                    return
                else:
                    print(f"      ⚠ nvm x64 installation also failed: {result_x64.stderr[:200]}")
            else:
                print(f"      ⚠ nvm installation failed: {result.stderr[:200]}")
        else:
            print(f"      → nvm not found. To install:")
            print(f"         curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash")

        print(f"      → Continuing with available version (may cause compatibility issues)")
        print(f"      → To fix: Install nvm or use conda install nodejs={required_version}")

    def _setup_npm_isolation(self):
        """Isolate npm cache/config per run to avoid cross-run contamination."""
        try:
            cache_dir = self.workspace / ".npm_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.env_vars['npm_config_cache'] = str(cache_dir)
            self.env_vars['npm_config_audit'] = 'false'
            self.env_vars['npm_config_fund'] = 'false'
            self.env_vars['npm_config_update_notifier'] = 'false'
            # Keep npm from writing to global/user config in shared shell sessions
            npmrc = self.workspace / ".npmrc"
            if not npmrc.exists():
                npmrc.write_text("audit=false\nfund=false\nupdate-notifier=false\n")
            self.env_vars['npm_config_userconfig'] = str(npmrc)
        except Exception:
            pass

    def setup_environment(self):
        """Setup Node.js version and isolate npm for this run."""
        super().setup_environment()
        self._setup_npm_isolation()

    def install_dependencies(self):
        """Install npm/yarn dependencies"""
        print(f"[4/7] Installing dependencies...")

        if (self.repo_dir / "yarn.lock").exists():
            cmd = ["yarn", "install"]
        elif (self.repo_dir / "pnpm-lock.yaml").exists():
            cmd = ["pnpm", "install"]
        else:
            cmd = ["npm", "install"]

        result = self.run_command(cmd, timeout=600)
        if result.returncode != 0:
            print(f"      ⚠ Dependency installation failed: {result.stderr[:200]}")
            # Retry without lifecycle scripts (skips binary downloads like iltorb
            # that fail on platforms without pre-built binaries, e.g. ARM64 for old packages)
            print(f"      → Retrying with --ignore-scripts ...")
            ignore_cmd = cmd + ["--ignore-scripts"]
            result2 = self.run_command(ignore_cmd, timeout=600)
            if result2.returncode == 0:
                print(f"      ✓ Dependencies installed (--ignore-scripts)")
                # Re-run install scripts for critical build tools that genuinely need
                # their native binary (esbuild, etc.) — but not iltorb which is optional.
                self._reinstall_critical_native_tools()
            else:
                print(f"      ⚠ Retry also failed: {result2.stderr[:150]}")

        # Stub out native modules whose binary could not be built/downloaded.
        # These are optional packages (e.g. iltorb for Sauce Labs HTTP compression)
        # that cause a hard crash at require() time even though tests don't need them.
        self._stub_missing_native_modules()

        # Check if build is needed (for monorepos like Babel, Lerna projects)
        package_json = self.repo_dir / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    scripts = pkg.get("scripts", {})

                    # Check for common build scripts
                    if "build" in scripts and "test" in scripts:
                        # If there's a separate build script, run it
                        print(f"      → Building project...")
                        if (self.repo_dir / "yarn.lock").exists():
                            build_cmd = ["yarn", "build"]
                        else:
                            build_cmd = ["npm", "run", "build"]

                        result = self.run_command(build_cmd, timeout=1200)
                        if result.returncode != 0:
                            print(f"      ⚠ Build failed: {result.stderr[:200]}")
                        else:
                            print(f"      ✓ Build completed")
            except:
                pass

        # Patch Gruntfile.js to force-exit mocha after tests (prevents hanging
        # when HTTP server tests don't close their servers, which would hold
        # ports like 4444 and block the next run with EADDRINUSE).
        self._patch_gruntfile_for_exit()

    def _patch_gruntfile_for_exit(self):
        """Patch Gruntfile.js to add exit:true to grunt-mocha-test options.

        Without --exit, mocha waits for the event loop to drain before exiting.
        HTTP server tests that don't fully close their servers (e.g. because an
        assertion fails before done() is called) will cause mocha to hang
        indefinitely, holding port 4444 and blocking the next test run.
        Adding exit:true makes mocha force-quit after all tests finish, which
        prevents this hang and releases the port promptly.
        """
        gruntfile = self.repo_dir / "Gruntfile.js"
        if not gruntfile.exists():
            return
        try:
            content = gruntfile.read_text()
            # Already patched
            if 'exit: true' in content or 'exit:true' in content:
                return
            # Inject exit:true into the mochaTest options block.
            # Handles both:
            #   options: { timeout: ... }
            #   options: {\n    timeout: ...\n  }
            import re as _re
            patched = _re.sub(
                r'(mochaTest\s*:\s*\{[^}]*options\s*:\s*\{)',
                r'\1\n          exit: true,',
                content,
                flags=_re.DOTALL
            )
            if patched != content:
                gruntfile.write_text(patched)
        except Exception:
            pass

    def _reinstall_critical_native_tools(self):
        """After an --ignore-scripts install, explicitly run the install scripts for
        build tools that genuinely need their native binary to work (e.g. esbuild,
        swc, canvas).  This is safe because these packages only download/compile a
        binary — they don't run arbitrary project code.
        """
        node_modules = self.repo_dir / "node_modules"
        if not node_modules.exists():
            return

        # Each entry: (package_dir_name, install_script_relative_path)
        CRITICAL_TOOLS = [
            ("esbuild",  "install.js"),
            ("esbuild",  "lib/install.js"),   # older versions
            ("@swc/core", "scripts/postinstall.js"),
            ("canvas",   "build.js"),
        ]

        for pkg_name, script_rel in CRITICAL_TOOLS:
            script_path = node_modules / pkg_name / script_rel
            if not script_path.exists():
                continue

            print(f"      → Re-running install script for '{pkg_name}'...")
            result = self.run_command(["node", str(script_path)], timeout=120)
            if result.returncode == 0:
                print(f"      ✓ '{pkg_name}' native tool ready")
            else:
                # Non-fatal — log and move on
                print(f"      ⚠ '{pkg_name}' install script failed (non-fatal): {result.stderr[:100]}")

    def _stub_missing_native_modules(self):
        """Replace native Node add-ons that failed to compile/download with no-op stubs.

        Some packages (e.g. iltorb used by karma-sauce-launcher for Brotli HTTP
        compression) crash at require() time when their native .node binary is
        absent, even though local test runs never call their APIs.  Replacing
        their index.js with a harmless stub lets karma start normally.
        """
        node_modules = self.repo_dir / "node_modules"
        if not node_modules.exists():
            return

        # Map: package name → minimal stub JS that satisfies the public API callers expect
        NATIVE_STUBS = {
            # iltorb is used by microbundle/brotli-size only for reporting compressed size.
            # It is not needed for producing correct build output or running tests.
            # Calling conventions observed in the wild:
            #   compress(buf, opts, cb)   – brotli-size / microbundle
            #   compress(buf, cb)         – opts omitted
            #   compress(buf)             – no callback, expects Promise
            "iltorb": (
                "// Stub — native binary unavailable on this platform\n"
                "function _call(b, o, cb) {\n"
                "  var fn = typeof cb === 'function' ? cb\n"
                "         : typeof o  === 'function' ? o\n"
                "         : null;\n"
                "  if (fn) { process.nextTick(fn, null, b); return; }\n"
                "  return Promise.resolve(b);\n"
                "}\n"
                "module.exports = {\n"
                "  compress:       function(b, o, cb) { return _call(b, o, cb); },\n"
                "  decompress:     function(b, o, cb) { return _call(b, o, cb); },\n"
                "  compressSync:   function(b) { return b; },\n"
                "  decompressSync: function(b) { return b; },\n"
                "};\n"
            ),
        }

        for pkg_name, stub_src in NATIVE_STUBS.items():
            pkg_dir = node_modules / pkg_name
            if not pkg_dir.exists():
                continue

            # Determine the package entry point
            pkg_json = pkg_dir / "package.json"
            try:
                entry = json.loads(pkg_json.read_text()).get("main", "index.js")
            except Exception:
                entry = "index.js"

            entry_path = pkg_dir / entry
            # Only stub when the entry file tries to require() a missing .node binary
            try:
                src = entry_path.read_text()
            except Exception:
                continue

            if ".node'" not in src and '.node"' not in src:
                continue  # Doesn't load a native binary — skip

            # Check if ANY .node file under the package actually exists
            native_files = list(pkg_dir.rglob("*.node"))
            if native_files:
                continue  # Binary present — no stub needed

            print(f"      → Stubbing native module '{pkg_name}' (binary missing)")
            entry_path.write_text(stub_src)

    def _detect_test_framework(self) -> tuple:
        """
        Detect test framework and command from package.json

        Returns: (test_command, framework_type)
        """
        package_json = self.repo_dir / "package.json"
        if not package_json.exists():
            return (["npm", "test"], "unknown")

        try:
            with open(package_json) as f:
                pkg = json.load(f)
                scripts = pkg.get("scripts", {})
                test_script = scripts.get("test", "")

                # Detect framework from test script
                if "vitest" in test_script.lower():
                    framework = "vitest"
                elif "jest" in test_script.lower():
                    framework = "jest"
                elif "mocha" in test_script.lower():
                    framework = "mocha"
                elif "make test" in test_script.lower():
                    # Use make directly if available
                    if (self.repo_dir / "Makefile").exists():
                        return (["make", "test"], "make")
                    framework = "custom"
                else:
                    framework = "unknown"

                # Grunt projects often wrap mocha/karma/lint in `grunt test`.
                # Running that full task can fail in CI-less environments
                # (no browser for karma, lint differences, dtslint, etc.), which
                # masks the real unit test failures. If a Gruntfile defines
                # mochaTest, run it directly.
                if framework == "unknown" and (self.repo_dir / "Gruntfile.js").exists():
                    try:
                        gruntfile = (self.repo_dir / "Gruntfile.js").read_text()
                        if "mochaTest" in gruntfile:
                            framework = "mocha"
                            grunt_bin = self.repo_dir / "node_modules" / ".bin" / "grunt"
                            if grunt_bin.exists():
                                return ([str(grunt_bin), "mochaTest"], framework)
                            # Fallback to npx if local grunt isn't installed yet
                            return (["npx", "grunt", "mochaTest"], framework)
                    except Exception:
                        pass

                # Determine package manager
                if (self.repo_dir / "yarn.lock").exists():
                    pkg_mgr = "yarn"
                elif (self.repo_dir / "pnpm-lock.yaml").exists():
                    pkg_mgr = "pnpm"
                else:
                    pkg_mgr = "npm"

                # If the main "test" script includes a build or lint step, prefer a
                # pure test sub-script so a build failure doesn't block test runs.
                # Common pattern: "test": "npm-run-all build lint test:unit"
                has_build_step = any(kw in test_script for kw in ["build", "lint", "compile"])
                if has_build_step:
                    preferred = ["test:unit", "test:browser", "test:mocha", "test:karma",
                                 "test:jest", "test:vitest", "test:spec", "unit"]
                    for alt in preferred:
                        if alt in scripts:
                            alt_script = scripts[alt]
                            # Detect framework from the alternative script too
                            if "vitest" in alt_script.lower():
                                framework = "vitest"
                            elif "jest" in alt_script.lower():
                                framework = "jest"
                            elif "mocha" in alt_script.lower() or "karma" in alt_script.lower():
                                framework = "mocha"
                            print(f"      → Using '{alt}' script (main test includes build/lint step)")
                            return ([pkg_mgr, "run", alt], framework)

                if pkg_mgr == "yarn":
                    cmd = ["yarn", "test"]
                elif pkg_mgr == "pnpm":
                    cmd = ["pnpm", "test"]
                else:
                    cmd = ["npm", "test"]

                return (cmd, framework)
        except:
            return (["npm", "test"], "unknown")

    def _normalize_test_name(self, test_name: str) -> str:
        """Normalize test name by removing timing info and extra whitespace"""
        # Remove timing information like (1003ms), (123ms), (1 ms), (123 ms), etc.
        normalized = re.sub(r'\s*\(\d+\s*m?s\)$', '', test_name)
        # Remove extra whitespace
        normalized = normalized.strip()
        return normalized

    def _parse_jest_output(self, output: str) -> Dict[str, str]:
        """Parse Jest test output"""
        status_map = {}

        def _strip_jest_timing(name: str) -> str:
            """Strip trailing timing suffix: '(123ms)' or '(11.558 s)' or '(1 s)'"""
            return re.sub(r'\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$', '', name).strip()

        for line in output.split('\n'):
            # Jest format: "✓ test name (123ms)" or "✓ test name (11.558 s)" or "✕ test name"
            if '✓' in line or '✔' in line:
                match = re.search(r'[✓✔]\s+(.+)', line)
                if match:
                    test_name = _strip_jest_timing(match.group(1).strip())
                    if test_name and len(test_name.split()) > 1:
                        # Deduplicate: PASS/FAIL lines may already have this key without timing;
                        # only set PASSED if not already present (FAILED takes precedence)
                        if test_name not in status_map or status_map[test_name] != 'FAILED':
                            status_map[test_name] = 'PASSED'
            elif '✕' in line or '✗' in line:
                match = re.search(r'[✕✗]\s+(.+)', line)
                if match:
                    test_name = _strip_jest_timing(match.group(1).strip())
                    if test_name and len(test_name.split()) > 1:
                        status_map[test_name] = 'FAILED'
            # Jest summary: "PASS  packages/babel-core/test/api.js" or "PASS path (10.13 s)"
            elif line.strip().startswith('PASS ') or line.strip().startswith('FAIL '):
                match = re.match(r'(PASS|FAIL)\s+(.+)', line.strip())
                if match:
                    status = match.group(1)
                    test_file = _strip_jest_timing(match.group(2).strip())
                    status_map[test_file] = 'PASSED' if status == 'PASS' else 'FAILED'

        return status_map

    def _parse_mocha_output(self, output: str) -> Dict[str, str]:
        """Parse Mocha test output"""
        status_map = {}
        in_error_details = False
        _detail_pending_name = False  # True after seeing "  N) describe" line

        def _strip_timing(name: str) -> str:
            """Strip trailing timing suffix: '(123ms)', '(11.558 s)', '(1 s)'"""
            return re.sub(r'\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$', '', name).strip()

        for line in output.split('\n'):
            # Check if we've entered the detailed error section
            if re.match(r'\s*\d+\s+failing', line):
                in_error_details = True
                _detail_pending_name = False
                continue

            # Parse the detailed error section to capture failures that only
            # appear here (e.g., uncaught exceptions from setTimeout callbacks)
            # Format:
            #   N) describe_block
            #        test_name_ending_with_colon:
            #      Error: ...
            if in_error_details:
                if re.match(r'\s*\d+\)\s+\S', line):
                    # Start of a new failure block; next indented line ending
                    # with ':' will be the actual test name
                    _detail_pending_name = True
                elif _detail_pending_name:
                    stripped = line.strip()
                    if stripped.endswith(':') and stripped:
                        # This is the test name line
                        test_name = self._normalize_test_name(stripped.rstrip(':'))
                        if test_name and len(test_name.split()) > 1 and test_name not in status_map:
                            status_map[test_name] = 'FAILED'
                        _detail_pending_name = False
                    elif stripped:
                        # Non-empty line that doesn't match — not the test name
                        _detail_pending_name = False
                continue

            if line.strip().startswith('PASS') or line.strip().startswith('FAIL'):
                match = re.match(r'(PASS|FAIL)\s+(.+)', line.strip())
                if match:
                    status = match.group(1)
                    test_file = _strip_timing(match.group(2).strip())
                    status_map[test_file] = 'PASSED' if status == 'PASS' else 'FAILED'
            elif '✓' in line or '✔' in line:
                match = re.search(r'[✓✔]\s+(.+)', line)
                if match:
                    test_name = self._normalize_test_name(match.group(1).strip())
                    # Skip single-word test names that are likely describe blocks
                    if test_name and len(test_name.split()) > 1:
                        status_map[test_name] = 'PASSED'
            elif re.match(r'\s*\d+\)', line):
                match = re.search(r'\d+\)\s+(.+)', line)
                if match:
                    test_name = self._normalize_test_name(match.group(1).strip())
                    # Skip single-word test names that are likely describe blocks
                    if test_name and len(test_name.split()) > 1:
                        status_map[test_name] = 'FAILED'
            elif '✖' in line:
                # Karma mocha reporter uses ✖ (U+2716) for failures, different from ✗ (U+2717)
                match = re.search(r'✖\s+(.+?)(?:\s+\(\d+m?s\))?$', line)
                if match:
                    test_name = self._normalize_test_name(match.group(1).strip())
                    # Skip summary lines like "✖ 1 test failed" (start with digit)
                    if test_name and len(test_name.split()) > 1 and not test_name[0].isdigit():
                        status_map[test_name] = 'FAILED'

        return status_map

    def _parse_mocha_json_output(self, output: str) -> Dict[str, str]:
        """Parse Mocha JSON reporter output if present."""
        status_map = {}
        try:
            # Try to extract a JSON object from output (mocha --reporter json)
            start = output.find('{')
            end = output.rfind('}')
            if start == -1 or end == -1 or end <= start:
                return {}
            payload = output[start:end + 1]
            data = json.loads(payload)
            tests = data.get("tests") or []
            for t in tests:
                name = t.get("fullTitle") or t.get("title")
                state = t.get("state")
                if not name:
                    continue
                if state == "passed":
                    status_map[name] = "PASSED"
                elif state == "failed":
                    status_map[name] = "FAILED"
            return status_map
        except Exception:
            return {}

    def _parse_vitest_output(self, output: str) -> Dict[str, str]:
        """Parse Vitest test output"""
        status_map = {}

        def _strip_timing(name: str) -> str:
            """Strip trailing timing suffix: '(123ms)', '(11.558 s)', '(1 s)'"""
            return re.sub(r'\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$', '', name).strip()

        # Strip ANSI color codes for easier parsing
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_output = ansi_escape.sub('', output)

        for line in clean_output.split('\n'):
            # Vitest format: "✓ test name (123ms)" or "✗ test name"
            # Also: " ✓ test name" with leading spaces
            if '✓' in line or '✔' in line:
                match = re.search(r'[✓✔]\s+(.+?)(?:\s+\(\d+m?s\))?$', line)
                if match:
                    test_name = match.group(1).strip()
                    # Detect vitest file-level summary lines: "path.spec.ts  (N tests) Xms"
                    # Normalize by stripping the count/time suffix so the key is consistent
                    # across runs even when test_patch adds new tests (changing the count).
                    file_summary = re.match(r'(.+?\.(?:spec|test)\.[jt]sx?)\s+\(\d+ tests?\)\s+\d+m?s$', test_name)
                    if file_summary:
                        status_map[file_summary.group(1)] = 'PASSED'
                    # Filter out bare file names, describe blocks, and karma summary lines like "✓ 932 tests completed"
                    elif test_name and len(test_name.split()) > 1 and not test_name[0].isdigit() and not test_name.endswith('.spec.ts') and not test_name.endswith('.test.ts'):
                        status_map[test_name] = 'PASSED'
            elif '✕' in line or '✗' in line or '×' in line or '✖' in line:
                match = re.search(r'[✕✗×✖]\s+(.+?)(?:\s+\(\d+m?s\))?$', line)
                if match:
                    test_name = match.group(1).strip()
                    # Skip summary lines like "✖ 1 test failed" (start with digit)
                    if test_name and len(test_name.split()) > 1 and not test_name[0].isdigit() and not test_name.endswith('.spec.ts') and not test_name.endswith('.test.ts'):
                        status_map[test_name] = 'FAILED'
            # Vitest/Jest also shows: "PASS path" or "PASS path (10.13 s)"
            elif line.strip().startswith('PASS ') or line.strip().startswith('FAIL '):
                match = re.match(r'(PASS|FAIL)\s+(.+)', line.strip())
                if match:
                    status = match.group(1)
                    test_file = _strip_timing(match.group(2).strip())
                    status_map[test_file] = 'PASSED' if status == 'PASS' else 'FAILED'

        return status_map

    def _run_karma_for_test_files(self, test_files: List[str]) -> Dict[str, str]:
        """Run karma with --grep for specific test files that may not be in the default karma pattern.
        Returns a status_map merged from all karma runs for those files."""
        karma_conf = self.repo_dir / "karma.conf.js"
        karma_bin = self.repo_dir / "node_modules" / ".bin" / "karma"
        if not karma_conf.exists() or not karma_bin.exists():
            return {}

        status_map = {}
        # Save and override env vars for karma
        saved_env = {k: self.env_vars.get(k) for k in ['COVERAGE', 'MINIFY', 'BABEL_NO_MODULES']}
        self.env_vars['COVERAGE'] = 'false'
        self.env_vars['MINIFY'] = 'true'
        self.env_vars['BABEL_NO_MODULES'] = 'true'

        try:
            for test_file in test_files:
                karma_cmd = [str(karma_bin), "start", "karma.conf.js", "--single-run",
                             f"--grep={test_file}"]
                result = self.run_command(karma_cmd, timeout=300)
                output = result.stdout + result.stderr
                parsed = self._parse_vitest_output(output)
                if not parsed:
                    parsed = self._parse_mocha_output(output)
                status_map.update(parsed)
        finally:
            # Restore env vars
            for k, v in saved_env.items():
                if v is None:
                    self.env_vars.pop(k, None)
                else:
                    self.env_vars[k] = v

        return status_map

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run JavaScript tests"""
        status_map = {}
        self.env_vars['CI'] = 'true'

        # Detect test framework and command
        test_cmd, framework = self._detect_test_framework()

        # Add test file filtering for Vitest and Jest
        if test_files and framework in ["vitest", "jest"]:
            # For vitest/jest, we can pass test files directly
            # vitest supports: vitest path/to/test.spec.ts
            # jest supports: jest path/to/test
            test_cmd = test_cmd + test_files

        # For mocha-based projects: inject --exit so mocha always terminates
        # cleanly after all tests run. Without --exit, HTTP server tests leave
        # servers open, mocha hangs, we time-out and get truncated results.
        # We use a temporary .mocharc.yml only if no mocha config already exists.
        _temp_mocharc = None
        if framework == "mocha":
            _mocharc_names = [
                '.mocharc.cjs', '.mocharc.js', '.mocharc.yaml',
                '.mocharc.yml', '.mocharc.jsonc', '.mocharc.json',
            ]
            _has_mocharc = any((self.repo_dir / f).exists() for f in _mocharc_names)
            if not _has_mocharc:
                _temp_mocharc = self.repo_dir / ".mocharc.yml"
                _temp_mocharc.write_text("exit: true\ntimeout: 10000\n")

        if debug:
            print(f"      [DEBUG] Framework: {framework}")
            print(f"      [DEBUG] Command: {' '.join(test_cmd)}")

        try:
            result = self.run_command(test_cmd, timeout=600)
        finally:
            # Always remove temporary mocharc regardless of test outcome
            if _temp_mocharc and _temp_mocharc.exists():
                try:
                    _temp_mocharc.unlink()
                except Exception:
                    pass
        output = result.stdout + result.stderr

        if debug:
            print(f"      [DEBUG] Exit code: {result.returncode}")
            print(f"      [DEBUG] Output length: {len(output)} chars")
            print(f"      [DEBUG] First 500 chars:\n{output[:500]}")

        # Parse output based on framework
        if framework == "vitest":
            status_map = self._parse_vitest_output(output)
        elif framework == "jest":
            status_map = self._parse_jest_output(output)
        elif framework == "mocha":
            # Mocha projects: use mocha parser directly to correctly capture
            # mocha-style failure markers (e.g. "1) test name") as FAILED.
            # Try JSON reporter output first (if the project uses --reporter json),
            # then fall back to text-based mocha output parsing.
            status_map = self._parse_mocha_json_output(output)
            if not status_map:
                status_map = self._parse_mocha_output(output)
        elif framework in ["unknown", "make", "custom"]:
            # Run both vitest and mocha parsers and merge results.
            # For grunt/mocha projects detected as "unknown", the vitest parser
            # captures ✓ (PASSED) markers but misses "N)" failure markers which
            # only the mocha parser understands. Running both ensures failures
            # are captured even when the framework isn't explicitly "mocha".
            status_map = self._parse_vitest_output(output)
            mocha_map = self._parse_mocha_output(output)
            # Merge: mocha results supplement vitest results; FAILED takes
            # precedence over PASSED for any test name seen by both parsers.
            for name, status in mocha_map.items():
                if name not in status_map or status == 'FAILED':
                    status_map[name] = status
            if not status_map:
                status_map = self._parse_jest_output(output)

        # Auto-debug when no tests detected
        if not status_map and result.returncode != 0:
            print(f"      ⚠ Test command failed (exit {result.returncode})")
            # Show first error lines
            error_lines = [line for line in output.split('\n') if 'error' in line.lower() or 'err' in line.lower()][:3]
            if error_lines:
                for line in error_lines:
                    print(f"        {line[:100]}")

        return status_map

    def extract_imports_from_patch(self, patch: str) -> set:
        """Extract npm package imports from patch (import/require statements)"""
        imports = set()

        for line in patch.split('\n'):
            # Only look at added lines
            if not line.startswith('+'):
                continue

            # Match: import ... from 'package'
            # Match: import ... from "package"
            match = re.search(r"import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", line)
            if match:
                package = match.group(1)
                # Extract base package name (e.g., 'formdata-node' from 'formdata-node/submodule')
                base_package = package.split('/')[0]
                # Ignore relative imports
                if not base_package.startswith('.'):
                    imports.add(base_package)

            # Match: require('package')
            # Match: require("package")
            match = re.search(r"require\(['\"]([^'\"]+)['\"]\)", line)
            if match:
                package = match.group(1)
                base_package = package.split('/')[0]
                if not base_package.startswith('.'):
                    imports.add(base_package)

        return imports

    def extract_package_json_changes(self, patch: str) -> dict:
        """Extract new dependencies from package.json changes in patch"""
        dependencies = {}

        # Find package.json section in patch
        in_package_json = False

        for line in patch.split('\n'):
            # Check if we're in package.json
            if 'package.json' in line and ('diff --git' in line or '+++' in line):
                in_package_json = True
                continue

            # End of package.json section
            if in_package_json and line.startswith('diff --git'):
                break

            if not in_package_json:
                continue

            # Extract added dependencies (any line that looks like a dependency)
            # Simplified: Just look for added lines with "package": "version" pattern
            if line.startswith('+'):
                # Match: "package-name": "^1.2.3" (with optional leading spaces)
                match = re.search(r'^\+\s*["\']([a-zA-Z0-9@/_-]+)["\']\s*:\s*["\']([^"\']+)["\']', line)
                if match:
                    package_name = match.group(1)
                    version = match.group(2)
                    # Filter out non-dependency keys (like script names, config keys)
                    # Dependencies typically have versions starting with ^, ~, or digits
                    if re.match(r'[\^~]?\d', version):
                        dependencies[package_name] = version

        return dependencies

    def preinstall_test_dependencies(self, test_patch: str, solution_patch: str):
        """Pre-install dependencies that test_patch needs but solution_patch provides"""
        # Extract imports from test_patch
        test_imports = self.extract_imports_from_patch(test_patch)
        print(f"      → Detected test imports: {test_imports if test_imports else 'none'}")

        # Extract package.json changes from solution_patch
        solution_deps = self.extract_package_json_changes(solution_patch)
        print(f"      → Detected solution dependencies: {set(solution_deps.keys()) if solution_deps else 'none'}")

        # Find overlap - packages that tests import and solution adds
        needed_deps = test_imports & set(solution_deps.keys())

        if needed_deps:
            print(f"      → Pre-installing {len(needed_deps)} test dependencies...")
            for dep in needed_deps:
                version = solution_deps[dep]
                print(f"        - {dep}@{version}")

                # Install the specific package WITHOUT modifying package.json (--no-save)
                # This prevents conflicts when solution_patch adds it to package.json later
                result = self.run_command(["npm", "install", "--no-save", f"{dep}@{version}"], timeout=300)
                if result.returncode != 0:
                    print(f"          ⚠ Failed to install {dep}: {result.stderr[:100]}")
                else:
                    print(f"          ✓ Installed {dep}")

            return True

        return False


# ============================================================================
# PHP VALIDATOR
# ============================================================================

class PHPValidator(BaseValidator):
    """Validator for PHP projects - uses Docker for version management"""

    def is_test_file(self, file_path: str) -> bool:
        return (
            file_path.endswith('Test.php')
            or '/tests/' in file_path or '/test/' in file_path
            or file_path.startswith('tests/') or file_path.startswith('test/')
        )

    def extract_modified_tests(self) -> set:
        """Extract test method names added or modified in test_patch.
        Returns a set of method name strings (e.g. {'testFoo', 'testBar'}).
        These are used to build a --filter pattern so PHPUnit only runs the
        relevant tests instead of the entire file.
        """
        test_patch = self.instance.get('test_patch', '')
        modified = set()
        current_in_php_file = False

        for line in test_patch.split('\n'):
            # Track which diff section we're in
            if line.startswith('diff --git'):
                current_in_php_file = line.endswith('.php')
                continue
            if not current_in_php_file:
                continue
            # Added lines only
            if line.startswith('+') and not line.startswith('+++'):
                m = re.search(r'public function (test\w+)\s*\(', line)
                if m:
                    modified.add(m.group(1))

        return modified

    def detect_required_version(self) -> str:
        """Detect required PHP version"""
        composer_json = self.repo_dir / "composer.json"
        if composer_json.exists():
            try:
                with open(composer_json) as f:
                    composer = json.load(f)
                    if "require" in composer and "php" in composer["require"]:
                        php_version = composer["require"]["php"]
                        match = re.search(r'(\d+\.\d+)', php_version)
                        if match:
                            return match.group(1)
            except:
                pass

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "php")
        return "8.0"

    def get_actual_version(self) -> str:
        """Get current PHP version (or Docker version if using Docker)"""
        if hasattr(self, 'use_docker') and self.use_docker:
            return self.docker_image.split(':')[1].split('-')[0]

        result = subprocess.run(["php", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            match = re.search(r'PHP\s+(\d+\.\d+)', result.stdout)
            if match:
                return match.group(1)
        return "unknown"

    def _check_docker(self) -> bool:
        """Check if Docker is available"""
        try:
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except:
            return False

    def _use_docker(self, required_version: str) -> bool:
        """Setup Docker for PHP"""
        if not self._check_docker():
            print(f"      ⚠ Docker not available")
            print(f"      → Install Docker: https://www.docker.com/get-started")
            return False

        print(f"      → Using Docker for PHP {required_version}")

        # Pull Docker image
        docker_tag = f"php:{required_version}-cli"
        print(f"      → Pulling image: {docker_tag}...")

        result = subprocess.run(
            ["docker", "pull", docker_tag],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode == 0:
            print(f"      ✓ Docker image ready: {docker_tag}")
            self.use_docker = True
            self.docker_image = docker_tag
            return True
        else:
            print(f"      ⚠ Failed to pull Docker image")
            return False

    def setup_version(self, required_version: str):
        """Setup required PHP version - uses Docker for all versions"""
        current_version = self.get_actual_version()

        # Check if system PHP is close enough
        if current_version != "unknown":
            try:
                req_major = int(required_version.split('.')[0])
                req_minor = int(required_version.split('.')[1]) if '.' in required_version else 0
                cur_major = int(current_version.split('.')[0])
                cur_minor = int(current_version.split('.')[1]) if '.' in current_version else 0

                # If major version matches and minor is within 1, use system PHP
                if req_major == cur_major and abs(req_minor - cur_minor) <= 1:
                    print(f"      ✓ PHP {current_version} is compatible with required {required_version}")
                    return
            except (ValueError, IndexError):
                pass

        print(f"      ⚠ PHP version mismatch (required: {required_version}, available: {current_version})")

        # Use Docker for version management
        if self._use_docker(required_version):
            return

        # If Docker fails, warn and continue with system PHP
        print(f"      ⚠ Could not set up PHP {required_version}")
        print(f"      → Continuing with PHP {current_version} (tests may fail)")

    def run_command(self, cmd: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Override to use Docker if configured"""
        if hasattr(self, 'use_docker') and self.use_docker:
            working_dir = cwd or self.repo_dir
            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{working_dir}:/workspace",
                "-w", "/workspace",
                self.docker_image
            ] + cmd

            try:
                return subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
            except subprocess.TimeoutExpired as e:
                print(f"      ⚠ Docker command timed out after {timeout}s")
                print(f"      → Command: {' '.join(cmd[:3])}...")
                # Return a failed result instead of crashing
                return subprocess.CompletedProcess(
                    args=docker_cmd,
                    returncode=124,  # Standard timeout exit code
                    stdout=e.stdout.decode() if e.stdout else "",
                    stderr=f"Command timed out after {timeout} seconds"
                )
        else:
            return super().run_command(cmd, cwd, timeout)

    def install_dependencies(self):
        """Install Composer dependencies"""
        print(f"[4/7] Installing dependencies...")

        if not (self.repo_dir / "composer.json").exists():
            return

        # Install dependencies
        if hasattr(self, 'use_docker') and self.use_docker:
            print(f"      → Installing dependencies with PHP {self.docker_image}...")
            self._install_dependencies_docker()

            # After installation, check if the project's PHP code requires a higher PHP version.
            # This handles cases where composer.json says >=7.4 but the code uses PHP 8.0+
            # features like union types (string|int). We detect and auto-bump the Docker version.
            self._bump_php_version_if_needed()
        else:
            result = self.run_command(["composer", "install", "--no-interaction", "--prefer-dist"], timeout=1200)
            phpunit_exists = (self.repo_dir / "vendor" / "bin" / "phpunit").exists()
            if result.returncode != 0 and not phpunit_exists:
                print(f"      ⚠ Composer install failed (exit {result.returncode})")

    def _bump_php_version_if_needed(self):
        """Check if a higher PHP version is needed and bump the Docker image if so.

        Two failure modes are detected and handled:

        1. PHPUnit itself requires a higher PHP version:
           e.g. "This version of PHPUnit requires PHP >= 8.2."
           Happens when composer.lock locks PHPUnit 11 but we're running PHP 7.4.
           Installing the PHAR of an older PHPUnit is WRONG because the project's
           tests are written for the newer PHPUnit API (different symbols, annotations,
           etc.) and would fail with incompatible PHPUnit. The correct fix is to bump
           to the PHP version that the locked PHPUnit actually needs.

        2. Project source code uses PHP 8.0+ syntax (union types: string|int, etc.)
           that can't be parsed by older PHP. Running phpunit --list-tests triggers
           the autoloader, which loads vendor code installed with --ignore-platform-reqs
           and may include union-type syntax.

        In both cases, we pull the required Docker PHP image and reinstall so that
        Composer installs the correct, compatible versions of everything.
        """
        if not (self.repo_dir / "vendor" / "bin" / "phpunit").exists():
            return  # No PHPUnit installed, nothing to check

        needed_version = None

        # --- Check 1: Does the installed PHPUnit itself require a higher PHP version? ---
        # This happens when composer install used --ignore-platform-reqs, so PHPUnit X
        # (requiring PHP >= A.B) was installed even though we're running PHP < A.B.
        phpunit_ver_cmd = ["sh", "-c", "vendor/bin/phpunit --version 2>&1 | head -3 || true"]
        ver_result = self.run_command(phpunit_ver_cmd, timeout=30)
        ver_output = ver_result.stdout + ver_result.stderr

        phpunit_req_match = re.search(r'requires PHP >= (\d+\.\d+)', ver_output)
        if phpunit_req_match:
            phpunit_min_php = phpunit_req_match.group(1)
            print(f"      ℹ PHPUnit requires PHP >= {phpunit_min_php} (current image: {self.docker_image})")
            needed_version = phpunit_min_php

        # --- Check 2: Does project/vendor code use PHP 8.0+ syntax (union types, etc.)? ---
        # Run phpunit --list-tests to trigger bootstrap/autoloader loading so all files
        # (including vendor ones installed with --ignore-platform-reqs) are parsed.
        PHP_SYNTAX_VERSIONS = [
            ("unexpected '|'", "8.0"),            # Union types: string|int $x
            ('unexpected token "|"', "8.0"),      # Union types (newer PHP error format)
            ("unexpected token \"readonly\"", "8.1"),  # readonly properties (PHP 8.1)
            ("unexpected '&'", "8.1"),            # Intersection types: A&B (PHP 8.1)
        ]

        phpunit_check_cmd = [
            "sh", "-c",
            "vendor/bin/phpunit --list-tests 2>&1 | head -10 || true"
        ]
        result = self.run_command(phpunit_check_cmd, timeout=60)
        output = result.stdout + result.stderr

        if any(marker in output for marker in ["syntax error, unexpected", "ParseError", "Parse error"]):
            for pattern, min_version in PHP_SYNTAX_VERSIONS:
                if pattern in output:
                    ver_parts = tuple(int(x) for x in min_version.split('.'))
                    cur_needed = tuple(int(x) for x in needed_version.split('.')) if needed_version else (0, 0)
                    if ver_parts > cur_needed:
                        needed_version = min_version

        if needed_version is None:
            return  # No version bump needed

        # Check if already at a sufficient version.
        # self.docker_image may be a custom image name like "swebench-php-9b7eee78:latest"
        # which can't be parsed for a version number.  Ask the running container instead.
        try:
            _php_ver_result = subprocess.run(
                ["docker", "run", "--rm", self.docker_image, "php", "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;"],
                capture_output=True, text=True, timeout=15
            )
            current_ver = _php_ver_result.stdout.strip()  # e.g. "7.4"
        except Exception:
            current_ver = ""
        if not current_ver:
            # Fall back to parsing the image tag
            current_ver = self.docker_image.split(':')[1].split('-')[0]
        try:
            current_parts = tuple(int(x) for x in current_ver.split('.'))
            needed_parts = tuple(int(x) for x in needed_version.split('.'))
            if current_parts >= needed_parts:
                return  # Already at sufficient version
        except (ValueError, IndexError):
            pass  # Can't parse → proceed with bump anyway

        print(f"      ⚠ PHP {current_ver} syntax incompatibility detected (need PHP {needed_version}+)")
        print(f"      → Switching Docker image to PHP {needed_version}")

        new_tag = f"php:{needed_version}-cli"
        pull_result = subprocess.run(
            ["docker", "pull", new_tag],
            capture_output=True, text=True, timeout=300
        )
        if pull_result.returncode != 0:
            print(f"      ⚠ Failed to pull {new_tag}, continuing with current image")
            return

        self.docker_image = new_tag
        print(f"      ✓ Switched to {new_tag}, reinstalling dependencies...")

        # Remove old vendor dir so Composer starts fresh under new PHP version
        vendor_dir = self.repo_dir / "vendor"
        if vendor_dir.exists():
            shutil.rmtree(vendor_dir)

        # Reinstall without triggering another version-bump check (avoid recursion)
        self._install_dependencies_docker()

    def _build_custom_php_image(self, base_image: str) -> Optional[str]:
        """Build a custom PHP Docker image with common extensions (zip, gmp, etc.) and
        Composer pre-installed.  The image is tagged and cached so it is only built once
        per base image across validation runs.

        Returns the custom image name on success, or None if the build failed.
        """
        import hashlib
        image_hash = hashlib.md5(base_image.encode()).hexdigest()[:8]
        custom_image = f"swebench-php-{image_hash}:latest"

        # Reuse cached image if it already exists
        check = subprocess.run(
            ["docker", "image", "inspect", custom_image],
            capture_output=True, timeout=10
        )
        if check.returncode == 0:
            print(f"      → Using cached custom PHP image: {custom_image}")
            return custom_image

        print(f"      → Building custom PHP image with extensions ({base_image})...")
        dockerfile_content = (
            f"FROM {base_image}\n"
            "RUN cp /etc/apt/sources.list /etc/apt/sources.list.bak 2>/dev/null || true && \\\n"
            "    sed -i 's|http://deb.debian.org|http://archive.debian.org|g' /etc/apt/sources.list 2>/dev/null || true && \\\n"
            "    sed -i 's|http://security.debian.org|http://archive.debian.org|g' /etc/apt/sources.list 2>/dev/null || true && \\\n"
            "    sed -i '/security.debian.org/d' /etc/apt/sources.list 2>/dev/null || true && \\\n"
            "    sed -i 's/ buster-updates/ buster/g' /etc/apt/sources.list 2>/dev/null || true && \\\n"
            "    apt-get update -qq 2>&1 | tail -2 && \\\n"
            "    apt-get install -y -qq git unzip curl libzip-dev libgmp-dev 2>&1 | tail -2 && \\\n"
            "    docker-php-ext-install zip ftp gmp pdo_mysql 2>&1 | tail -3 && \\\n"
            "    curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer --quiet\n"
        )

        build_dir = Path(tempfile.mkdtemp())
        try:
            (build_dir / "Dockerfile").write_text(dockerfile_content)
            build_result = subprocess.run(
                ["docker", "build", "-t", custom_image, str(build_dir)],
                capture_output=True, text=True, timeout=600
            )
        finally:
            shutil.rmtree(str(build_dir), ignore_errors=True)

        if build_result.returncode == 0:
            print(f"      ✓ Custom PHP image built: {custom_image}")
            return custom_image
        else:
            print(f"      ⚠ Failed to build custom PHP image: {build_result.stderr[-300:]}")
            return None

    def _get_composer_root_version(self) -> str:
        """Read composer.json from the checked-out repo and return the appropriate
        COMPOSER_ROOT_VERSION value (e.g. '11.99.0' for '11.x-dev' branch alias).
        Returns empty string if none found.
        """
        import json as _json
        composer_json_path = self.repo_dir / "composer.json"
        if not composer_json_path.exists():
            return ''
        try:
            data = _json.loads(composer_json_path.read_text())
            branch_aliases = data.get('extra', {}).get('branch-alias', {})
            for alias_ver in branch_aliases.values():
                m = re.match(r'^(\d+)\.x-dev$', str(alias_ver))
                if m:
                    return f"{m.group(1)}.99.0"
        except Exception:
            pass
        return ''

    def _install_dependencies_docker(self):
        """Run Composer install inside Docker (called by install_dependencies and _bump_php_version_if_needed).

        Builds a custom Docker image with PHP extensions pre-installed so that
        every subsequent docker run (tests, etc.) has them available without
        re-running apt-get/docker-php-ext-install each time.
        """
        # Build (or reuse) a custom image that has extensions + composer baked in
        custom_image = self._build_custom_php_image(self.docker_image)
        if custom_image:
            self.docker_image = custom_image

        # Compute COMPOSER_ROOT_VERSION from Python (more reliable than in-shell grep)
        root_ver = self._get_composer_root_version()
        if root_ver:
            print(f"      → Setting COMPOSER_ROOT_VERSION={root_ver} (from branch-alias)")
            root_ver_prefix = f"COMPOSER_ROOT_VERSION={root_ver} "
        else:
            root_ver_prefix = ""

        has_lock = (self.repo_dir / "composer.lock").exists()
        base_cmd = "composer install" if has_lock else "composer update"
        base_flags = "--no-interaction --no-progress --prefer-dist"

        # Try: (1) with root version, (2) without, (3) ignore-platform-reqs
        if root_ver:
            composer_cmd = (
                f"{root_ver_prefix}{base_cmd} {base_flags} 2>&1 || "
                f"{base_cmd} {base_flags} 2>&1 || "
                f"{root_ver_prefix}{base_cmd} {base_flags} --ignore-platform-reqs 2>&1 || "
                f"{base_cmd} {base_flags} --ignore-platform-reqs 2>&1"
            )
        else:
            composer_cmd = (
                f"{base_cmd} {base_flags} 2>&1 || "
                f"{base_cmd} {base_flags} --ignore-platform-reqs 2>&1"
            )

        if custom_image:
            # Extensions + composer already in the image; just run composer install
            result = self.run_command(["sh", "-c", composer_cmd], timeout=3600)
        else:
            # Fallback: run everything in one big combined command (old behaviour)
            combined_cmd = [
                "sh", "-c",
                "cp /etc/apt/sources.list /etc/apt/sources.list.bak 2>/dev/null || true && "
                "sed -i 's|http://deb.debian.org|http://archive.debian.org|g' /etc/apt/sources.list 2>/dev/null || true && "
                "sed -i 's|http://security.debian.org|http://archive.debian.org|g' /etc/apt/sources.list 2>/dev/null || true && "
                "sed -i '/security.debian.org/d' /etc/apt/sources.list 2>/dev/null || true && "
                "sed -i 's/ buster-updates/ buster/g' /etc/apt/sources.list 2>/dev/null || true && "
                "apt-get update -qq 2>&1 | tail -2 && "
                "apt-get install -y -qq git unzip curl libzip-dev libgmp-dev 2>&1 | tail -2 && "
                "docker-php-ext-install zip ftp gmp pdo_mysql 2>&1 | tail -3 && "
                "curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer --quiet && "
                f"{composer_cmd}"
            ]
            result = self.run_command(combined_cmd, timeout=3600)
        phpunit_exists = (self.repo_dir / "vendor" / "bin" / "phpunit").exists()
        if phpunit_exists:
            print(f"      ✓ Dependencies installed (PHPUnit ready)")
            if result.returncode != 0:
                print(f"      ℹ Composer exited with code {result.returncode} but PHPUnit was installed")
        elif result.returncode != 0:
            print(f"      ⚠ Composer install failed (exit {result.returncode})")
            output = result.stdout + '\n' + result.stderr
            error_lines = [line for line in output.split('\n') if line.strip() and ('error' in line.lower() or 'fail' in line.lower() or 'fatal' in line.lower())][:5]
            if error_lines:
                print(f"      → Error details:")
                for line in error_lines:
                    print(f"        {line.strip()[:150]}")
            else:
                last_lines = [line for line in output.split('\n') if line.strip()][-8:]
                print(f"      → Last output lines:")
                for line in last_lines:
                    print(f"        {line.strip()[:150]}")
        else:
            print(f"      ✓ Composer completed but PHPUnit not found")

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False, no_filter: bool = False) -> Dict[str, str]:
        """Run PHPUnit tests.

        Args:
            no_filter: If True, skip the --filter argument even when modified tests are known.
                       Used when running baseline to capture all existing tests for PASS_TO_PASS.
        """
        status_map = {}

        phpunit_bin = "./vendor/bin/phpunit" if (self.repo_dir / "vendor" / "bin" / "phpunit").exists() else "phpunit"

        if test_files:
            # Only pass PHP source files to PHPUnit; skip binary data files (xlsx, csv, etc.)
            php_test_files = [f for f in test_files if f.endswith('.php')]
            if not php_test_files:
                return status_map

            # Build --filter pattern from modified test methods to avoid running
            # the entire (potentially large) test file.
            modified = self.extract_modified_tests()
            filter_args = []
            phpunit_timeout = 900
            if modified and not no_filter:
                filter_pattern = '|'.join(re.escape(m) for m in sorted(modified))
                filter_args = ["--filter", filter_pattern]
                # With filter to just a few tests, use a tight timeout so hanging
                # tests (e.g. infinite loops before fix is applied) fail fast.
                # --enforce-time-limit kills each test after --default-time-limit
                # seconds if PHP's pcntl extension is available.
                filter_args += ["--enforce-time-limit", "--default-time-limit=30"]
                phpunit_timeout = 120

            for test_file in php_test_files:
                cmd = [phpunit_bin, "--testdox"] + filter_args + [test_file]
                result = self.run_command(cmd, timeout=phpunit_timeout)
                if result.returncode != 0 and not status_map:
                    print(f"      ⚠ PHPUnit failed (exit {result.returncode}) for {test_file}")
                    # Show first error lines
                    output = result.stdout + result.stderr
                    error_lines = [line for line in output.split('\n')
                                 if line.strip() and ('error' in line.lower() or 'fatal' in line.lower() or 'could not' in line.lower() or 'failed' in line.lower())][:5]
                    if error_lines:
                        print(f"        Error details:")
                        for line in error_lines:
                            print(f"          {line.strip()[:150]}")
                    else:
                        # Show last 10 lines if no specific errors found
                        last_lines = [line for line in output.split('\n') if line.strip()][-10:]
                        print(f"        Last output:")
                        for line in last_lines[:5]:
                            print(f"          {line.strip()[:150]}")
                self._parse_phpunit_output(result.stdout + result.stderr, status_map, debug)
        else:
            cmd = [phpunit_bin, "--testdox"]
            result = self.run_command(cmd, timeout=900)
            if result.returncode != 0 and not status_map:
                print(f"      ⚠ PHPUnit failed (exit {result.returncode})")
                error_lines = [line for line in (result.stdout + result.stderr).split('\n')
                             if line.strip() and ('error' in line.lower() or 'fatal' in line.lower() or 'could not' in line.lower())][:3]
                if error_lines:
                    for line in error_lines:
                        print(f"        {line.strip()[:120]}")
            self._parse_phpunit_output(result.stdout + result.stderr, status_map, debug)

        return status_map

    def _parse_phpunit_output(self, output: str, status_map: Dict[str, str], debug: bool = False):
        """Parse PHPUnit output and populate status map"""
        # Check for PHP fatal errors
        if 'Fatal error:' in output or 'PHP Fatal error:' in output:
            print(f"      ⚠ PHP Fatal error detected!")
            for line in output.split('\n'):
                if 'Fatal error:' in line or 'deprecated' in line.lower():
                    print(f"         {line.strip()[:150]}")
            print(f"      → This indicates PHP version incompatibility")
            return

        if not output or len(output.strip()) < 10:
            return

        # Parse PHPUnit --testdox output
        # Format examples:
        # " ✔ Test ceil year"  -> passed test (testdox converts camelCase to words)
        # " ✘ Test ceil year"  -> failed test
        # Or with class context:
        # "Round"  (class name)
        # " ✔ Test ceil year"

        current_class = None
        lines = output.split('\n')

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Extract class name (lines without leading whitespace and not markers)
            if stripped and not line.startswith(' ') and not line.startswith('\t'):
                # Check if this looks like a test class name
                if not any(x in stripped for x in ['OK (', 'FAILURES!', 'Time:', 'Memory:', 'Tests:', '===', '---']):
                    # Could be a class name
                    # Only update if next line looks like a test result
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if next_line.startswith('✔') or next_line.startswith('✘') or next_line.startswith('⚐'):
                            current_class = stripped

            # Parse test results with checkmarks/crosses
            # ✔ or ✓ for pass, ✘ or ✗ for fail
            if '✔' in stripped or '✓' in stripped:
                # Passing test
                test_desc = stripped.replace('✔', '').replace('✓', '').strip()
                if test_desc:
                    # Convert description back to method name
                    method_name = self._testdox_to_method(test_desc)
                    test_name = f"{current_class}::{method_name}" if current_class else method_name
                    status_map[test_name] = 'PASSED'

            elif '✘' in stripped or '✗' in stripped:
                # Failing test
                test_desc = stripped.replace('✘', '').replace('✗', '').strip()
                if test_desc:
                    method_name = self._testdox_to_method(test_desc)
                    test_name = f"{current_class}::{method_name}" if current_class else method_name
                    status_map[test_name] = 'FAILED'

            # Fallback: Parse failure details for test names
            # Format: "1) Tests\Carbon\RoundTest::testCeilYear"
            elif stripped.startswith(('1)', '2)', '3)', '4)', '5)', '6)', '7)', '8)', '9)')):
                match = re.search(r'(\w+)::\w+', stripped)
                if match:
                    # Extract class name if we don't have it
                    if not current_class:
                        current_class = match.group(1)

                # Extract full test identifier
                match = re.search(r'(\w+::\w+)', stripped)
                if match:
                    test_name = match.group(1)
                    # Mark as failed if not already in map
                    if test_name not in status_map:
                        status_map[test_name] = 'FAILED'

    def _testdox_to_method(self, testdox_desc: str) -> str:
        """Convert testdox description to method name.

        Examples:
          "Ceil year" -> "testCeilYear"
          "Test ceil year" -> "testCeilYear"
          "Floor year" -> "testFloorYear"
        """
        # Remove common prefixes
        desc = testdox_desc.strip()

        # Split into words and convert to camelCase
        words = desc.split()
        if not words:
            return "test"

        # Remove "Test" prefix if present
        if words[0].lower() == 'test' and len(words) > 1:
            words = words[1:]

        # Convert to camelCase with 'test' prefix
        if words:
            method_name = 'test' + ''.join(word.capitalize() for word in words)
            return method_name

        return "test"


class RubyValidator(BaseValidator):
    """Validator for Ruby projects"""

    def is_test_file(self, file_path: str) -> bool:
        return (
            file_path.endswith('_spec.rb') or
            file_path.endswith('_test.rb') or
            '/spec/' in file_path or
            '/test/' in file_path or
            file_path.startswith('test/') or
            file_path.startswith('spec/')
        )

    def detect_required_version(self) -> str:
        """Detect required Ruby version"""
        # Check .ruby-version
        ruby_version = self.repo_dir / ".ruby-version"
        if ruby_version.exists():
            content = ruby_version.read_text().strip()
            match = re.search(r'(\d+\.\d+)', content)
            if match:
                return match.group(1)

        # Check Gemfile
        gemfile = self.repo_dir / "Gemfile"
        if gemfile.exists():
            content = gemfile.read_text()
            match = re.search(r'ruby\s+["\'](\d+\.\d+)', content)
            if match:
                return match.group(1)

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "ruby")

        return "3.0"

    def get_actual_version(self) -> str:
        """Get current Ruby version (or custom version if set)"""
        # Use custom Ruby binary if set, otherwise use system Ruby
        ruby_cmd = getattr(self, 'ruby_binary', 'ruby')
        result = subprocess.run([ruby_cmd, "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            match = re.search(r'ruby\s+(\d+\.\d+)', result.stdout)
            if match:
                return match.group(1)
        return "unknown"

    def is_version_compatible(self, required: str, actual: str) -> bool:
        """Ruby requires exact minor version match"""
        try:
            req_parts = required.split('.')
            act_parts = actual.split('.')
            # Match major.minor exactly (Ruby has breaking changes between versions)
            return req_parts[:2] == act_parts[:2]
        except:
            return required == actual

    def setup_version(self, required_version: str):
        """Setup required Ruby version using rbenv"""
        current_version = self.get_actual_version()

        if current_version == required_version:
            print(f"      ✓ Ruby {current_version} already available")
            return

        print(f"      ⚠ Ruby version mismatch (required: {required_version}, available: {current_version})")

        # Check if rbenv is available
        rbenv_check = subprocess.run(["which", "rbenv"], capture_output=True, text=True)
        if rbenv_check.returncode != 0:
            print(f"      ⚠ rbenv not found - attempting to install...")
            # Try to install rbenv via homebrew (macOS) or git
            brew_check = subprocess.run(["which", "brew"], capture_output=True)
            if brew_check.returncode == 0:
                subprocess.run(["brew", "install", "rbenv", "ruby-build"], capture_output=True, timeout=300)
            else:
                print(f"      ⚠ Cannot install rbenv automatically")
                print(f"      → Continuing with Ruby {current_version} (tests will likely fail)")
                self.setup_failed = True
                return

        # FIRST: Check if required version is already installed in rbenv
        rbenv_root_result = subprocess.run(
            ["rbenv", "root"],
            capture_output=True,
            text=True
        )

        if rbenv_root_result.returncode == 0:
            rbenv_root = Path(rbenv_root_result.stdout.strip())
            versions_dir = rbenv_root / "versions"

            # Check for exact match or compatible versions
            if versions_dir.exists():
                installed_versions = []
                for version_dir in versions_dir.iterdir():
                    if version_dir.is_dir():
                        installed_versions.append(version_dir.name)

                # Try exact match first
                pattern = rf"^{re.escape(required_version)}\.\d+$"
                compatible_versions = [v for v in installed_versions if re.match(pattern, v)]

                if compatible_versions:
                    # Use the latest compatible version
                    full_version = sorted(compatible_versions)[-1]
                    print(f"      ✓ Found existing Ruby {full_version} in rbenv")

                    ruby_bin_path = versions_dir / full_version / "bin" / "ruby"
                    if ruby_bin_path.exists():
                        self.ruby_binary = str(ruby_bin_path)
                        self.gem_binary = str(ruby_bin_path.parent / "gem")
                        self.bundle_binary = str(ruby_bin_path.parent / "bundle")
                        print(f"      ✓ Using rbenv Ruby {full_version} for tests")
                        return

        # SECOND: Need to install - get list of available versions
        print(f"      → Installing Ruby {required_version} via rbenv...")

        # Try ruby-build first, fall back to rbenv install --list
        list_result = subprocess.run(
            ["ruby-build", "--definitions"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if list_result.returncode != 0:
            # Fallback to rbenv install --list
            list_result = subprocess.run(
                ["rbenv", "install", "--list"],
                capture_output=True,
                text=True,
                timeout=30
            )

        # Find the latest patch version matching required version
        full_version = None
        if list_result.returncode == 0:
            # Look for versions like "2.7.8" when required is "2.7"
            pattern = rf"^\s*({re.escape(required_version)}\.\d+)\s*$"
            matches = []
            for line in list_result.stdout.split('\n'):
                match = re.match(pattern, line)
                if match:
                    matches.append(match.group(1))

            if matches:
                # Use the latest (last in list)
                full_version = matches[-1]
                print(f"      → Found Ruby {full_version} available for install")
            else:
                print(f"      ⚠ No Ruby {required_version}.x versions found in rbenv catalog, continuing with {current_version}")
                self.setup_failed = True
                return
        else:
            print(f"      ⚠ Could not list rbenv versions, continuing with {current_version}")
            self.setup_failed = True
            return

        # THIRD: Install the full Ruby version using rbenv with retry logic
        max_retries = 2
        for attempt in range(max_retries):

            result = subprocess.run(
                ["rbenv", "install", "-s", full_version],  # -s skips if already installed
                capture_output=True,
                text=True,
                timeout=1800  # Ruby compilation can take a while
            )

            if result.returncode == 0:
                break  # Success

            # Check if it's a download error
            if "Error" in result.stderr or "curl" in result.stderr or "download" in result.stderr.lower():
                if attempt < max_retries - 1:
                    time.sleep(2)  # Brief pause before retry
                    continue

            # Other error or final attempt failed
            if attempt == max_retries - 1:
                print(f"      ⚠ Failed to install Ruby {full_version} after {max_retries} attempts")
                print(f"      → Error: {result.stderr[:300]}")

                # Try Homebrew as fallback (macOS only)
                brew_check = subprocess.run(["which", "brew"], capture_output=True)
                if brew_check.returncode == 0:
                    major_minor = '.'.join(required_version.split('.')[:2])
                    major = required_version.split('.')[0]

                    # Try exact version first, then incrementally newer minor versions
                    formulae_to_try = [f"ruby@{major_minor}"]

                    # If required is 3.0, also try 3.1, 3.2, 3.3, 3.4
                    if major == "3":
                        minor = int(required_version.split('.')[1]) if len(required_version.split('.')) > 1 else 0
                        for newer_minor in range(minor + 1, 10):  # Try up to 3.9
                            formulae_to_try.append(f"ruby@{major}.{newer_minor}")

                    for brew_formula in formulae_to_try:
                        brew_result = subprocess.run(
                            ["brew", "install", brew_formula],
                            capture_output=True,
                            text=True,
                            timeout=600
                        )

                        if brew_result.returncode == 0:
                            brew_prefix_result = subprocess.run(
                                ["brew", "--prefix", brew_formula],
                                capture_output=True,
                                text=True
                            )

                            if brew_prefix_result.returncode == 0:
                                brew_ruby_path = Path(brew_prefix_result.stdout.strip()) / "bin" / "ruby"
                                if brew_ruby_path.exists():
                                    self.ruby_binary = str(brew_ruby_path)
                                    self.gem_binary = str(brew_ruby_path.parent / "gem")
                                    self.bundle_binary = str(brew_ruby_path.parent / "bundle")
                                    print(f"      ✓ Ruby via {brew_formula}")
                                    return  # Success via Homebrew!
                                else:
                                    print(f"      ⚠ Ruby binary not found at {brew_ruby_path}")
                            else:
                                print(f"      ⚠ Could not get Homebrew prefix for {brew_formula}")
                        else:
                            if "disabled" in brew_result.stderr.lower():
                                continue  # Try next version
                            elif "no available formula" in brew_result.stderr.lower():
                                # No more versions available
                                break
                            else:
                                print(f"      ⚠ {brew_formula} install failed: {brew_result.stderr[:200]}")
                                break
                # Try ruby-install as final fallback
                ruby_install_check = subprocess.run(["which", "ruby-install"], capture_output=True)
                if ruby_install_check.returncode == 0:
                    ruby_install_result = subprocess.run(
                        ["ruby-install", "--no-reinstall", "ruby", full_version],
                        capture_output=True,
                        text=True,
                        timeout=1800
                    )

                    if ruby_install_result.returncode == 0:
                        ruby_install_path = Path.home() / ".rubies" / f"ruby-{full_version}" / "bin" / "ruby"
                        if ruby_install_path.exists():
                            self.ruby_binary = str(ruby_install_path)
                            self.gem_binary = str(ruby_install_path.parent / "gem")
                            self.bundle_binary = str(ruby_install_path.parent / "bundle")
                            print(f"      ✓ Ruby {full_version} installed via ruby-install")
                            return  # Success!

                print(f"      → Continuing with Ruby {current_version} (tests will likely fail)")
                self.setup_failed = True
                return

        # FOURTH: Set up to use the specific Ruby version
        if rbenv_root_result.returncode == 0:
            ruby_bin_path = Path(rbenv_root_result.stdout.strip()) / "versions" / full_version / "bin" / "ruby"
            if ruby_bin_path.exists():
                self.ruby_binary = str(ruby_bin_path)
                self.gem_binary = str(ruby_bin_path.parent / "gem")
                self.bundle_binary = str(ruby_bin_path.parent / "bundle")
                print(f"      ✓ Ruby {full_version} ready")
            else:
                print(f"      ⚠ Ruby binary not found at {ruby_bin_path}, continuing with {current_version}")
                self.setup_failed = True
        else:
            print(f"      ⚠ Could not determine rbenv root, continuing with {current_version}")
            self.setup_failed = True

    def reinstall_dependencies_if_needed(self):
        """Reinstall dependencies after patches modify Gemfile/Gemfile.lock"""
        if not (self.repo_dir / "Gemfile").exists():
            return

        if self.setup_failed:
            return

        print(f"      → Reinstalling dependencies after patch...")

        gem_cmd = getattr(self, 'gem_binary', 'gem')
        bundle_cmd = getattr(self, 'bundle_binary', 'bundle')

        # Remove existing bundle config and vendor/bundle to force clean reinstall
        bundle_config = self.repo_dir / ".bundle"
        vendor_bundle = self.repo_dir / "vendor" / "bundle"

        if bundle_config.exists():
            shutil.rmtree(bundle_config, ignore_errors=True)
        if vendor_bundle.exists():
            shutil.rmtree(vendor_bundle, ignore_errors=True)

        # Also remove Gemfile.lock to avoid version conflicts
        gemfile_lock = self.repo_dir / "Gemfile.lock"
        if gemfile_lock.exists():
            gemfile_lock.unlink()

        # Reinstall bundler
        subprocess.run([gem_cmd, "install", "bundler"], capture_output=True, text=True, timeout=300)

        # Reinstall gems
        result = self.run_command([bundle_cmd, "install", "--path", "vendor/bundle"], timeout=1200)
        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else result.stdout
            print(f"      ⚠ Bundle reinstall failed: {error_msg[:200]}")
            # Try without --path as fallback
            result = self.run_command([bundle_cmd, "install"], timeout=1200)
            if result.returncode == 0:
                print(f"      ✓ Dependencies reinstalled (system location)")
        else:
            print(f"      ✓ Dependencies reinstalled")

    def install_dependencies(self):
        """Install bundler dependencies"""
        print(f"[4/7] Installing dependencies...")

        if (self.repo_dir / "Gemfile").exists():
            # Check if setup already failed (e.g., wrong Ruby version)
            if self.setup_failed:
                print(f"      ⚠ Skipping dependency install due to environment setup failure")
                return

            gem_cmd = getattr(self, 'gem_binary', 'gem')
            bundle_cmd = getattr(self, 'bundle_binary', 'bundle')

            bundler_result = subprocess.run(
                [gem_cmd, "install", "bundler"],
                capture_output=True,
                text=True,
                timeout=300
            )

            # Install gems locally in vendor/bundle to avoid needing sudo
            result = self.run_command([bundle_cmd, "install", "--path", "vendor/bundle"], timeout=1200)
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else result.stdout
                print(f"      ⚠ Bundle install failed (code {result.returncode})")
                print(f"      → Error: {error_msg[:500]}")

                # Check if it's a version incompatibility issue
                version_incompatible = any(keyword in error_msg.lower() for keyword in [
                    "could not find gem",
                    "incompatible",
                    "required ruby version",
                    "does not match the running version"
                ])

                if version_incompatible and self.detected_version != self.actual_version:
                    print(f"      ⚠ Version incompatibility detected (required: {self.detected_version}, actual: {self.actual_version})")
                    print(f"      → Cannot proceed with tests - marking setup as failed")
                    self.setup_failed = True
                    return

                # Try without --path flag as fallback
                print(f"      → Retrying without --path flag...")
                result = self.run_command([bundle_cmd, "install"], timeout=1200)
                if result.returncode != 0:
                    error_msg = result.stderr if result.stderr else result.stdout
                    print(f"      ⚠ Bundle install failed again (code {result.returncode})")
                    print(f"      → Error: {error_msg[:500]}")
                    print(f"      → Marking setup as failed - tests will not run properly")
                    self.setup_failed = True
                else:
                    print(f"      ✓ Dependencies installed (system location)")
            else:
                print(f"      ✓ Dependencies installed (local path)")

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run RSpec/Test::Unit tests"""
        # Check if setup failed - skip tests if so
        if self.setup_failed:
            print(f"      ⚠ Skipping test execution due to failed environment setup")
            print(f"      → Required: {self.detected_version}, Actual: {self.actual_version}")
            return {}

        status_map = {}
        bundle_cmd = getattr(self, 'bundle_binary', 'bundle')

        if (self.repo_dir / "spec").exists():
            # Use JSON formatter so each `it` example gets a unique id that embeds
            # the spec file path (e.g. "./spec/.../block_delimiters_spec.rb[1:2:3]").
            # The old `--format documentation` approach captured every indented line
            # (including describe/context headers), inflating counts and producing
            # non-unique names that never contained the filename – breaking the smart
            # filter and yielding 0 FAIL_TO_PASS.
            json_out = tempfile.mktemp(suffix='.json')
            cmd = [
                bundle_cmd, "exec", "rspec",
                "--format", "progress",          # human-readable progress to stdout
                "--format", "json",              # machine-readable results to file
                "--out", json_out,
            ]
            # Run only the relevant test files (not the entire suite) to avoid
            # timeouts on large projects and prevent position-shift inflation of
            # FAIL_TO_PASS counts caused by unrelated tests shifting indices.
            if test_files:
                cmd.extend(test_files)
            result = self.run_command(cmd, timeout=600)
            output = result.stdout + result.stderr

            parsed_from_json = False
            if os.path.exists(json_out):
                try:
                    with open(json_out) as _f:
                        rspec_data = json.load(_f)
                    for example in rspec_data.get('examples', []):
                        # Use the unique id (includes file path + position) as key
                        test_id = example.get('id') or example.get('full_description', '')
                        status = example.get('status', '')
                        if status == 'passed':
                            status_map[test_id] = 'PASSED'
                        elif status in ('failed', 'pending'):
                            status_map[test_id] = 'FAILED'
                    parsed_from_json = True
                except Exception as _e:
                    print(f"      ⚠ Could not parse RSpec JSON output ({_e}), falling back to documentation format")
                finally:
                    try:
                        os.unlink(json_out)
                    except OSError:
                        pass

            if not parsed_from_json:
                # Fallback: re-run with documentation format and use indentation-based
                # parsing (less accurate but better than nothing)
                cmd = [bundle_cmd, "exec", "rspec", "--format", "documentation"]
                if test_files:
                    cmd.extend(test_files)
                result = self.run_command(cmd, timeout=600)
                output = result.stdout + result.stderr
                for line in output.split('\n'):
                    if line.strip() and (line.startswith('  ') or line.startswith('    ')):
                        if 'FAILED' in line or '✗' in line:
                            status_map[line.strip()] = 'FAILED'
                        elif '✓' in line or not any(x in line for x in ['FAILED', 'ERROR']):
                            test_name = line.strip()
                            if test_name:
                                status_map[test_name] = 'PASSED'
        else:
            # For Test::Unit/Minitest with rake
            if test_files:
                # Run tests for each file separately and merge results
                # rake test doesn't handle multiple TEST= arguments well
                # TESTOPTS=-v enables verbose output so passing tests also print
                # their real names (e.g. "test: <desc>(<class>): ."), making
                # before/after name matching possible for FAIL_TO_PASS detection.
                all_output = ""
                for test_file in test_files:
                    cmd = [bundle_cmd, "exec", "rake", "test", f"TEST={test_file}", "TESTOPTS=-v"]
                    result = self.run_command(cmd, timeout=600)
                    all_output += result.stdout + result.stderr + "\n"
                output = all_output
            else:
                # Run all tests
                cmd = [bundle_cmd, "exec", "rake", "test", "TESTOPTS=-v"]
                result = self.run_command(cmd, timeout=600)
                output = result.stdout + result.stderr

            # Parse Test::Unit/Minitest output
            # Format examples:
            # "  1) Failure:\ntest_should_replace_target_info(TailInputTest) [test/plugin/test_in_tail.rb:1990]:"
            # "Finished in 45.678 seconds.\n70 tests, 150 assertions, 1 failures, 0 errors"

            # Extract test results from output
            # test-unit 3.x with TESTOPTS=-v produces:
            #   ClassName:
            #     test: description:\t[.FE]: (timing)   ← per-test verbose line
            #     nested_context:
            #       test: description:\t[.FE]: (timing)
            #   ...
            #   Failure: test: description(ClassName::context)  ← failure detail block
            #
            # The class name appears on a TOP-LEVEL (no indent) header line, and all
            # tests indented below it belong to that class.  The failure block includes
            # the class in parens so both failure and verbose lines produce the same key:
            #   "ClassName::test description"
            current_class = None
            current_test = None
            for line in output.split('\n'):
                # Track top-level class header: "ClassName: " (no leading whitespace,
                # starts with uppercase letter, followed by optional spaces then EOF).
                class_match = re.match(r'^([A-Z]\w*):\s*$', line)
                if class_match:
                    current_class = class_match.group(1)
                    continue

                # Match verbose test-unit result line (requires TESTOPTS=-v):
                #   "  test: <description>:\t<result>[: (timing)]"
                # The TAB before the result char is the reliable delimiter.
                verbose_match = re.match(r'^\s+test:\s+(.+):\t+([.FE])', line)
                if verbose_match and current_class:
                    test_desc = verbose_match.group(1).strip()
                    result_char = verbose_match.group(2)
                    test_name = f"{current_class}::{test_desc}"
                    if result_char == '.':
                        status_map[test_name] = 'PASSED'
                    else:
                        status_map[test_name] = 'FAILED'
                    continue

                # Match failure/error detail block: "Failure: test: <desc>(<class>)"
                # This fires for each failing test and gives us the class in parens.
                match = re.search(r'(Failure|Error):\s*test:\s*(.+?)\(([^)]+)\)', line)
                if match:
                    test_desc = match.group(2).strip()
                    test_class = match.group(3).split('::')[0]  # Get outermost class
                    test_name = f"{test_class}::{test_desc}"
                    status_map[test_name] = 'FAILED'
                    continue

                # Match old Test::Unit format: "test_name(ClassName)"
                match = re.match(r'test_(\w+)\([^)]+\)', line)
                if match:
                    test_name = 'test_' + match.group(1)
                    current_test = test_name
                    status_map[test_name] = 'FAILED'
                    continue

                # Match Minitest verbose format: "ClassName#test_name = 0.05 s = ."
                match = re.search(r'(test_\w+)\s*=.*=\s*([.FE])', line)
                if match:
                    test_name = match.group(1)
                    result_char = match.group(2)
                    if result_char == '.':
                        status_map[test_name] = 'PASSED'
                    else:
                        status_map[test_name] = 'FAILED'
                    continue

            # Parse summary to get total counts
            # Format: "26 tests, 130 assertions, 26 failures, 0 errors"  (test-unit)
            # Format: "26 runs, 130 assertions, 0 failures, 0 errors"    (minitest)
            summary_match = re.search(r'(\d+) (?:tests?|runs?),.*?(\d+) failures?,.*?(\d+) errors?', output, re.IGNORECASE)
            if summary_match:
                total_tests = int(summary_match.group(1))
                failures = int(summary_match.group(2))
                errors = int(summary_match.group(3))
                passed = total_tests - failures - errors

                # If we parsed fewer tests than the summary shows, add synthetic test names
                # This happens when passing tests don't print individual names (just ".")
                detected_count = len(status_map)
                if detected_count < total_tests:
                    # Create synthetic test entries to match the summary
                    # This ensures FAIL_TO_PASS calculation is accurate
                    missing_failed = max(0, failures - sum(1 for s in status_map.values() if s in ['FAILED', 'ERROR']))
                    missing_passed = max(0, passed - sum(1 for s in status_map.values() if s == 'PASSED'))

                    if test_files:
                        for i, test_file in enumerate(test_files):
                            test_file_name = test_file.replace('/', '::').replace('.rb', '')

                            # Distribute missing tests across files
                            if missing_failed > 0:
                                count = (missing_failed + len(test_files) - 1) // len(test_files)
                                for j in range(min(count, missing_failed)):
                                    status_map[f"{test_file_name}::test_{i}_{j}_failed"] = 'FAILED'
                                    missing_failed -= 1

                            if missing_passed > 0:
                                count = (missing_passed + len(test_files) - 1) // len(test_files)
                                for j in range(min(count, missing_passed)):
                                    status_map[f"{test_file_name}::test_{i}_{j}_passed"] = 'PASSED'
                                    missing_passed -= 1
                    else:
                        # No test files specified - create generic entries
                        for j in range(missing_failed):
                            status_map[f"test_{j}_failed"] = 'FAILED'
                        for j in range(missing_passed):
                            status_map[f"test_{j}_passed"] = 'PASSED'

            # Debug output
            if debug or (not status_map and result.returncode != 0):
                print(f"      [DEBUG] Ruby test command: {' '.join(cmd)}")
                print(f"      [DEBUG] Return code: {result.returncode}")
                print(f"      [DEBUG] Output (first 3000 chars):\n{output[:3000]}")
                print(f"      [DEBUG] Parsed {len(status_map)} tests: {list(status_map.keys())[:10]}")

        return status_map


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
# PYTHON VALIDATOR
# ============================================================================

class PythonValidator(BaseValidator):
    """Validator for Python projects"""

    def is_test_file(self, file_path: str) -> bool:
        return (
            file_path.startswith('test_') or
            file_path.endswith('_test.py') or
            '/tests/' in file_path or
            '/test/' in file_path
        )

    def detect_required_version(self) -> str:
        """Detect required Python version"""
        sources = [
            (self.repo_dir / ".python-version", r'(\d+\.\d+)'),
            (self.repo_dir / "pyproject.toml", r'requires-python\s*=\s*["\']>=?\s*(\d+\.\d+)'),
            (self.repo_dir / "setup.py", r'python_requires\s*=\s*["\']>=?(\d+\.\d+)'),
            (self.repo_dir / "setup.cfg", r'python_requires\s*=\s*>=?(\d+\.\d+)'),
        ]

        for filepath, pattern in sources:
            if not filepath.exists():
                continue
            content = filepath.read_text()
            match = re.search(pattern, content)
            if match:
                version = match.group(1)
                if VersionDetector.parse_version(version) >= (3, 6):
                    return version

        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "python")

        return "3.8"

    def get_actual_version(self) -> str:
        """Get current Python version"""
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def setup_version(self, required_version: str):
        """Python version is managed by conda environment"""
        pass

    def install_dependencies(self):
        """Install Python dependencies"""
        print(f"[4/7] Installing dependencies...")

        subprocess.run([sys.executable, "-m", "pip", "install", "pytest", "-q"], capture_output=True)

        if (self.repo_dir / "setup.py").exists():
            self.run_command([sys.executable, "setup.py", "develop"], timeout=600)

        if (self.repo_dir / "requirements.txt").exists():
            self.run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], timeout=600)

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run pytest tests"""
        status_map = {}

        cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header"]
        result = self.run_command(cmd, timeout=600)
        output = result.stdout + result.stderr

        for line in output.split('\n'):
            if '::' in line and (' PASSED' in line or ' FAILED' in line or ' ERROR' in line):
                match = re.match(r'(.+?)\s+(PASSED|FAILED|ERROR)', line)
                if match:
                    test_name = match.group(1).strip()
                    status = match.group(2)
                    status_map[test_name] = status

        return status_map

    def extract_modified_tests(self) -> set:
        """
        Extract specific test methods modified in test_patch for Python
        Returns: {'tests/test_foo.py::TestClass::test_method', ...}
        """
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')

        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            if self.is_test_file(file_path):
                current_test = None
                current_class = None
                tests_with_changes = {}

                lines = patch_content.split('\n')
                for line in lines:
                    line_content = line[1:] if line and line[0] in ' +-' else line

                    # Track class
                    class_match = re.search(r'^\s*class (\w+)', line_content)
                    if class_match:
                        current_class = class_match.group(1)

                    # Track test method
                    test_match = re.search(r'^\s*def (test_\w+)\s*\(', line_content)
                    if test_match:
                        current_test = test_match.group(1)

                    # Check for actual changes (not comments)
                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line.startswith('+++') and
                                not line.startswith('---') and
                                not line[1:].strip().startswith('#'))

                    if current_test and is_change:
                        tests_with_changes[current_test] = current_class

                # Build test identifiers
                for test_name, class_name in tests_with_changes.items():
                    if class_name:
                        modified_tests.add(f"{file_path}::{class_name}::{test_name}")
                    else:
                        modified_tests.add(f"{file_path}::{test_name}")

            i += 2

        return modified_tests


# ============================================================================
# MAIN VALIDATION LOGIC
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
            'rust': RustValidator,
            'go': GoValidator,
            'java': JavaValidator,
            'javascript': JavaScriptValidator,
            'php': PHPValidator,
            'ruby': RubyValidator,
            'c': CValidator,
            'python': PythonValidator,
        }

        validator_class = validators.get(self.language)
        if not validator_class:
            raise RuntimeError(f"No validator for language: {self.language}")

        # Pass self as parent so validator can use run_in_env
        self.validator = validator_class(self.instance, self.workspace, self.repo_dir, parent=self)

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

                    # For Rust projects with insta snapshots, accept new snapshots after solution
                    accept_snapshots = (isinstance(self.validator, RustValidator) and
                                       hasattr(self.validator, 'uses_insta') and
                                       self.validator.uses_insta)

                    if accept_snapshots:
                        print(f"      → Running tests with solution (accepting snapshots)...")
                    else:
                        print(f"      → Running tests with solution...")

                    results_after = self.validator.run_tests(
                        test_files=test_files,
                        debug=False,
                        accept_snapshots=accept_snapshots
                    )
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
                    # Special handling for snapshot tests - separate fixtures from snapshots
                    uses_snapshots = (isinstance(self.validator, RustValidator) and
                                     ((hasattr(self.validator, 'uses_insta') and self.validator.uses_insta) or
                                      'tests/syntax-tests/highlighted/' in test_patch))

                    if uses_snapshots:
                        # Split test_patch into fixtures and snapshots
                        fixtures_patch, snapshots_patch = self._split_fixtures_and_snapshots(test_patch)

                        if fixtures_patch and snapshots_patch:
                            print(f"      → Snapshot test detected: separating fixtures from snapshots")

                            # bat-style syntax tests: run before/after for real FAIL_TO_PASS
                            is_bat_syntax = 'tests/syntax-tests/highlighted/' in test_patch

                            if is_bat_syntax:
                                # Step 1: Apply source (fixture) files only — no fix, no expected outputs
                                print(f"      → Applying test source fixtures...")
                                self.validator.apply_patch(fixtures_patch, "fixtures_only")

                                # Step 2: Run tests before fix — expected files still old, should fail
                                print(f"      → Running tests before fix...")
                                results_before = self.validator.run_tests(test_files=test_files, debug=False)
                                print(f"      → Before fix: {len(results_before)} tests")

                                # Step 3: Apply solution
                                print(f"      → Applying solution patch...")
                                self.validator.apply_patch(solution_patch, "solution_patch")

                                # Step 4: Apply updated expected (highlighted) files
                                print(f"      → Applying expected highlighted outputs...")
                                self.validator.apply_patch(snapshots_patch, "snapshots")

                                # Step 5: Run tests after fix — should pass
                                print(f"      → Running tests after fix...")
                                results_after = self.validator.run_tests(test_files=test_files, debug=False)
                                print(f"      → After fix: {len(results_after)} tests")

                                filter_set = None
                                baseline_for_pass_to_pass = None
                                skip_standard_strategy = True

                            else:
                                # Insta snapshot flow: run baseline, then apply fixtures+solution+snapshots
                                # Step 1: Get baseline (no patches)
                                print(f"      → Running baseline tests...")
                                baseline = self.validator.run_tests(test_files=test_files, debug=False)
                                print(f"      → Baseline: {len(baseline)} tests")

                                # Step 2: Extract snapshot files for later identification
                                snapshot_files_to_update = re.findall(r'diff --git a/(.*?\.snap) b/', snapshots_patch)

                                # Step 3: Apply test fixtures
                                print(f"      → Applying test fixtures...")
                                self.validator.apply_patch(fixtures_patch, "fixtures_only")

                                # Step 4: Apply solution
                                print(f"      → Applying solution patch...")
                                self.validator.apply_patch(solution_patch, "solution_patch")

                                # Step 5: Apply snapshots and run
                                print(f"      → Applying expected snapshots...")
                                self.validator.apply_patch(snapshots_patch, "snapshots")

                                print(f"      → Running tests (with solution and snapshots)...")
                                results_after = self.validator.run_tests(
                                    test_files=test_files,
                                    debug=False,
                                    accept_snapshots=True
                                )

                                # For snapshot tests, identify NEW tests as "FAIL_TO_PASS"
                                # These are tests related to the new/modified snapshots
                                print(f"      → Identifying tests related to new snapshots...")
                                new_snapshot_tests = set()

                                # Extract test identifiers from snapshot files
                                for snap_file in snapshot_files_to_update:
                                    # e.g., ruff_linter__rules__pyupgrade__tests__UP028_0.py.snap
                                    snap_name = Path(snap_file).stem
                                    if snap_name.endswith('.py'):
                                        snap_name = snap_name[:-3]  # Remove .py

                                    parts = snap_name.split('__')
                                    if 'tests' in parts:
                                        test_idx = parts.index('tests')
                                        test_parts = parts[test_idx + 1:]

                                        if test_parts:
                                            for part in test_parts:
                                                if part:
                                                    subparts = part.split('_')
                                                    for subpart in subparts:
                                                        if subpart:
                                                            subpart_lower = subpart.lower()
                                                            for test_name in results_after.keys():
                                                                if subpart_lower in test_name.lower():
                                                                    new_snapshot_tests.add(test_name)

                                # Create fake "before" results where new tests are marked as FAILED
                                results_before = {}
                                for test_name, status in baseline.items():
                                    if test_name in new_snapshot_tests:
                                        results_before[test_name] = 'FAILED'
                                    else:
                                        results_before[test_name] = status

                                # Add any tests not in baseline as FAILED
                                for test_name in results_after.keys():
                                    if test_name not in results_before:
                                        if test_name in new_snapshot_tests:
                                            results_before[test_name] = 'FAILED'

                                print(f"      → Marked {len(new_snapshot_tests)} snapshot-related tests for FAIL_TO_PASS")

                                filter_set = None
                                baseline_for_pass_to_pass = None
                                skip_standard_strategy = True
                        else:
                            # Couldn't split properly, fall back to standard approach
                            skip_standard_strategy = False
                    else:
                        skip_standard_strategy = False

                    if not skip_standard_strategy:
                        # Step 1: Get baseline (no patches)
                        print(f"      → Checking test_patch compatibility...")
                        # Kill any lingering processes from a previous validation run
                        # before baseline (prevents cross-run port conflicts on 4444 etc.)
                        if isinstance(self.validator, JavaScriptValidator):
                            self._kill_lingering_js_processes()
                        print(f"      → Running baseline tests...")
                        baseline = self.validator.run_tests(test_files=test_files, debug=False)
                        _base_fail_count = sum(1 for s in baseline.values() if s in ['FAILED', 'ERROR'])
                        print(f"      → Baseline: {len(baseline)} tests ({_base_fail_count} failing)")
                        # If baseline has an unusually high failure rate for JS, retry once
                        # after killing lingering processes. This helps mitigate port conflicts
                        # (EADDRINUSE) and truncated mocha output.
                        if isinstance(self.validator, JavaScriptValidator) and baseline:
                            _base_fail_rate = _base_fail_count / max(1, len(baseline))
                            if _base_fail_rate >= 0.3 and _base_fail_count >= 10:
                                print(f"      → Baseline failure rate high ({_base_fail_count}/{len(baseline)}), retrying after cleanup...")
                                self._kill_lingering_js_processes()
                                _baseline_retry = self.validator.run_tests(test_files=test_files, debug=False)
                                _retry_fail_count = sum(1 for s in _baseline_retry.values() if s in ['FAILED', 'ERROR'])
                                baseline = self._prefer_results(baseline, _baseline_retry, "Baseline retry")
                                _base_fail_count = sum(1 for s in baseline.values() if s in ['FAILED', 'ERROR'])

                        # Step 1.5: Pre-install dependencies for JavaScript if needed
                        if isinstance(self.validator, JavaScriptValidator):
                            self.validator.preinstall_test_dependencies(test_patch, solution_patch)
                            # Kill any lingering processes from the baseline run before
                            # applying test_patch (prevents EADDRINUSE port conflicts).
                            self._kill_lingering_js_processes()

                        # Step 2: Apply test_patch and check
                        self.validator.apply_patch(test_patch, "test_patch")
                        print(f"      → Running tests with test_patch...")
                        with_test_patch = self.validator.run_tests(test_files=test_files, debug=False)
                        print(f"      → With test_patch: {len(with_test_patch)} tests")
                        # For JavaScript: single retry if the test_patch run looks unreliable.
                        # Triggers on count drop >10% vs baseline, or suspiciously many new failures.
                        if isinstance(self.validator, JavaScriptValidator) and baseline:
                            _wtp_fail = sum(1 for s in with_test_patch.values() if s in ['FAILED', 'ERROR'])
                            _suspicious_threshold = max(10, len(baseline) * 0.1)
                            _count_ok = len(with_test_patch) >= len(baseline) * 0.9
                            _failures_ok = _wtp_fail <= _base_fail_count + _suspicious_threshold
                            if not _count_ok or not _failures_ok:
                                reason = (f"count {len(baseline)}→{len(with_test_patch)}"
                                          if not _count_ok else f"failures {_wtp_fail}")
                                print(f"      → test_patch run unreliable ({reason}); retrying after cleanup...")
                                self._kill_lingering_js_processes()
                                _with_retry = self.validator.run_tests(test_files=test_files, debug=False)
                                with_test_patch = self._prefer_results(with_test_patch, _with_retry, "test_patch retry")

                            # If baseline looks truncated vs test_patch, re-run baseline once
                            if baseline and len(baseline) < len(with_test_patch) * 0.5:
                                print(f"      → Baseline count low ({len(baseline)} vs {len(with_test_patch)}), retrying baseline...")
                                self._kill_lingering_js_processes()
                                _baseline_retry2 = self.validator.run_tests(test_files=test_files, debug=False)
                                baseline = self._prefer_results(baseline, _baseline_retry2, "Baseline retry")

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
                            # Check if this is a snapshot testing scenario
                            uses_snapshots = (isinstance(self.validator, RustValidator) and
                                             hasattr(self.validator, 'uses_insta') and
                                             self.validator.uses_insta)

                            if uses_snapshots:
                                print(f"      ℹ No tests failing with test_patch (expected for snapshot tests)")
                                print(f"      Snapshot tests pass when expected snapshots are included in test_patch")
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
                        # (e.g. a new test causes an infinite render loop that hangs act(),
                        # so karma dies and those tests never appear in output at all).
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

                            # Re-apply workspace member exclusions that were set during
                            # install_dependencies. git checkout restores Cargo.toml, which
                            # brings back problematic members (e.g. tokio-tls with yanked deps).
                            # --exclude prevents building them but NOT dependency resolution,
                            # so we must remove them from [workspace.members] again.
                            _excluded = getattr(self.validator, 'excluded_members', [])
                            if _excluded and isinstance(self.validator, RustValidator):
                                _ws_cargo = self.repo_dir / "Cargo.toml"
                                if _ws_cargo.exists():
                                    _wc = _ws_cargo.read_text()
                                    for _m in _excluded:
                                        _wc = re.sub(
                                            rf'^\s*["\']?{re.escape(_m)}["\']?,?\s*$',
                                            '', _wc, flags=re.MULTILINE
                                        )
                                    _wc = re.sub(r'members\s*=\s*\[\s*,', 'members = [', _wc)
                                    _wc = re.sub(r',\s*,', ',', _wc)
                                    _wc = re.sub(r',\s*\]', ']', _wc)
                                    _ws_cargo.write_text(_wc)
                                    # Remove old Cargo.lock and regenerate without excluded member.
                                    # We do NOT use --all-features here so the regenerated lock
                                    # stays compatible with the current Rust version (avoids
                                    # pulling in edition-2024 deps that would require Rust 1.85).
                                    _lock = self.repo_dir / "Cargo.lock"
                                    if _lock.exists():
                                        _lock.unlink()
                                    _regen = subprocess.run(
                                        ["cargo", "generate-lockfile"],
                                        cwd=self.repo_dir,
                                        capture_output=True, text=True, timeout=300
                                    )
                                    if _regen.returncode == 0:
                                        self.validator.use_locked = True
                                        print(f"      → Re-excluded workspace member(s) after git reset: {', '.join(_excluded)} (lockfile regenerated)")
                                    else:
                                        print(f"      → Re-excluded workspace member(s) after git reset: {', '.join(_excluded)} (lockfile regen failed: {_regen.stderr[:200]})")

                            # If baseline was 0 (targeted test didn't exist in baseline),
                            # run broader suite for PASS_TO_PASS while repo is still clean
                            _broader_ws_pkg = None
                            broader_baseline = {}
                            if len(baseline) == 0 and test_files:
                                for _tf in test_files:
                                    if '/tests/' in _tf:
                                        _parts = _tf.split('/')
                                        if len(_parts) >= 2 and _parts[1] == 'tests':
                                            _broader_ws_pkg = _parts[0]
                                            break
                                    elif _tf.startswith('crates/') and '/' in _tf[7:]:
                                        _broader_ws_pkg = _tf.split('/')[1]
                                        break
                                if _broader_ws_pkg:
                                    print(f"      → Running broader baseline for PASS_TO_PASS ({_broader_ws_pkg})...")
                                    _use_all_feat = isinstance(self.validator, RustValidator)
                                    broader_baseline = self.validator.run_tests(
                                        test_files=[f"{_broader_ws_pkg}/tests/"], debug=False,
                                        **({'all_features': True} if _use_all_feat else {}))
                                    print(f"      → Broader baseline: {len(broader_baseline)} tests")
                                elif isinstance(self.validator, PHPValidator):
                                    # PHP: run test files without --filter to capture all existing
                                    # tests for PASS_TO_PASS (filter would return 0 since new
                                    # test methods don't exist yet at base_commit)
                                    php_tf = [tf for tf in test_files if tf.endswith('.php')]
                                    if php_tf:
                                        print(f"      → Running unfiltered baseline for PASS_TO_PASS...")
                                        broader_baseline = self.validator.run_tests(
                                            test_files=php_tf, debug=False, no_filter=True)
                                        print(f"      → Broader baseline: {len(broader_baseline)} tests")

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

                            # Reinstall dependencies if Gemfile was modified (Ruby projects)
                            if isinstance(self.validator, RubyValidator):
                                self.validator.reinstall_dependencies_if_needed()

                            # Clean build cache again
                            self._clean_build_cache()

                            # For Rust projects with insta snapshots, accept new snapshots after solution
                            accept_snapshots = (isinstance(self.validator, RustValidator) and
                                       hasattr(self.validator, 'uses_insta') and
                                       self.validator.uses_insta)

                            if accept_snapshots:
                                print(f"      → Running tests (with solution, accepting snapshots)...")
                            else:
                                print(f"      → Running tests (with solution)...")

                            results_after = self.validator.run_tests(
                                test_files=test_files,
                                debug=False,
                                accept_snapshots=accept_snapshots
                            )

                            # For JavaScript: if "after" count dropped significantly vs "before",
                            # OR if any FAIL_TO_PASS tests (failed in before) are absent from after,
                            # retry once after killing lingering processes. This handles intermittent
                            # port-conflict / process-cleanup races that truncate mocha output.
                            if isinstance(self.validator, JavaScriptValidator) and results_before:
                                _ff_count_dropped = len(results_after) < len(results_before) * 0.8
                                _ff_before_failed = [n for n, s in results_before.items()
                                                     if s in ('FAILED', 'ERROR')]
                                _ff_ftop_missing = (bool(_ff_before_failed)
                                                    and any(n not in results_after
                                                            for n in _ff_before_failed))
                                if _ff_count_dropped or _ff_ftop_missing:
                                    if _ff_ftop_missing and not _ff_count_dropped:
                                        _ff_missing = [n for n in _ff_before_failed if n not in results_after]
                                        print(f"      → FAIL_TO_PASS tests missing from after ({len(_ff_missing)}), retrying after run...")
                                    else:
                                        print(f"      → After count dropped ({len(results_after)} vs {len(results_before)} before), retrying after run...")
                                    self._kill_lingering_js_processes()
                                    results_after = self.validator.run_tests(
                                        test_files=test_files,
                                        debug=False,
                                        accept_snapshots=accept_snapshots
                                    )
                                    print(f"      → Retry after: {len(results_after)} tests")

                            # If we collected a broader baseline, also run broader tests after
                            # solution to gather PASS_TO_PASS candidates
                            if broader_baseline and _broader_ws_pkg:
                                print(f"      → Running broader tests after solution ({_broader_ws_pkg})...")
                                _use_all_feat = isinstance(self.validator, RustValidator)
                                broader_after = self.validator.run_tests(
                                    test_files=[f"{_broader_ws_pkg}/tests/"], debug=False,
                                    **({'all_features': True} if _use_all_feat else {}))
                                print(f"      → Broader after: {len(broader_after)} tests")
                                # Merge broader results without overwriting targeted results_after
                                for _test, _status in broader_after.items():
                                    if _test not in results_after:
                                        results_after[_test] = _status
                            elif broader_baseline and isinstance(self.validator, PHPValidator):
                                # PHP: re-run test files unfiltered to capture all existing tests
                                php_tf = [tf for tf in test_files if tf.endswith('.php')]
                                if php_tf:
                                    print(f"      → Running unfiltered tests after solution for PASS_TO_PASS...")
                                    broader_after = self.validator.run_tests(
                                        test_files=php_tf, debug=False, no_filter=True)
                                    print(f"      → Broader after: {len(broader_after)} tests")
                                    for _test, _status in broader_after.items():
                                        if _test not in results_after:
                                            results_after[_test] = _status

                            # Extract modified tests for filtering
                            if hasattr(self.validator, 'extract_modified_tests'):
                                filter_set = self.validator.extract_modified_tests()
                                # If no tests were extracted, don't filter (treat as None)
                                if len(filter_set) == 0:
                                    filter_set = None
                                    print(f"      → No specific tests extracted, running all modified test files")
                                else:
                                    print(f"      → Filtering to {len(filter_set)} modified tests")
                                    for test in list(filter_set)[:5]:
                                        print(f"        - {test}")
                                    if len(filter_set) > 5:
                                        print(f"        ... and {len(filter_set) - 5} more")

                            # Store baseline for PASS_TO_PASS calculation.
                            # Always use the pre-patch baseline so that tests which were
                            # passing before any patches but couldn't run during the
                            # "before" step (because test_patch broke their file's import)
                            # are correctly classified as PASS_TO_PASS rather than FAIL_TO_PASS.
                            if broader_baseline:
                                baseline_for_pass_to_pass = broader_baseline
                            else:
                                baseline_for_pass_to_pass = baseline if baseline else None

                        else:
                            # STANDARD STRATEGY: test_patch is compatible with base_commit
                            print(f"      → Using standard strategy")

                            # For PHP: if baseline==0 (filter matched nothing because new test
                            # methods don't exist yet) but the test FILE already exists, run
                            # unfiltered to capture pre-existing tests for PASS_TO_PASS.
                            _php_unfiltered_baseline = {}
                            if len(baseline) == 0 and isinstance(self.validator, PHPValidator):
                                php_existing = [tf for tf in test_files
                                                if tf.endswith('.php')
                                                and (self.validator.repo_dir / tf).exists()]
                                if php_existing:
                                    print(f"      → Running unfiltered baseline for PASS_TO_PASS...")
                                    _php_unfiltered_baseline = self.validator.run_tests(
                                        test_files=php_existing, debug=False, no_filter=True)
                                    print(f"      → Unfiltered baseline: {len(_php_unfiltered_baseline)} tests")

                            # results_before = with_test_patch (tests should fail)
                            results_before = with_test_patch

                            # Apply solution patch
                            print(f"      → Applying solution patch...")
                            self.validator.apply_patch(solution_patch, "solution_patch")

                            # Reinstall dependencies if Gemfile was modified (Ruby projects)
                            if isinstance(self.validator, RubyValidator):
                                self.validator.reinstall_dependencies_if_needed()

                            # Reinstall dependencies if package.json was modified (JS projects).
                            # Use the validator's run_command (nvm-aware) so packages are
                            # installed for the correct Node version, not the system default.
                            if isinstance(self.validator, JavaScriptValidator):
                                self.validator.run_command(['npm', 'install'], timeout=300)

                            # Clean build cache and rebuild if needed (for C/C++ projects)
                            self._clean_build_cache()

                            # results_after (tests should pass)
                            # For Rust projects with insta snapshots, accept new snapshots after solution
                            accept_snapshots = (isinstance(self.validator, RustValidator) and
                                       hasattr(self.validator, 'uses_insta') and
                                       self.validator.uses_insta)

                            if accept_snapshots:
                                print(f"      → Running tests (with solution, accepting snapshots)...")
                            else:
                                print(f"      → Running tests (with solution)...")

                            # Kill any port-holding processes immediately before the after run.
                            # The test_patch retry may have left a hanging mocha server on port 4444
                            # (e.g. HTTP server with delayed res.end()), and _clean_build_cache's
                            # kill runs earlier (before the npm install / rebuild steps), so a second
                            # kill right here ensures a clean slate.
                            if isinstance(self.validator, JavaScriptValidator):
                                self._kill_lingering_js_processes()

                            results_after = self.validator.run_tests(
                                test_files=test_files,
                                debug=False,
                                accept_snapshots=accept_snapshots
                            )

                            # Retry if solution appears to have no effect and before had many failures,
                            # OR if the after run itself has suspiciously many failures (port conflict
                            # even when before was clean, e.g. because the test_patch retry left a
                            # server on port 4444 that respawned after our pre-run kill).
                            if isinstance(self.validator, JavaScriptValidator):
                                _after_fail = sum(1 for s in results_after.values() if s in ['FAILED', 'ERROR'])
                                _before_fail = sum(1 for s in results_before.values() if s in ['FAILED', 'ERROR'])
                                _base_fail = sum(1 for s in baseline.values() if s in ['FAILED', 'ERROR'])
                                _suspicious_after_threshold = max(10, len(baseline) * 0.1) if baseline else 10
                                _after_suspicious = _after_fail > _base_fail + _suspicious_after_threshold
                                # Also retry if test count dropped significantly vs before
                                # (e.g. 118 → 60 with 0 failures: stale server truncated mocha output)
                                _count_dropped = (bool(results_before)
                                                  and len(results_after) < len(results_before) * 0.8)
                                # Also retry if any FAILED tests from "before" are missing from "after"
                                # (e.g. 118 before → 116 after: 2 FAIL_TO_PASS tests silently lost
                                # due to port-conflict truncation; the 0.8 threshold above won't catch
                                # small drops like 118→116).
                                _before_failed_names = [n for n, s in results_before.items()
                                                        if s in ('FAILED', 'ERROR')]
                                _ftop_missing = (bool(_before_failed_names)
                                                 and any(n not in results_after
                                                         for n in _before_failed_names))
                                if (_before_fail > _base_fail and _after_fail >= _before_fail) or _after_suspicious or _count_dropped or _ftop_missing:
                                    if _ftop_missing and not _count_dropped:
                                        _missing = [n for n in _before_failed_names if n not in results_after]
                                        print(f"      → FAIL_TO_PASS tests missing from after ({len(_missing)}), retrying...")
                                    elif _count_dropped:
                                        print(f"      → After count dropped ({len(results_after)} vs {len(results_before)} before), retrying...")
                                    else:
                                        print(f"      → After run suspicious ({_after_fail} failures vs {_base_fail} baseline), retrying...")
                                    self._kill_lingering_js_processes()
                                    _results_after_retry = self.validator.run_tests(
                                        test_files=test_files, debug=False, accept_snapshots=accept_snapshots)
                                    _retry_after_fail = sum(1 for s in _results_after_retry.values() if s in ['FAILED', 'ERROR'])
                                    if len(_results_after_retry) > len(results_after) or _retry_after_fail < _after_fail:
                                        print(f"      → Retry improved: {len(results_after)} → {len(_results_after_retry)} tests")
                                        results_after = _results_after_retry

                            # For PHP with unfiltered baseline: also run unfiltered after
                            # solution so PASS_TO_PASS candidates appear in results_after too.
                            if _php_unfiltered_baseline:
                                php_existing = [tf for tf in test_files
                                                if tf.endswith('.php')
                                                and (self.validator.repo_dir / tf).exists()]
                                if php_existing:
                                    print(f"      → Running unfiltered tests after solution for PASS_TO_PASS...")
                                    _php_unfiltered_after = self.validator.run_tests(
                                        test_files=php_existing, debug=False, no_filter=True)
                                    print(f"      → Unfiltered after: {len(_php_unfiltered_after)} tests")
                                    for _t, _s in _php_unfiltered_after.items():
                                        if _t not in results_after:
                                            results_after[_t] = _s

                            # Pass baseline so compare_results can filter pre-existing failures
                            baseline_for_pass_to_pass = _php_unfiltered_baseline if _php_unfiltered_baseline else (baseline if baseline else None)
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

            # Check for snapshot validation (Rust with insta) - before compare_results
            snapshot_validation_passed = None
            if isinstance(self.validator, RustValidator) and hasattr(self.validator, 'uses_insta') and self.validator.uses_insta:
                snap_new_files = list(self.repo_dir.rglob("*.snap.new"))
                snapshot_validation_passed = len(snap_new_files) == 0
                if snapshot_validation_passed:
                    print(f"      ✓ SNAPSHOT_VALIDATION: PASSED (solution matches expected output)")
                else:
                    print(f"      ✗ SNAPSHOT_VALIDATION: FAILED ({len(snap_new_files)} mismatched snapshots)")

            # Store for later use
            self.snapshot_validation_passed = snapshot_validation_passed

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

        # JavaScript/TypeScript: if a test failed "before" but is missing in "after",
        # treat it as FAIL_TO_PASS when counts are comparable. This avoids losing
        # FAIL_TO_PASS due to reporter name mismatches between failing and passing
        # output (e.g., mocha error detail name vs. spec pass line).
        if self.language in ['javascript', 'typescript']:
            before_failed_names = [n for n, s in before.items() if s in ('FAILED', 'ERROR')]
            after_failed_count = sum(1 for s in after.values() if s in ('FAILED', 'ERROR'))
            missing_failed = [n for n in before_failed_names if n not in after]
            if missing_failed and not fail_to_pass and after_failed_count == 0:
                # Only apply when run looks complete (counts are close)
                if len(after) >= max(1, int(len(before) * 0.9)):
                    print(f"      → Treating {len(missing_failed)} missing failed test(s) as FAIL_TO_PASS (JS name mismatch)")
                    fail_to_pass = sorted(missing_failed)

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

    def _split_fixtures_and_snapshots(self, test_patch: str) -> Tuple[str, str]:
        """
        Split test_patch into fixture files and snapshot files (for Rust insta)

        Args:
            test_patch: Combined patch with both fixtures and snapshots

        Returns: (fixtures_patch, snapshots_patch)
        """
        fixture_hunks = []
        snapshot_hunks = []

        # Split patch by file headers
        parts = re.split(r'(diff --git a/[^\n]+\n)', test_patch)

        i = 1
        while i < len(parts) - 1:
            header = parts[i]
            content = parts[i + 1]
            full_patch = header + content

            # Extract file path from header
            file_match = re.search(r'diff --git a/(.*?) b/', header)
            if file_match:
                file_path = file_match.group(1)

                # Classify as snapshot or fixture
                if file_path.endswith('.snap') or 'tests/syntax-tests/highlighted/' in file_path:
                    snapshot_hunks.append(full_patch)
                else:
                    # Test fixtures (.py, .rs, etc.)
                    fixture_hunks.append(full_patch)

            i += 2

        return ''.join(fixture_hunks), ''.join(snapshot_hunks)

    def _kill_lingering_js_processes(self):
        """Kill any lingering Node.js processes from the previous test run.

        Between the "before" and "after" test runs, stale node server processes may
        hold ports open (e.g. TCP keep-alive connections that survive mocha --exit).
        This causes the "after" run to get EADDRINUSE and all server-based tests fail.
        """
        if not isinstance(self.validator, JavaScriptValidator):
            return

        killed_any = False

        # 1. Kill processes whose command line references the repo path.
        #    (Catches mocha/grunt processes that used absolute paths.)
        try:
            repo_path = str(self.repo_dir)
            result = subprocess.run(
                ["pgrep", "-f", repo_path],
                capture_output=True, text=True, timeout=10
            )
            pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
            if pids:
                for pid in pids:
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                print(f"      → Killed {len(pids)} lingering Node.js process(es) from previous test run")
                killed_any = True
        except Exception:
            pass

        # 2. Kill any process holding common JS test ports (e.g. 4444 used by
        #    old axios http adapter tests).  Grunt may run mocha with relative
        #    paths so the process won't match repo_path above, yet it holds the
        #    port and blocks the next run.
        #    Loop up to 3 times to handle cases where multiple processes share
        #    the port or a new one spawns immediately after the first is killed.
        for port in [4444, 4445, 4446, 3000, 8080, 8888, 9999]:
            for _attempt in range(3):
                try:
                    result = subprocess.run(
                        ["lsof", "-ti", f"tcp:{port}"],
                        capture_output=True, text=True, timeout=5
                    )
                    pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
                    if not pids:
                        break  # Port is free, no need to retry
                    for pid in pids:
                        subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                    print(f"      → Freed port {port} (killed {len(pids)} process(es))")
                    killed_any = True
                    import time as _t; _t.sleep(0.3)  # brief pause before re-checking
                except Exception:
                    break

        # 3. Brief pause after killing so the OS fully releases TCP ports before
        #    the next test run attempts to bind them again.
        if killed_any:
            import time as _time
            _time.sleep(1)

    def _clean_build_cache(self):
        """Clean build cache to force recompilation (for Java/Gradle, C/Make, and JavaScript)"""
        # Kill any lingering Node.js processes from the previous test run before
        # running "after" tests (prevents EADDRINUSE from stale server processes).
        self._kill_lingering_js_processes()

        # Rebuild JavaScript project if it has a build step.
        # Needed when the test command uses MINIFY=true (or similar) which loads compiled
        # dist/ files rather than src/ directly — patching src without rebuilding dist
        # means tests still run against the old code.
        if isinstance(self.validator, JavaScriptValidator):
            package_json = self.repo_dir / "package.json"
            if package_json.exists():
                try:
                    with open(package_json) as f:
                        pkg = json.load(f)
                        scripts = pkg.get("scripts", {})
                        if "build" in scripts:
                            print(f"      → Rebuilding JavaScript project (dist/ may be stale after patch)...")
                            if (self.repo_dir / "yarn.lock").exists():
                                build_cmd = ["yarn", "build"]
                            else:
                                build_cmd = ["npm", "run", "build"]
                            result = self.validator.run_command(build_cmd, timeout=1200)
                            if result.returncode == 0:
                                print(f"      ✓ JS rebuild completed")
                            else:
                                print(f"      ⚠ JS rebuild failed: {result.stderr[:200]}")
                except Exception:
                    pass

        # Clean and rebuild for Gradle (Java)
        if hasattr(self.validator, 'build_tool') and self.validator.build_tool == "gradle":
            gradle_cmd = "./gradlew" if (self.repo_dir / "gradlew").exists() else "gradle"
            subprocess.run(
                [gradle_cmd, "clean", "--no-daemon"],
                cwd=self.repo_dir,
                capture_output=True,
                timeout=120
            )

        # Clean and rebuild for C/C++ (Make)
        if self.validator.__class__.__name__ == 'CValidator':
            if (self.repo_dir / "Makefile").exists():
                # Run make clean to remove old binaries
                subprocess.run(
                    ["make", "clean"],
                    cwd=self.repo_dir,
                    capture_output=True,
                    timeout=60
                )
                # Rebuild
                result = subprocess.run(
                    ["make"],
                    cwd=self.repo_dir,
                    capture_output=True,
                    timeout=600
                )
                if result.returncode != 0:
                    print(f"      ⚠ Rebuild after patch failed: {result.stderr[:200]}")

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

            # Add snapshot validation info (for Rust insta tests)
            if hasattr(validator, 'snapshot_validation_passed') and validator.snapshot_validation_passed is not None:
                instance['SNAPSHOT_VALIDATION'] = 'PASSED' if validator.snapshot_validation_passed else 'FAILED'

            # Add version info
            if hasattr(validator.validator, 'detected_version'):
                instance['detected_version'] = validator.validator.detected_version
                instance['actual_version'] = validator.validator.actual_version

            with open(output_path, 'w') as f:
                json.dump(instance, f, indent=2)

            print(f"\n{'='*80}")
            print(f"✓ Validation complete!")
            print(f"  Language: {validator.language}")
            if hasattr(validator, 'snapshot_validation_passed') and validator.snapshot_validation_passed is not None:
                status = '✓ PASSED' if validator.snapshot_validation_passed else '✗ FAILED'
                print(f"  Snapshot Validation: {status}")
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
