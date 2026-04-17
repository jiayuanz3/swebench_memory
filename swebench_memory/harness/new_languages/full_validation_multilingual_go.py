#!/usr/bin/env python3
"""
SWE-bench Go Instance Validation Script

Go-only version extracted from full_validation_multilingual.py.
Supports: caddyserver/caddy, gin-gonic/gin, gohugoio/hugo, prometheus/prometheus,
          hashicorp/terraform, and any other Go (go.mod) project.

Usage:
    python3 full_validation_multilingual_go.py instance.json [--output validated.json]
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
        """Determine appropriate Go version based on creation date"""
        try:
            year = int(created_at.split('-')[0])
            month = int(created_at.split('-')[1])
        except (ValueError, IndexError):
            year = 2020

        if language == "go":
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

        return "latest"


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

class LanguageDetector:
    """Detect Go from repository structure"""

    @staticmethod
    def detect(repo_dir: Path) -> str:
        """Returns 'go' if go.mod exists, else 'unknown'"""
        if (repo_dir / "go.mod").exists():
            return "go"
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
        self.env_vars['CGO_ENABLED'] = '0'

        self.detected_version = None
        self.actual_version = None
        self.parent = parent
        self.setup_failed = False

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
        test_files = []
        for match in re.finditer(r'diff --git a/(.*?) b/', patch):
            file_path = match.group(1)
            if self.is_test_file(file_path):
                test_files.append(file_path)
        return list(set(test_files))

    def is_test_file(self, file_path: str) -> bool:
        raise NotImplementedError

    def _rebuild_assets(self):
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

        if "binary patch" in result.stderr and "without full index" in result.stderr:
            print(f"      ⚠ Binary files in patch cannot be applied (missing full index)")
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

        # Strategy 3: Reject
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

    def run_command(self, cmd: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with environment (uses isolated conda env if available)"""
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
# GO VALIDATOR
# ============================================================================

class GoValidator(BaseValidator):
    """Validator for Go projects"""

    def is_test_file(self, file_path: str) -> bool:
        if file_path.endswith('_test.go'):
            return True
        if '/testdata/' in file_path:
            return True
        return False

    def is_version_compatible(self, required: str, actual: str) -> bool:
        """Go is generally backward compatible - newer versions usually work"""
        try:
            req_parts = [int(x) for x in required.split('.')]
            act_parts = [int(x) for x in actual.split('.')]
            if req_parts[0] != act_parts[0]:
                return False
            return act_parts[1] >= req_parts[1]
        except:
            return required == actual

    def detect_required_version(self) -> str:
        """Detect required Go version from go.mod"""
        go_mod = self.repo_dir / "go.mod"
        if go_mod.exists():
            content = go_mod.read_text()
            match = re.search(r'^go\s+(\d+\.\d+)', content, re.MULTILINE)
            if match:
                return match.group(1)

        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "go")

        return "1.20"

    def get_actual_version(self) -> str:
        """Get current Go version (or custom version if set)"""
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

        import tarfile

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
                if version_parts[0] == 1 and version_parts[1] < 16:
                    print(f"      ⚠ Go {required_version} doesn't support Apple Silicon (darwin-arm64)")
                    print(f"      → Minimum version for darwin-arm64 is Go 1.16")
                    print(f"      → Using Go {current_version} instead (compatibility mode)")
                    return
            except:
                pass

        full_version = required_version
        if required_version.count('.') == 1:
            for patch in range(10, -1, -1):
                test_version = f"{required_version}.{patch}"
                test_url = f"https://go.dev/dl/go{test_version}.{os_name}-{arch}.tar.gz"
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
        """Verify that the Go binary works correctly"""
        go_cmd = getattr(self, 'go_binary', 'go')
        try:
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
        if packages:
            for pkg in list(packages)[:3]:
                result = self.run_command([go_cmd, "build", "-o", "/dev/null", pkg], timeout=300)
                if result.returncode != 0:
                    print(f"      ✗ Compilation check failed for {pkg}:")
                    for line in result.stderr.split('\n')[:10]:
                        if line.strip():
                            print(f"        {line}")
                    return False
        else:
            result = self.run_command([go_cmd, "build", "./..."], timeout=300)
            if result.returncode != 0:
                print(f"      ✗ Compilation check failed:")
                for line in result.stderr.split('\n')[:10]:
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
            packages = self._extract_go_packages(test_files)
            if packages:
                print(f"      → Targeting {len(packages)} package(s)")
                for pkg in list(packages)[:5]:
                    print(f"        - {pkg}")
                if len(packages) > 5:
                    print(f"        ... and {len(packages) - 5} more")

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
                            original_go = go_cmd
                            go_cmd = "go"
                            cmd = [go_cmd, "test", "-v", pkg]
                            result = self.run_command(cmd, timeout=600)
                            output = result.stdout + result.stderr
                            go_cmd = original_go

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
            cmd = [go_cmd, "test", "-v", "./..."]
            result = self.run_command(cmd, timeout=600)
            output = result.stdout + result.stderr
            self._parse_go_test_output(output, status_map)

        return status_map

    def _extract_go_packages(self, test_files: List[str]) -> set:
        """Extract Go package paths from test files"""
        packages = set()
        for file_path in test_files:
            if file_path.endswith('_test.go'):
                pkg_dir = str(Path(file_path).parent)
                if pkg_dir == '.':
                    packages.add('.')
                else:
                    packages.add(f"./{pkg_dir}")
            elif '/testdata/' in file_path:
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
        """Extract specific test functions modified in test_patch"""
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')
        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            if self.is_test_file(file_path):
                package = str(Path(file_path).parent)
                current_test = None
                for line in patch_content.split('\n'):
                    line_content = line[1:] if line and line[0] in ' +-' else line
                    test_match = re.search(r'func\s+(Test\w+)\s*\(', line_content)
                    if test_match:
                        current_test = test_match.group(1)
                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line.startswith('+++') and
                                not line.startswith('---'))
                    if current_test and is_change:
                        modified_tests.add(f"{package}.{current_test}")
                        current_test = None

            i += 2

        return modified_tests


# ============================================================================
# GO VALIDATOR ORCHESTRATOR
# ============================================================================

class GoValidationRunner:
    """Main runner that orchestrates Go validation"""

    def __init__(self, instance: dict, workspace: Path, keep_env: bool = False):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.keep_env = keep_env
        self.language = None
        self.validator = None
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
        """Detect programming language (must be Go)"""
        print(f"[2/7] Detecting language...")
        self.language = LanguageDetector.detect(self.repo_dir)
        print(f"      ✓ Detected: {self.language}")
        if self.language != "go":
            raise RuntimeError(f"Expected Go project, detected: {self.language}")

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
        """Create GoValidator"""
        self.validator = GoValidator(self.instance, self.workspace, self.repo_dir, parent=self)

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
                # No test_patch: baseline vs after solution
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
                # No solution_patch: baseline vs after test_patch
                print(f"      → Running baseline...")
                results_before = self.validator.run_tests(test_files=test_files, debug=False)

                print(f"      → Applying test patch...")
                self.validator.apply_patch(test_patch, "test_patch")

                print(f"      → Running tests with test_patch...")
                results_after = self.validator.run_tests(test_files=test_files, debug=False)

            else:
                # Both patches exist — determine strategy

                # STRATEGY 1: test_patch contains both tests AND fixes
                has_fix_in_test_patch = False
                for match in re.finditer(r'diff --git a/(.*?) b/', test_patch):
                    file_path = match.group(1)
                    if not self.validator.is_test_file(file_path):
                        has_fix_in_test_patch = True
                        break

                if has_fix_in_test_patch:
                    print(f"      → test_patch contains fix, splitting...")
                    test_only, fix_only = self._split_test_patch(test_patch)

                    # Run baseline BEFORE applying test_only: test_only may reference new APIs
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
                    # STRATEGY 2 & 3: Standard compatibility check
                    print(f"      → Checking test_patch compatibility...")
                    print(f"      → Running baseline tests...")
                    baseline = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → Baseline: {len(baseline)} tests")

                    self.validator.apply_patch(test_patch, "test_patch")
                    print(f"      → Running tests with test_patch...")
                    with_test_patch = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → With test_patch: {len(with_test_patch)} tests")

                    failing_tests = [n for n, s in with_test_patch.items() if s in ['FAILED', 'ERROR']]
                    passing_tests = [n for n, s in with_test_patch.items() if s == 'PASSED']
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

                    collection_broken = len(with_test_patch) < len(baseline) * 0.5 if baseline else len(with_test_patch) == 0

                    if collection_broken:
                        # FIX-FIRST STRATEGY
                        print(f"      ⚠ test_patch incompatible with base_commit")
                        if baseline and len(with_test_patch) < len(baseline) * 0.5:
                            print(f"      → Test count dropped: {len(baseline)} → {len(with_test_patch)} (>50% loss)")
                        else:
                            print(f"      → Tests failed to run with test_patch")
                        print(f"      → Using fix-first strategy")

                        subprocess.run(["git", "checkout", "."], cwd=self.repo_dir, capture_output=True)
                        subprocess.run(["git", "clean", "-fd"], cwd=self.repo_dir, capture_output=True)

                        print(f"      → Applying solution patch first...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        print(f"      → Applying test patch...")
                        self.validator.apply_patch(test_patch, "test_patch")

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

                        print(f"      → Re-applying solution...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                        if hasattr(self.validator, 'extract_modified_tests'):
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

                        baseline_for_pass_to_pass = baseline if len(results_before) == 0 else None

                    else:
                        # STANDARD STRATEGY
                        print(f"      → Using standard strategy")
                        results_before = with_test_patch

                        print(f"      → Applying solution patch...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(test_files=test_files, debug=False)

                        baseline_for_pass_to_pass = None
                        filter_set = None

            print(f"      ✓ Tests completed ({len(results_before)} before, {len(results_after)} after)")

            if len(results_before) == 0 and len(results_after) == 0:
                print(f"      ⚠ WARNING: No tests were detected or run!")
                if hasattr(self.validator, 'setup_failed') and self.validator.setup_failed:
                    print(f"      → Environment setup failed - unable to run tests")
                    print(f"      → Required: {self.validator.detected_version}, Actual: {self.validator.actual_version}")
                else:
                    print(f"      This may indicate a version mismatch or build failure.")
                    print(f"      Required: {self.validator.detected_version}, Actual: {self.validator.actual_version}")

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
                print(f"      Tests that failed before and after:")
                still_failing = [n for n in results_before
                                 if results_before.get(n) in ['FAILED', 'ERROR']
                                 and results_after.get(n) in ['FAILED', 'ERROR']]
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
                        pass_to_pass.append(test)
                    else:
                        if filter_to_modified is None:
                            if smart_filter is None or self._test_matches_smart_filter(test, smart_filter):
                                fail_to_pass.append(test)
                        elif self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                else:
                    pass_to_pass.append(test)

        return sorted(fail_to_pass), sorted(pass_to_pass)

    def _test_in_filter(self, test_name: str, filter_set: set) -> bool:
        if test_name in filter_set:
            return True
        test_parts = test_name.split('.')
        test_method = test_parts[-1] if test_parts else test_name
        for pattern in filter_set:
            pattern_parts = pattern.split('.')
            pattern_method = pattern_parts[-1] if pattern_parts else pattern
            if test_method == pattern_method:
                if len(test_parts) >= 2 and len(pattern_parts) >= 2:
                    if test_parts[-2] == pattern_parts[-2]:
                        return True
                else:
                    return True
        return False

    def _extract_describe_blocks_from_patch(self, test_patch: str) -> Set[str]:
        """Extract test function names from patch for Go"""
        describe_names = set()
        try:
            in_hunk_with_additions = False
            hunk_lines = []
            for line in test_patch.split('\n'):
                if line.startswith('@@'):
                    if in_hunk_with_additions:
                        self._extract_patterns_from_lines(hunk_lines, describe_names)
                    in_hunk_with_additions = False
                    hunk_lines = []
                elif line.startswith('+') and not line.startswith('+++'):
                    in_hunk_with_additions = True
                    hunk_lines.append(line)
                elif not line.startswith('-') and not line.startswith('+++') and not line.startswith('---'):
                    hunk_lines.append(line)
            if in_hunk_with_additions:
                self._extract_patterns_from_lines(hunk_lines, describe_names)
        except Exception:
            pass
        return describe_names

    def _extract_patterns_from_lines(self, lines: List[str], describe_names: Set[str]):
        """Extract Go test function names from lines of code"""
        for line in lines:
            clean_line = line.lstrip('+ \t')
            for match in re.finditer(r'func\s+Test(\w+)', clean_line):
                name = match.group(1)
                if name:
                    describe_names.add(name)

    def _create_smart_filter(self, test_files: List[str]) -> Optional[Set[str]]:
        """Create smart filter patterns from Go test files"""
        if not test_files:
            return None

        patterns = set()
        for file_path in test_files:
            file_name = Path(file_path).stem
            if file_name in ['test', 'tests', '__init__', 'mod']:
                continue
            if file_path.endswith('.go'):
                identifier = file_name.replace('test_', '').replace('_test', '')
                if identifier and identifier not in ['test', 'tests']:
                    patterns.add(identifier)
                if file_name != identifier and file_name not in ['test', 'tests']:
                    patterns.add(file_name)

        test_patch = self.instance.get('test_patch', '')
        if test_patch:
            describe_blocks = self._extract_describe_blocks_from_patch(test_patch)
            patterns.update(describe_blocks)

        return patterns if patterns else None

    def _test_matches_smart_filter(self, test_name: str, smart_filter: Set[str]) -> bool:
        """Check if Go test name matches any pattern in smart filter"""
        test_lower = test_name.lower()

        for pattern in smart_filter:
            pattern_lower = pattern.lower()

            if pattern_lower in test_lower:
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
        runner = GoValidationRunner(instance, workspace, keep_env)

        try:
            fail_to_pass, pass_to_pass = runner.validate()

            instance['FAIL_TO_PASS'] = fail_to_pass
            instance['PASS_TO_PASS'] = pass_to_pass
            instance['environment_setup_commit'] = instance['base_commit']

            if hasattr(runner.validator, 'detected_version'):
                instance['detected_version'] = runner.validator.detected_version
                instance['actual_version'] = runner.validator.actual_version

            with open(output_path, 'w') as f:
                json.dump(instance, f, indent=2)

            print(f"\n{'='*80}")
            print(f"✓ Validation complete!")
            print(f"  Language: go")
            if hasattr(runner.validator, 'detected_version'):
                print(f"  Required version: {runner.validator.detected_version}")
                print(f"  Actual version: {runner.validator.actual_version}")
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
            runner.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench Go instance validation"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
