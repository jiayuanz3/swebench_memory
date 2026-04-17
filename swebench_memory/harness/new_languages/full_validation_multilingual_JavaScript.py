#!/usr/bin/env python3
"""
SWE-bench JavaScript Instance Validation Script

JavaScript-only version extracted from full_validation_multilingual.py.
Supports: any JavaScript/TypeScript (package.json) project.

Usage:
    python3 full_validation_multilingual_JavaScript.py instance.json [--output validated.json]
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

        if language == "javascript":
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

        return "latest"


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

class LanguageDetector:
    """Detect JavaScript from repository structure"""

    @staticmethod
    def detect(repo_dir: Path) -> str:
        """Returns 'javascript' if package.json exists, else 'unknown'"""
        if (repo_dir / "package.json").exists():
            return "javascript"
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
# MULTILINGUAL VALIDATOR (orchestrator — JavaScript only)
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
        """Detect programming language (always JavaScript for this script)"""
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
        """Create JavaScript validator"""
        # Pass self as parent so validator can use run_in_env
        self.validator = JavaScriptValidator(self.instance, self.workspace, self.repo_dir, parent=self)

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
                    accept_snapshots = False

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

                    # Rebuild JS project if solution_patch touched source files (dist/ may be stale)
                    if solution_patch and isinstance(self.validator, JavaScriptValidator):
                        self._clean_build_cache()

                    print(f"      → Running tests (after fix)...")
                    results_after = self.validator.run_tests(test_files=test_files, debug=False)

                    filter_set = None  # No filtering needed

                # STRATEGY 2 & 3: Standard compatibility check
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

                            # If baseline was 0 (targeted test didn't exist in baseline),
                            # run broader suite for PASS_TO_PASS while repo is still clean
                            _broader_ws_pkg = None
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

                            accept_snapshots = False

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

                            # results_before = with_test_patch (tests should fail)
                            results_before = with_test_patch

                            # Apply solution patch
                            print(f"      → Applying solution patch...")
                            self.validator.apply_patch(solution_patch, "solution_patch")

                            # Reinstall dependencies if package.json was modified (JS projects).
                            # Use the validator's run_command (nvm-aware) so packages are
                            # installed for the correct Node version, not the system default.
                            if isinstance(self.validator, JavaScriptValidator):
                                self.validator.run_command(['npm', 'install'], timeout=300)

                            # Clean build cache and rebuild if needed (for C/C++ projects)
                            self._clean_build_cache()

                            # results_after (tests should pass)
                            accept_snapshots = False

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
        """Clean build cache to force recompilation (for JavaScript)"""
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
        description="SWE-bench JavaScript instance validation with version detection"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
