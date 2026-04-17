#!/usr/bin/env python3
"""
SWE-bench Ruby Instance Validation Script

Simplified version of full_validation_multilingual.py that only supports Ruby.

Usage:
    python3 full_validation_multilingual_ruby.py instance.json [--output validated.json]
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
        """Determine appropriate Ruby version based on creation date"""
        try:
            year = int(created_at.split('-')[0])
            month = int(created_at.split('-')[1])
        except (ValueError, IndexError):
            year = 2020

        if language == "ruby":
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
        """Detect language - returns 'ruby' or 'unknown'"""
        if (repo_dir / "Gemfile").exists():
            return "ruby"
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
        raise NotImplementedError

    def get_actual_version(self) -> str:
        raise NotImplementedError

    def setup_version(self, required_version: str):
        raise NotImplementedError

    def setup_environment(self):
        """Setup language-specific environment with version detection"""
        print(f"[3/7] Setting up environment...")

        required_version = self.detect_required_version()
        self.detected_version = required_version
        print(f"      → Required version: {required_version}")

        try:
            self.setup_version(required_version)
        except Exception as e:
            print(f"      ⚠ Version setup failed: {e}")

        actual_version = self.get_actual_version()
        self.actual_version = actual_version
        print(f"      → Actual version: {actual_version}")

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
            if req_parts[0] != act_parts[0]:
                return False
            if len(req_parts) > 1 and len(act_parts) > 1:
                if act_parts[1] < req_parts[1] - 2:
                    return False
            return True
        except (ValueError, IndexError):
            return True

    def install_dependencies(self):
        raise NotImplementedError

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
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
        raise NotImplementedError

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
            return

        # Check if failure is due to binary patches
        if "binary patch" in result.stderr and "without full index" in result.stderr:
            print(f"      ⚠ Binary files in patch cannot be applied (missing full index)")
            self._fetch_binary_files_from_patch(patch_content)
            if "assets/" in result.stderr or "/assets/" in patch_content:
                print(f"      → Binary assets detected - attempting to rebuild...")
                self._rebuild_assets()
            else:
                print(f"      → Attempting to apply non-binary changes only...")
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

        # Strategy 2: Three-way merge
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

        # Strategy 3: Apply with reject
        print(f"      ⚠ Three-way merge failed, trying partial application...")
        result = subprocess.run(
            ["git", "apply", "--reject", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        rej_files = list(self.repo_dir.rglob("*.rej"))
        if rej_files:
            print(f"      ⚠ Partial application - {len(rej_files)} conflict(s) in .rej files")
            return
        elif result.returncode == 0:
            print(f"      ✓ Applied with --reject")
            return

        # Strategy 4: patch command with fuzz
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

        raise RuntimeError(f"Failed to apply {patch_name}: {result.stderr}")

    def _fetch_binary_files_from_patch(self, patch_content: str) -> bool:
        """Fetch new binary files from remote repo when patch lacks full index data."""
        pattern = re.compile(
            r'diff --git a/(.+?) b/\1\nnew file mode[^\n]*\nindex 0+\.\.[0-9a-f]+\nBinary files /dev/null'
        )
        new_binary_files = [m.group(1) for m in pattern.finditer(patch_content)]
        if not new_binary_files:
            return False

        missing = [f for f in new_binary_files if not (self.repo_dir / f).exists()]
        if not missing:
            return True

        print(f"      → Fetching {len(missing)} binary file(s) from remote...")
        fetched_any = False
        for branch in ["main", "master"]:
            fetch_result = subprocess.run(
                ["git", "fetch", "--depth=1", "origin", branch],
                cwd=self.repo_dir, capture_output=True, text=True, timeout=60
            )
            if fetch_result.returncode != 0:
                continue
            for file_path in missing:
                target = self.repo_dir / file_path
                if target.exists():
                    continue
                co = subprocess.run(
                    ["git", "checkout", f"origin/{branch}", "--", file_path],
                    cwd=self.repo_dir, capture_output=True, text=True, timeout=30
                )
                if co.returncode == 0:
                    print(f"      ✓ Fetched binary file from origin/{branch}: {file_path}")
                    fetched_any = True
                else:
                    print(f"      ⚠ Could not checkout {file_path} from origin/{branch}")
            break
        return fetched_any

    def _rebuild_assets(self):
        """Try to rebuild binary assets using common patterns"""
        rebuild_commands = []
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

    def run_command(self, cmd: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with environment (uses isolated env if available)"""
        if self.parent and hasattr(self.parent, 'run_in_env'):
            return self.parent.run_in_env(cmd, cwd=cwd or self.repo_dir, timeout=timeout)

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
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=124,
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )


# ============================================================================
# RUBY VALIDATOR
# ============================================================================

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
        """Get current Ruby version"""
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
            brew_check = subprocess.run(["which", "brew"], capture_output=True)
            if brew_check.returncode == 0:
                subprocess.run(["brew", "install", "rbenv", "ruby-build"], capture_output=True, timeout=300)
            else:
                print(f"      ⚠ Cannot install rbenv automatically")
                print(f"      → Continuing with Ruby {current_version} (tests will likely fail)")
                self.setup_failed = True
                return

        # Check if required version is already installed in rbenv
        rbenv_root_result = subprocess.run(
            ["rbenv", "root"],
            capture_output=True,
            text=True
        )

        if rbenv_root_result.returncode == 0:
            rbenv_root = Path(rbenv_root_result.stdout.strip())
            versions_dir = rbenv_root / "versions"

            if versions_dir.exists():
                installed_versions = []
                for version_dir in versions_dir.iterdir():
                    if version_dir.is_dir():
                        installed_versions.append(version_dir.name)

                pattern = rf"^{re.escape(required_version)}\.\d+$"
                compatible_versions = [v for v in installed_versions if re.match(pattern, v)]

                if compatible_versions:
                    full_version = sorted(compatible_versions)[-1]
                    print(f"      ✓ Found existing Ruby {full_version} in rbenv")

                    ruby_bin_path = versions_dir / full_version / "bin" / "ruby"
                    if ruby_bin_path.exists():
                        self.ruby_binary = str(ruby_bin_path)
                        self.gem_binary = str(ruby_bin_path.parent / "gem")
                        self.bundle_binary = str(ruby_bin_path.parent / "bundle")
                        print(f"      ✓ Using rbenv Ruby {full_version} for tests")
                        return

        # Need to install - get list of available versions
        print(f"      → Installing Ruby {required_version} via rbenv...")

        list_result = subprocess.run(
            ["ruby-build", "--definitions"],
            capture_output=True, text=True, timeout=30
        )
        if list_result.returncode != 0:
            list_result = subprocess.run(
                ["rbenv", "install", "--list"],
                capture_output=True, text=True, timeout=30
            )

        full_version = None
        if list_result.returncode == 0:
            pattern = rf"^\s*({re.escape(required_version)}\.\d+)\s*$"
            matches = []
            for line in list_result.stdout.split('\n'):
                match = re.match(pattern, line)
                if match:
                    matches.append(match.group(1))

            if matches:
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

        # Install the full Ruby version using rbenv with retry logic
        max_retries = 2
        for attempt in range(max_retries):
            result = subprocess.run(
                ["rbenv", "install", "-s", full_version],
                capture_output=True, text=True, timeout=1800
            )

            if result.returncode == 0:
                break

            if "Error" in result.stderr or "curl" in result.stderr or "download" in result.stderr.lower():
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue

            if attempt == max_retries - 1:
                print(f"      ⚠ Failed to install Ruby {full_version} after {max_retries} attempts")
                print(f"      → Error: {result.stderr[:300]}")

                # Try Homebrew as fallback (macOS only)
                brew_check = subprocess.run(["which", "brew"], capture_output=True)
                if brew_check.returncode == 0:
                    major_minor = '.'.join(required_version.split('.')[:2])
                    major = required_version.split('.')[0]

                    formulae_to_try = [f"ruby@{major_minor}"]
                    if major == "3":
                        minor = int(required_version.split('.')[1]) if len(required_version.split('.')) > 1 else 0
                        for newer_minor in range(minor + 1, 10):
                            formulae_to_try.append(f"ruby@{major}.{newer_minor}")

                    for brew_formula in formulae_to_try:
                        brew_result = subprocess.run(
                            ["brew", "install", brew_formula],
                            capture_output=True, text=True, timeout=600
                        )

                        if brew_result.returncode == 0:
                            brew_prefix_result = subprocess.run(
                                ["brew", "--prefix", brew_formula],
                                capture_output=True, text=True
                            )

                            if brew_prefix_result.returncode == 0:
                                brew_ruby_path = Path(brew_prefix_result.stdout.strip()) / "bin" / "ruby"
                                if brew_ruby_path.exists():
                                    self.ruby_binary = str(brew_ruby_path)
                                    self.gem_binary = str(brew_ruby_path.parent / "gem")
                                    self.bundle_binary = str(brew_ruby_path.parent / "bundle")
                                    print(f"      ✓ Ruby via {brew_formula}")
                                    return
                                else:
                                    print(f"      ⚠ Ruby binary not found at {brew_ruby_path}")
                            else:
                                print(f"      ⚠ Could not get Homebrew prefix for {brew_formula}")
                        else:
                            if "disabled" in brew_result.stderr.lower():
                                continue
                            elif "no available formula" in brew_result.stderr.lower():
                                break
                            else:
                                print(f"      ⚠ {brew_formula} install failed: {brew_result.stderr[:200]}")
                                break

                # Try ruby-install as final fallback
                ruby_install_check = subprocess.run(["which", "ruby-install"], capture_output=True)
                if ruby_install_check.returncode == 0:
                    ruby_install_result = subprocess.run(
                        ["ruby-install", "--no-reinstall", "ruby", full_version],
                        capture_output=True, text=True, timeout=1800
                    )

                    if ruby_install_result.returncode == 0:
                        ruby_install_path = Path.home() / ".rubies" / f"ruby-{full_version}" / "bin" / "ruby"
                        if ruby_install_path.exists():
                            self.ruby_binary = str(ruby_install_path)
                            self.gem_binary = str(ruby_install_path.parent / "gem")
                            self.bundle_binary = str(ruby_install_path.parent / "bundle")
                            print(f"      ✓ Ruby {full_version} installed via ruby-install")
                            return

                print(f"      → Continuing with Ruby {current_version} (tests will likely fail)")
                self.setup_failed = True
                return

        # Set up to use the specific Ruby version
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

        bundle_config = self.repo_dir / ".bundle"
        vendor_bundle = self.repo_dir / "vendor" / "bundle"

        if bundle_config.exists():
            shutil.rmtree(bundle_config, ignore_errors=True)
        if vendor_bundle.exists():
            shutil.rmtree(vendor_bundle, ignore_errors=True)

        gemfile_lock = self.repo_dir / "Gemfile.lock"
        if gemfile_lock.exists():
            gemfile_lock.unlink()

        subprocess.run([gem_cmd, "install", "bundler"], capture_output=True, text=True, timeout=300)

        result = self.run_command([bundle_cmd, "install", "--path", "vendor/bundle"], timeout=1200)
        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else result.stdout
            print(f"      ⚠ Bundle reinstall failed: {error_msg[:200]}")
            result = self.run_command([bundle_cmd, "install"], timeout=1200)
            if result.returncode == 0:
                print(f"      ✓ Dependencies reinstalled (system location)")
        else:
            print(f"      ✓ Dependencies reinstalled")

    def install_dependencies(self):
        """Install bundler dependencies"""
        print(f"[4/7] Installing dependencies...")

        if (self.repo_dir / "Gemfile").exists():
            if self.setup_failed:
                print(f"      ⚠ Skipping dependency install due to environment setup failure")
                return

            gem_cmd = getattr(self, 'gem_binary', 'gem')
            bundle_cmd = getattr(self, 'bundle_binary', 'bundle')

            bundler_result = subprocess.run(
                [gem_cmd, "install", "bundler"],
                capture_output=True, text=True, timeout=300
            )

            result = self.run_command([bundle_cmd, "install", "--path", "vendor/bundle"], timeout=1200)
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else result.stdout
                print(f"      ⚠ Bundle install failed (code {result.returncode})")
                print(f"      → Error: {error_msg[:500]}")

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
        if self.setup_failed:
            print(f"      ⚠ Skipping test execution due to failed environment setup")
            print(f"      → Required: {self.detected_version}, Actual: {self.actual_version}")
            return {}

        status_map = {}
        bundle_cmd = getattr(self, 'bundle_binary', 'bundle')

        if (self.repo_dir / "spec").exists():
            # RSpec: use JSON formatter so each `it` example gets a unique id that embeds
            # the spec file path (e.g. "./spec/.../block_delimiters_spec.rb[1:2:3]").
            json_out = tempfile.mktemp(suffix='.json')
            cmd = [
                bundle_cmd, "exec", "rspec",
                "--format", "progress",
                "--format", "json",
                "--out", json_out,
            ]
            # Run only the relevant test files to avoid timeouts on large projects
            # and prevent position-shift inflation of FAIL_TO_PASS counts.
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
                # Fallback: re-run with documentation format
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
            # Test::Unit/Minitest with rake
            # TESTOPTS=-v enables verbose output so passing tests also print their real names,
            # making before/after name matching possible for FAIL_TO_PASS detection.
            if test_files:
                # Run tests for each file separately and merge results
                all_output = ""
                for test_file in test_files:
                    cmd = [bundle_cmd, "exec", "rake", "test", f"TEST={test_file}", "TESTOPTS=-v"]
                    result = self.run_command(cmd, timeout=600)
                    all_output += result.stdout + result.stderr + "\n"
                output = all_output
            else:
                cmd = [bundle_cmd, "exec", "rake", "test", "TESTOPTS=-v"]
                result = self.run_command(cmd, timeout=600)
                output = result.stdout + result.stderr

            # Parse Test::Unit/Minitest output
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
                match = re.search(r'(Failure|Error):\s*test:\s*(.+?)\(([^)]+)\)', line)
                if match:
                    test_desc = match.group(2).strip()
                    test_class = match.group(3).split('::')[0]
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
                detected_count = len(status_map)
                if detected_count < total_tests:
                    missing_failed = max(0, failures - sum(1 for s in status_map.values() if s in ['FAILED', 'ERROR']))
                    missing_passed = max(0, passed - sum(1 for s in status_map.values() if s == 'PASSED'))

                    if test_files:
                        for i, test_file in enumerate(test_files):
                            test_file_name = test_file.replace('/', '::').replace('.rb', '')

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
# MAIN VALIDATOR
# ============================================================================

class MultilingualValidator:
    """Main validator - Ruby only"""

    def __init__(self, instance: dict, workspace: Path, keep_env: bool = False):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.keep_env = keep_env
        self.language = None
        self.validator = None

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
        """Detect programming language (expects Ruby)"""
        print(f"[2/7] Detecting language...")
        self.language = LanguageDetector.detect(self.repo_dir)
        print(f"      ✓ Detected: {self.language}")

        if self.language != "ruby":
            raise RuntimeError(f"Expected ruby project, detected: {self.language}")

    def run_in_env(self, command: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with validator environment variables"""
        try:
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
        """Create RubyValidator"""
        self.validator = RubyValidator(self.instance, self.workspace, self.repo_dir, parent=self)

    def validate(self) -> Tuple[List[str], List[str]]:
        """Run full validation workflow"""
        try:
            self.setup_repo()
            self.detect_language()
            self.create_validator()

            self.validator.setup_environment()
            self.validator.install_dependencies()

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

            print(f"[6/7] Running tests...")

            filter_set = None
            baseline_for_pass_to_pass = None

            if not test_patch:
                # No test_patch: run baseline → apply solution → run after
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
                # No solution_patch: run baseline → apply test_patch → run after
                print(f"      → Running baseline...")
                results_before = self.validator.run_tests(test_files=test_files, debug=False)

                print(f"      → Applying test patch...")
                self.validator.apply_patch(test_patch, "test_patch")

                print(f"      → Running tests with test_patch...")
                results_after = self.validator.run_tests(test_files=test_files, debug=False)

            else:
                # Both patches exist - determine strategy
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

                    print(f"      → Running baseline tests (before test_patch)...")
                    _baseline_before_test_only = self.validator.run_tests(test_files=test_files, debug=False)

                    if test_only:
                        self.validator.apply_patch(test_only, "test_only")
                    print(f"      → Running tests (before fix)...")
                    results_before = self.validator.run_tests(test_files=test_files, debug=False)

                    if len(results_before) == 0 and len(_baseline_before_test_only) > 0:
                        print(f"      → Before-fix build failed (test_patch references new APIs); using baseline for PASS_TO_PASS")
                        baseline_for_pass_to_pass = _baseline_before_test_only

                    if fix_only:
                        self.validator.apply_patch(fix_only, "fix_from_test_patch")
                    if solution_patch:
                        self.validator.apply_patch(solution_patch, "solution_patch")

                    print(f"      → Running tests (after fix)...")
                    results_after = self.validator.run_tests(test_files=test_files, debug=False)

                    filter_set = None

                else:
                    # Standard compatibility check
                    print(f"      → Checking test_patch compatibility...")
                    print(f"      → Running baseline tests...")
                    baseline = self.validator.run_tests(test_files=test_files, debug=False)
                    _base_fail_count = sum(1 for s in baseline.values() if s in ['FAILED', 'ERROR'])
                    print(f"      → Baseline: {len(baseline)} tests ({_base_fail_count} failing)")

                    self.validator.apply_patch(test_patch, "test_patch")
                    print(f"      → Running tests with test_patch...")
                    with_test_patch = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → With test_patch: {len(with_test_patch)} tests")

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

                    collection_broken = (
                        len(with_test_patch) < len(baseline) * 0.5 if baseline
                        else len(with_test_patch) == 0
                    )

                    if collection_broken:
                        # Fix-first strategy: test_patch needs solution to compile/run
                        print(f"      ⚠ test_patch incompatible with base_commit")
                        if baseline:
                            print(f"      → Test count dropped: {len(baseline)} → {len(with_test_patch)} (>50% loss)")
                        print(f"      → Using fix-first strategy")

                        subprocess.run(["git", "checkout", "."], cwd=self.repo_dir, capture_output=True)
                        subprocess.run(["git", "clean", "-fd"], cwd=self.repo_dir, capture_output=True)

                        baseline_for_pass_to_pass = baseline if baseline else None

                        print(f"      → Applying solution patch first...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        print(f"      → Applying test patch...")
                        self.validator.apply_patch(test_patch, "test_patch")

                        # Temporarily revert solution to see tests fail
                        print(f"      → Temporarily reverting solution...")
                        solution_file = self.workspace / "solution_reverse.patch"
                        solution_file.write_text(solution_patch)
                        _revert_result = subprocess.run(
                            ["git", "apply", "-R", "--whitespace=fix", str(solution_file)],
                            cwd=self.repo_dir, capture_output=True
                        )
                        if _revert_result.returncode != 0:
                            print(f"      ⚠ git apply -R failed, using file-level revert...")
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

                        print(f"      → Running tests (without solution)...")
                        results_before = self.validator.run_tests(test_files=test_files, debug=False)

                        # Re-apply solution
                        print(f"      → Re-applying solution...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        self.validator.reinstall_dependencies_if_needed()

                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                    else:
                        # Standard strategy: test_patch is compatible with base_commit
                        print(f"      → Using standard strategy")

                        # results_before = with_test_patch (tests should fail)
                        results_before = with_test_patch

                        print(f"      → Applying solution patch...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        self.validator.reinstall_dependencies_if_needed()

                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                        baseline_for_pass_to_pass = baseline if baseline else None
                        filter_set = None

            print(f"      ✓ Tests completed ({len(results_before)} before, {len(results_after)} after)")

            if len(results_before) == 0 and len(results_after) == 0:
                print(f"      ⚠ WARNING: No tests were detected or run!")
                if hasattr(self.validator, 'setup_failed') and self.validator.setup_failed:
                    print(f"      → Environment setup failed - unable to run tests")
                    print(f"      → Required: {self.validator.detected_version}, Actual: {self.validator.actual_version}")
                else:
                    print(f"      This may indicate a version mismatch or build failure.")

            print(f"[7/7] Comparing results...")

            before_failed = sum(1 for s in results_before.values() if s in ['FAILED', 'ERROR'])
            before_passed = sum(1 for s in results_before.values() if s == 'PASSED')
            after_failed = sum(1 for s in results_after.values() if s in ['FAILED', 'ERROR'])
            after_passed = sum(1 for s in results_after.values() if s == 'PASSED')

            print(f"      → Before: {before_failed} failed, {before_passed} passed")
            print(f"      → After: {after_failed} failed, {after_passed} passed")

            self.snapshot_validation_passed = None

            fail_to_pass, pass_to_pass = self.compare_results(
                results_before, results_after, filter_set, test_files, baseline_for_pass_to_pass
            )

            print(f"      ✓ FAIL_TO_PASS: {len(fail_to_pass)} tests")
            for test in fail_to_pass[:10]:
                print(f"        - {test}")
            if len(fail_to_pass) > 10:
                print(f"        ... and {len(fail_to_pass) - 10} more")

            print(f"      ✓ PASS_TO_PASS: {len(pass_to_pass)} tests")

            if len(fail_to_pass) == 0 and before_failed > 0:
                print(f"      ⚠ WARNING: {before_failed} tests failed before, but none transitioned to PASS")
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
        """Compare test results to find FAIL_TO_PASS and PASS_TO_PASS"""
        fail_to_pass = []
        pass_to_pass = []

        all_tests = set(before.keys()) | set(after.keys())
        if baseline:
            all_tests |= set(baseline.keys())

        # Smart filtering: create filter based on test file names
        smart_filter = None
        if filter_to_modified is None and test_files:
            smart_filter = self._create_smart_filter(test_files)
            if smart_filter:
                print(f"      → Filtering FAIL_TO_PASS to tests matching: {', '.join(sorted(smart_filter)[:5])}")

        for test in all_tests:
            before_status = before.get(test)
            after_status = after.get(test)
            baseline_status = baseline.get(test) if baseline else None

            if after_status == 'PASSED':
                if before_status is None or before_status in ('FAILED', 'ERROR'):
                    if baseline_status == 'PASSED' and before_status is None:
                        # Test was passing in baseline — PASS_TO_PASS (unless explicitly modified)
                        if filter_to_modified and self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                        else:
                            pass_to_pass.append(test)
                    else:
                        # Skip pre-existing failures (not introduced by test_patch)
                        if baseline_status in ('FAILED', 'ERROR'):
                            pass
                        elif filter_to_modified is None:
                            if smart_filter is None or self._test_matches_smart_filter(test, smart_filter):
                                fail_to_pass.append(test)
                        elif self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                else:
                    # Test was passing before and still passes
                    pass_to_pass.append(test)

        return sorted(fail_to_pass), sorted(pass_to_pass)

    def _test_in_filter(self, test_name: str, filter_set: set) -> bool:
        """Check if test matches any pattern in filter set."""
        if test_name in filter_set:
            return True

        if '::' in test_name:
            test_method = test_name.split('::')[-1]
        else:
            test_parts = test_name.split('.')
            test_method = test_parts[-1] if test_parts else test_name

        if test_method in filter_set:
            return True

        for pattern in filter_set:
            pattern_parts = pattern.split('.')
            pattern_method = pattern_parts[-1] if pattern_parts else pattern

            if test_method == pattern_method:
                test_parts_dot = test_name.split('.')
                if len(test_parts_dot) >= 2 and len(pattern_parts) >= 2:
                    if test_parts_dot[-2] == pattern_parts[-2]:
                        return True
                else:
                    return True

        return False

    def _create_smart_filter(self, test_files: List[str]) -> Optional[Set[str]]:
        """Create smart filter patterns from test file names."""
        if not test_files:
            return None

        patterns = set()

        for file_path in test_files:
            file_name = Path(file_path).stem

            if file_name in ['test', 'tests', '__init__', 'mod']:
                continue

            if file_path.endswith('.rb'):
                identifier = file_name.replace('test_', '').replace('_test', '').replace('_spec', '')
                if identifier and identifier not in ['test', 'tests']:
                    patterns.add(identifier)
                # Keep full stem so RSpec JSON ids (e.g. "./spec/.../block_delimiters_spec.rb[1:2:3]") match
                if file_name != identifier and file_name not in ['test', 'tests']:
                    patterns.add(file_name)

        # Also extract describe block names from test_patch
        test_patch = self.instance.get('test_patch', '')
        if test_patch:
            describe_blocks = self._extract_describe_blocks_from_patch(test_patch)
            patterns.update(describe_blocks)

        return patterns if patterns else None

    def _test_matches_smart_filter(self, test_name: str, smart_filter: Set[str]) -> bool:
        """Check if test name matches any pattern in smart filter."""
        test_lower = test_name.lower()
        test_norm = re.sub(r'[^a-z0-9]+', ' ', test_lower).strip()

        for pattern in smart_filter:
            pattern_lower = pattern.lower()
            pattern_norm = re.sub(r'[^a-z0-9]+', ' ', pattern_lower).strip()

            if pattern_lower in test_lower:
                return True
            if pattern_norm and pattern_norm in test_norm:
                return True

            if pattern_lower.endswith('test'):
                pattern_without_test = pattern_lower[:-4]
                if pattern_without_test and pattern_without_test in test_lower:
                    return True

            if pattern_lower.startswith('test'):
                pattern_without_test = pattern_lower[4:]
                if pattern_without_test and pattern_without_test in test_lower:
                    return True

            for sep in ['::', '.', '__', '/', '_']:
                if f"{sep}{pattern_lower}{sep}" in test_lower:
                    return True
                if test_lower.startswith(f"{pattern_lower}{sep}"):
                    return True
                if test_lower.endswith(f"{sep}{pattern_lower}"):
                    return True

            words = [w for w in re.split(r'[_\-]', pattern_lower) if len(w) > 2]
            if len(words) >= 2 and all(w in test_lower for w in words):
                return True

        return False

    def _extract_describe_blocks_from_patch(self, test_patch: str) -> Set[str]:
        """Extract describe/context block names from test_patch for Ruby."""
        describe_names = set()

        try:
            in_hunk_with_additions = False
            hunk_lines = []

            for line in test_patch.split('\n'):
                if line.startswith('@@'):
                    if in_hunk_with_additions:
                        self._extract_ruby_patterns_from_lines(hunk_lines, describe_names)
                    in_hunk_with_additions = False
                    hunk_lines = []
                elif line.startswith('+') and not line.startswith('+++'):
                    in_hunk_with_additions = True
                    hunk_lines.append(line)
                elif not line.startswith('-') and not line.startswith('+++') and not line.startswith('---'):
                    hunk_lines.append(line)

            if in_hunk_with_additions:
                self._extract_ruby_patterns_from_lines(hunk_lines, describe_names)

        except Exception:
            pass

        return describe_names

    def _extract_ruby_patterns_from_lines(self, lines: List[str], patterns: Set[str]):
        """Extract RSpec describe/context names from lines of Ruby code."""
        for line in lines:
            clean_line = line.lstrip('+ \t')

            # RSpec: describe 'name', describe "name", context 'name', context "name"
            for match in re.finditer(r'(?:describe|context)\s+[\'"]([^\'"]+)[\'"]', clean_line):
                name = match.group(1)
                if name and name not in ['test', 'tests']:
                    patterns.add(name)

            # RSpec: describe ClassName
            for match in re.finditer(r'describe\s+([A-Z]\w+)', clean_line):
                name = match.group(1)
                if name:
                    patterns.add(name)

    def _split_test_patch(self, test_patch: str) -> Tuple[str, str]:
        """Split patch into test-only and fix-only parts."""
        test_hunks = []
        fix_hunks = []

        parts = re.split(r'(diff --git a/[^\n]+\n)', test_patch)

        i = 1
        while i < len(parts) - 1:
            header = parts[i]
            content = parts[i + 1]
            full_patch = header + content

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
        description="SWE-bench Ruby instance validation"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
