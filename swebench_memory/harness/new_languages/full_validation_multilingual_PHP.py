#!/usr/bin/env python3
"""
SWE-bench Multilingual Instance Validation Script - PHP Only

PHP-specific version of full_validation_multilingual.py.
Uses Docker for PHP version management and PHPUnit for testing.

Usage:
    python3 full_validation_multilingual_PHP.py instance.json [--output validated.json]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
import platform


# ============================================================================
# VERSION DETECTION UTILITIES
# ============================================================================

class VersionDetector:
    """Detect required PHP version from project files and dates"""

    @staticmethod
    def parse_version(version_str: str) -> Tuple[int, ...]:
        """Parse version string '8.1' -> (8, 1)"""
        try:
            return tuple(int(x) for x in version_str.split('.'))
        except (ValueError, AttributeError):
            return (0,)

    @staticmethod
    def get_version_from_date(created_at: str, language: str) -> str:
        """Determine appropriate PHP version based on creation date"""
        try:
            year = int(created_at.split('-')[0])
            month = int(created_at.split('-')[1])
        except (ValueError, IndexError):
            year = 2020  # Default fallback

        if language == "php":
            # PHP version timeline
            if year < 2019:
                return "7.2"
            elif year < 2021:
                return "7.4"
            elif year < 2023:
                return "8.0"
            else:
                return "8.1"

        return "latest"


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

class LanguageDetector:
    """Detect PHP from repository structure"""

    @staticmethod
    def detect(repo_dir: Path) -> str:
        """
        Detect language from repository files.
        Returns: 'php' or 'unknown'
        """
        # PHP - composer.json
        if (repo_dir / "composer.json").exists():
            return "php"

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
        """Run tests and return status map"""
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
        rebuild_commands = []

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
        via git apply, fetch them from the remote repo's default branch.

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


# ============================================================================
# MAIN VALIDATOR (PHP-only)
# ============================================================================

class MultilingualValidator:
    """Main validator that delegates to PHPValidator"""

    def __init__(self, instance: dict, workspace: Path, keep_env: bool = False):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.keep_env = keep_env
        self.language = None
        self.validator = None
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
            raise RuntimeError("Could not detect programming language (expected PHP/composer.json)")

    def run_in_env(self, command: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with language-specific environment variables"""
        try:
            # Merge environment variables from validator
            env = os.environ.copy()
            if hasattr(self.validator, 'env_vars'):
                env.update(self.validator.env_vars)

            return subprocess.run(
                command,
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
        """Create PHPValidator"""
        if self.language != 'php':
            raise RuntimeError(f"This script only supports PHP, but detected: {self.language}")

        # Pass self as parent so validator can use run_in_env
        self.validator = PHPValidator(self.instance, self.workspace, self.repo_dir, parent=self)

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

                    # Run baseline BEFORE applying test_only
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
                    print(f"      → Running tests (after fix)...")
                    results_after = self.validator.run_tests(test_files=test_files, debug=False)

                    filter_set = None  # No filtering needed

                else:
                    # STANDARD STRATEGY: test_patch is compatible with base_commit

                    # Step 1: Get baseline (no patches)
                    print(f"      → Checking test_patch compatibility...")
                    print(f"      → Running baseline tests...")
                    baseline = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → Baseline: {len(baseline)} tests")

                    # Step 2: Apply test_patch and check
                    self.validator.apply_patch(test_patch, "test_patch")
                    print(f"      → Running tests with test_patch...")
                    with_test_patch = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → With test_patch: {len(with_test_patch)} tests")

                    # Check if any tests are failing (as expected)
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

                    # If test_patch causes significant test count drop, it's incompatible
                    collection_broken = len(with_test_patch) < len(baseline) * 0.5 if baseline else len(with_test_patch) == 0

                    if collection_broken:
                        # FIX-FIRST STRATEGY: test_patch needs solution to work
                        print(f"      ⚠ test_patch incompatible with base_commit")
                        if baseline and len(with_test_patch) < len(baseline) * 0.5:
                            print(f"      → Test count dropped: {len(baseline)} → {len(with_test_patch)} (>50% loss)")
                        else:
                            print(f"      → Tests failed to run with test_patch")
                        print(f"      → Using fix-first strategy")

                        # Reset everything
                        subprocess.run(["git", "checkout", "."], cwd=self.repo_dir, capture_output=True)
                        subprocess.run(["git", "clean", "-fd"], cwd=self.repo_dir, capture_output=True)

                        # PHP: if baseline==0 run test files without --filter to capture all
                        # existing tests for PASS_TO_PASS
                        broader_baseline = {}
                        if len(baseline) == 0:
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
                        subprocess.run(
                            ["git", "apply", "-R", str(solution_file)],
                            cwd=self.repo_dir,
                            capture_output=True
                        )

                        print(f"      → Running tests (without solution)...")
                        results_before = self.validator.run_tests(test_files=test_files, debug=False)

                        # AFTER: Re-apply solution
                        print(f"      → Re-applying solution...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                        # PHP: also run unfiltered after solution for PASS_TO_PASS
                        if broader_baseline:
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
                        filter_set = self.validator.extract_modified_tests()
                        if len(filter_set) == 0:
                            filter_set = None
                            print(f"      → No specific tests extracted, running all modified test files")
                        else:
                            print(f"      → Filtering to {len(filter_set)} modified tests")
                            for test in list(filter_set)[:5]:
                                print(f"        - {test}")
                            if len(filter_set) > 5:
                                print(f"        ... and {len(filter_set) - 5} more")

                        # Store baseline for PASS_TO_PASS calculation
                        if broader_baseline:
                            baseline_for_pass_to_pass = broader_baseline
                        else:
                            baseline_for_pass_to_pass = baseline if len(results_before) == 0 else None

                    else:
                        # STANDARD STRATEGY: test_patch is compatible with base_commit

                        # For PHP: if baseline==0 (filter matched nothing because new test
                        # methods don't exist yet) but the test FILE already exists, run
                        # unfiltered to capture pre-existing tests for PASS_TO_PASS.
                        _php_unfiltered_baseline = {}
                        if len(baseline) == 0:
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

                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

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

                        baseline_for_pass_to_pass = _php_unfiltered_baseline if _php_unfiltered_baseline else None
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

        # Smart filtering: If no explicit filter but we have test_files, create a filter
        smart_filter = None
        if filter_to_modified is None and test_files:
            smart_filter = self._create_smart_filter(test_files)
            if smart_filter:
                print(f"      → Filtering FAIL_TO_PASS to tests matching: {', '.join(sorted(smart_filter)[:5])}")

        for test in all_tests:
            before_status = before.get(test)
            after_status = after.get(test)

            # If baseline provided and before is empty, use baseline for PASS_TO_PASS
            baseline_status = baseline.get(test) if baseline else None

            if after_status == 'PASSED':
                if before_status is None or before_status in ('FAILED', 'ERROR'):
                    # Test was failing/missing and now passes
                    if baseline_status == 'PASSED' and before_status is None:
                        if filter_to_modified and self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                        else:
                            pass_to_pass.append(test)
                    else:
                        # Normal FAIL_TO_PASS logic
                        if filter_to_modified is None:
                            if smart_filter is None or self._test_matches_smart_filter(test, smart_filter):
                                fail_to_pass.append(test)
                        elif self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                else:
                    # Test was passing in before and still passes
                    pass_to_pass.append(test)

        return sorted(fail_to_pass), sorted(pass_to_pass)

    def _test_in_filter(self, test_name: str, filter_set: set) -> bool:
        """Check if test matches any pattern in filter set.

        Handles PHP naming conventions:
          PHP:   ClassName (Namespace\\ClassName)::testBar  (:: separator)
        """
        if test_name in filter_set:
            return True

        # Extract the method name using :: separator (PHP style)
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
                return True

        return False

    def _create_smart_filter(self, test_files: List[str]) -> Optional[Set[str]]:
        """
        Create smart filter patterns from test files to identify which tests are relevant.

        Args:
            test_files: List of test file paths modified in test_patch

        Returns: Set of patterns to match against test names, or None if can't create
        """
        if not test_files:
            return None

        patterns = set()

        for file_path in test_files:
            # Extract identifying information from file path
            file_name = Path(file_path).stem  # e.g., "RoundTest" from "RoundTest.php"

            # Skip generic names
            if file_name in ['test', 'tests', '__init__']:
                continue

            if file_path.endswith('.php'):
                # Test file: use filename as identifier
                identifier = file_name.replace('Test', '').replace('_test', '').replace('_spec', '')
                if identifier and identifier not in ['test', 'tests']:
                    patterns.add(identifier)
                # Also keep the full stem
                if file_name != identifier and file_name not in ['test', 'tests']:
                    patterns.add(file_name)

        return patterns if patterns else None

    def _test_matches_smart_filter(self, test_name: str, smart_filter: Set[str]) -> bool:
        """
        Check if test name matches any pattern in smart filter.

        Args:
            test_name: Full test name (e.g., "Round::testCeilYear")
            smart_filter: Set of patterns (e.g., {"Round", "RoundTest"})

        Returns: True if test matches any pattern
        """
        test_lower = test_name.lower()

        for pattern in smart_filter:
            pattern_lower = pattern.lower()

            # Direct substring match
            if pattern_lower in test_lower:
                return True

            # For patterns ending with 'test', also try without it (e.g., RoundTest -> Round)
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

        return False

    def _split_test_patch(self, test_patch: str) -> Tuple[str, str]:
        """
        Split patch into test-only and fix-only parts.

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

            # Extract file path from header: "diff --git a/path/to/file.php b/..."
            file_match = re.search(r'diff --git a/(.*?) b/', header)
            if file_match:
                file_path = file_match.group(1)

                if self.validator.is_test_file(file_path):
                    test_hunks.append(full_patch)
                else:
                    fix_hunks.append(full_patch)

            i += 2

        return ''.join(test_hunks), ''.join(fix_hunks)

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
            import traceback
            traceback.print_exc()
            print(f"{'='*80}")
            raise
        finally:
            validator.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench PHP instance validation with version detection"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
