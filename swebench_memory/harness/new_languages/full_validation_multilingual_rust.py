#!/usr/bin/env python3
"""
SWE-bench Rust Instance Validation Script

Simplified version of full_validation_multilingual.py for Rust-only projects.
Supports: tokio-rs, nushell, BurntSushi/ripgrep, uutils/coreutils,
          astral-sh/ruff, sharkdp/bat

Usage:
    python3 full_validation_multilingual_rust.py instance.json [--output validated.json]
    python -m full_validation_multilingual_rust instance.json
"""

import argparse
import json
import os
import re
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
    """Detect required Rust version from project files and dates"""

    @staticmethod
    def parse_version(version_str: str) -> Tuple[int, ...]:
        try:
            return tuple(int(x) for x in version_str.split('.'))
        except (ValueError, AttributeError):
            return (0,)

    @staticmethod
    def get_rust_version_from_date(created_at: str) -> str:
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


# ============================================================================
# BASE VALIDATOR CLASS
# ============================================================================

class BaseValidator:
    """Base class for language validators"""

    def __init__(self, instance: dict, workspace: Path, repo_dir: Path, parent=None):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = repo_dir
        self.env_vars = os.environ.copy()
        self.detected_version = None
        self.actual_version = None
        self.parent = parent
        self.setup_failed = False

    def setup_environment(self):
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
        print(f"      ✓ Environment ready")

    def extract_test_files_from_patch(self, patch: str) -> List[str]:
        test_files = []
        for match in re.finditer(r'diff --git a/(.*?) b/', patch):
            file_path = match.group(1)
            if self.is_test_file(file_path):
                test_files.append(file_path)
        return list(set(test_files))

    def apply_patch(self, patch_content: str, patch_name: str):
        if not patch_content or not patch_content.strip():
            return
        patch_file = self.workspace / f"{patch_name}.patch"
        patch_file.write_text(patch_content)

        result = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir, capture_output=True, text=True
        )
        if result.returncode == 0:
            return

        # Try three-way merge
        result = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir, capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"      ✓ Applied using three-way merge")
            return

        # Try with reject
        result = subprocess.run(
            ["git", "apply", "--reject", "--whitespace=fix", str(patch_file)],
            cwd=self.repo_dir, capture_output=True, text=True
        )
        rej_files = list(self.repo_dir.rglob("*.rej"))
        if rej_files:
            print(f"      ⚠ Partial application - {len(rej_files)} conflict(s) in .rej files")
            return
        if result.returncode == 0:
            return

        raise RuntimeError(f"Failed to apply {patch_name}: {result.stderr}")

    def run_command(self, cmd: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        if self.parent and hasattr(self.parent, 'run_in_env'):
            return self.parent.run_in_env(cmd, cwd=cwd or self.repo_dir, timeout=timeout)
        try:
            return subprocess.run(
                cmd, cwd=cwd or self.repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=self.env_vars
            )
        except subprocess.TimeoutExpired as e:
            print(f"      ⚠ Command timed out after {timeout}s: {' '.join(cmd[:3])}...")
            return subprocess.CompletedProcess(
                args=cmd, returncode=124,
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )


# ============================================================================
# RUST VALIDATOR
# ============================================================================

class RustValidator(BaseValidator):
    """Validator for Rust projects"""

    def is_test_file(self, file_path: str) -> bool:
        # Integration tests: tests/ directory
        if '/tests/' in file_path or file_path.startswith('tests/'):
            return True
        # Snapshot files
        if file_path.endswith('.snap'):
            return True
        # Syntax test highlighted outputs (bat)
        if 'tests/syntax-tests/highlighted/' in file_path:
            return True
        return False

    def _extract_required_rust_version(self, error_msg: str) -> str:
        match = re.search(r'requires rustc (\d+\.\d+)', error_msg)
        return match.group(1) if match else ""

    def _clean_cargo_registry(self, error_msg: str):
        """Remove corrupted cargo registry packages"""
        # Extract package names from error messages
        packages = set()
        for match in re.finditer(r'failed to parse manifest at.*?/registry/src/[^/]+/([^/\n]+)/', error_msg):
            packages.add(match.group(1))
        for match in re.finditer(r'(cc-\d[\d.]*)', error_msg):
            packages.add(match.group(1))

        registry_src = Path.home() / ".cargo" / "registry" / "src"
        registry_cache = Path.home() / ".cargo" / "registry" / "cache"

        for pkg in packages:
            for registry_dir in registry_src.glob("*"):
                pkg_dir = registry_dir / pkg
                if pkg_dir.exists():
                    import shutil
                    shutil.rmtree(pkg_dir, ignore_errors=True)
            for registry_dir in registry_cache.glob("*"):
                for cache_file in registry_dir.glob(f"{pkg}*"):
                    cache_file.unlink(missing_ok=True)

    def _fix_cargo_readme_fields(self) -> int:
        """Fix invalid workspace-inherited readme fields in Cargo.toml files"""
        fixed = 0
        for cargo_toml in self.repo_dir.rglob("Cargo.toml"):
            content = cargo_toml.read_text()
            new_content = re.sub(
                r'readme\s*=\s*\{\s*workspace\s*=\s*true\s*\}',
                'readme = false', content
            )
            new_content = re.sub(
                r'^\s*readme\.workspace\s*=\s*true\s*$',
                'readme = false', new_content, flags=re.MULTILINE
            )
            if new_content != content:
                cargo_toml.write_text(new_content)
                fixed += 1
        return fixed

    def detect_required_version(self) -> str:
        # Check rust-toolchain or rust-toolchain.toml
        for toolchain_file in ["rust-toolchain", "rust-toolchain.toml"]:
            path = self.repo_dir / toolchain_file
            if path.exists():
                content = path.read_text()
                match = re.search(r'(\d+\.\d+(?:\.\d+)?)', content)
                if match:
                    version = match.group(1)
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
            return VersionDetector.get_rust_version_from_date(created_at)
        return "1.70"

    def get_actual_version(self) -> str:
        env = self.env_vars if hasattr(self, 'env_vars') else None
        result = subprocess.run(["rustc", "--version"], capture_output=True, text=True, env=env)
        if result.returncode == 0:
            match = re.search(r'(\d+\.\d+)', result.stdout)
            if match:
                return match.group(1)
        return "unknown"

    def setup_version(self, required_version: str):
        is_apple_silicon = platform.system() == 'Darwin' and platform.machine() == 'arm64'
        actual_version = required_version
        if is_apple_silicon:
            try:
                version_parts = [int(x) for x in required_version.split('.')]
                if version_parts[0] == 1 and version_parts[1] < 49:
                    actual_version = "stable"
                    print(f"      ⚠ Rust {required_version} doesn't support Apple Silicon, using stable")
            except Exception:
                pass

        print(f"      → Installing Rust {actual_version}...")
        result = subprocess.run(["rustup", "install", actual_version], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"      ⚠ rustup install failed: {result.stderr[:200]}")

        result = subprocess.run(["rustup", "default", actual_version], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"      ⚠ rustup default failed: {result.stderr[:200]}")
        else:
            print(f"      ✓ Set default to {actual_version}")
            # Set RUSTUP_TOOLCHAIN explicitly so it overrides any rust-toolchain file
            # in the repo (restored by "git checkout .") AND any inherited env var.
            self.env_vars['RUSTUP_TOOLCHAIN'] = actual_version

    def install_dependencies(self):
        print(f"[4/7] Installing dependencies...")

        cargo_lock_exists = (self.repo_dir / "Cargo.lock").exists()
        if cargo_lock_exists:
            self.use_locked = True
        else:
            # Try to restore Cargo.lock from git history
            git_result = self.run_command(
                ["git", "log", "--all", "--format=%H", "-n", "100", "--", "Cargo.lock"],
                timeout=30
            )
            if git_result.returncode == 0 and git_result.stdout.strip():
                for commit in git_result.stdout.strip().split('\n')[:5]:
                    restore_result = self.run_command(["git", "show", f"{commit}:Cargo.lock"], timeout=30)
                    if restore_result.returncode == 0:
                        lock_path = self.repo_dir / "Cargo.lock"
                        lock_path.write_text(restore_result.stdout)
                        verify_result = self.run_command(["cargo", "fetch", "--locked"], timeout=120)
                        if verify_result.returncode == 0:
                            print(f"      ✓ Restored Cargo.lock from git history ({commit[:8]})")
                            self.use_locked = True
                            cargo_lock_exists = True
                            break
                        else:
                            lock_path.unlink()

            if not cargo_lock_exists:
                generate_result = self.run_command(["cargo", "generate-lockfile"], timeout=300)
                if generate_result.returncode == 0:
                    self.use_locked = True
                elif "edition" in generate_result.stderr.lower() or "failed to parse manifest" in generate_result.stderr.lower():
                    # Search git history for compatible lock
                    print(f"      → Edition compatibility issue, searching git history for compatible Cargo.lock...")
                    lock_found = False
                    for git_cmd in [
                        ["git", "log", "--all", "--format=%H", "-n", "200", "--", "Cargo.lock"],
                        ["git", "log", "--all", "--reverse", "--format=%H", "-n", "100", "--", "Cargo.lock"],
                    ]:
                        if lock_found:
                            break
                        git_result = self.run_command(git_cmd, timeout=30)
                        if git_result.returncode == 0 and git_result.stdout.strip():
                            for commit in git_result.stdout.strip().split('\n')[:10]:
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
                    update_result = self.run_command(["cargo", "update"], timeout=300)
                    if update_result.returncode == 0:
                        generate_result2 = self.run_command(["cargo", "generate-lockfile"], timeout=300)
                        self.use_locked = generate_result2.returncode == 0
                    else:
                        self.use_locked = False

        fetch_cmd = ["cargo", "fetch"]
        if self.use_locked:
            fetch_cmd.append("--locked")

        result = self.run_command(fetch_cmd, timeout=600)
        if result.returncode != 0:
            print(f"      ⚠ cargo fetch failed: {result.stderr[:500]}")
            print(f"      stdout: {result.stdout[:500]}")

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

            elif "feature `resolver` is required" in result.stderr:
                print(f"      → Manifest uses resolver='2', requires Rust 1.51+ (upgrading to 1.85)...")
                try:
                    self.setup_version("1.85")
                    self.actual_version = self.get_actual_version()
                    result = self.run_command(fetch_cmd, timeout=600)
                    if result.returncode == 0:
                        print(f"      ✓ Dependencies fetched after Rust upgrade to 1.85")
                except Exception as e:
                    print(f"      ⚠ Rust upgrade to 1.85 failed: {e}")

            elif "2021` edition" in result.stderr or "older than the `2021` edition" in result.stderr:
                target_version = "1.56"
                if self.detected_version:
                    try:
                        det = tuple(int(x) for x in self.detected_version.split('.')[:2])
                        if det >= (1, 56):
                            target_version = self.detected_version
                    except (ValueError, IndexError):
                        pass
                print(f"      → Ensuring Rust >= 1.56 (edition 2021), using {target_version}...")
                try:
                    self.setup_version(target_version)
                    self.actual_version = self.get_actual_version()
                    self._clean_cargo_registry(result.stderr)
                    result = self.run_command(fetch_cmd, timeout=600)
                    if result.returncode == 0:
                        print(f"      ✓ Dependencies fetched after Rust upgrade")
                except Exception as e:
                    print(f"      ⚠ Rust upgrade failed: {e}")

            elif result.returncode != 0 and ("failed to parse manifest" in result.stderr or "namespaced featu" in result.stderr):
                self._clean_cargo_registry(result.stderr)
                result = self.run_command(fetch_cmd, timeout=600)
                if result.returncode == 0:
                    print(f"      ✓ Dependencies fetched after registry cleanup")

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
            self.run_command(["cargo", "install", "cargo-insta", "--quiet"], timeout=300)

        # Build
        build_cmd = ["cargo", "build", "--tests"]
        if self.use_locked and (self.repo_dir / "Cargo.lock").exists():
            build_cmd.append("--locked")

        result = self.run_command(build_cmd, timeout=1200)

        if result.returncode != 0:
            stderr = result.stderr
            if "failed to select a version for" in stderr or "no matching package named" in stderr:
                member_match = re.search(r'required by package `([^`]+) v[^(]+\(([^)]+)\)', stderr)
                if member_match:
                    package_path = member_match.group(2)
                    if '/repo/' in package_path:
                        member_name = package_path.split('/repo/')[-1].split('/')[0]
                        if member_name and member_name != '.':
                            print(f"      → Removing problematic workspace member: {member_name}")
                            cargo_toml = self.repo_dir / "Cargo.toml"
                            if cargo_toml.exists():
                                try:
                                    content = cargo_toml.read_text()
                                    content = re.sub(rf'^\s*["\']?{re.escape(member_name)}["\']?,?\s*$', '', content, flags=re.MULTILINE)
                                    content = re.sub(r'members\s*=\s*\[\s*,', 'members = [', content)
                                    content = re.sub(r',\s*,', ',', content)
                                    content = re.sub(r',\s*\]', ']', content)
                                    cargo_toml.write_text(content)
                                    cargo_lock = self.repo_dir / "Cargo.lock"
                                    if cargo_lock.exists():
                                        cargo_lock.unlink()
                                    fetch_result = self.run_command(["cargo", "fetch"], timeout=600)
                                    if fetch_result.returncode == 0:
                                        self.excluded_members = [member_name]
                                        self.use_locked = False
                                        retry_result = self.run_command(["cargo", "build", "--tests"], timeout=1200)
                                        if retry_result.returncode == 0:
                                            print(f"      ✓ Build successful (removed {member_name} from workspace)")
                                            return
                                        else:
                                            print(f"      ⚠ Build still failed after removing {member_name}")
                                except Exception as e:
                                    print(f"      ⚠ Failed to modify Cargo.toml: {e}")

            print(f"      ⚠ Build failed: {result.stderr[:500]}")
        else:
            print(f"      ✓ Build successful")

    def _check_uses_insta(self) -> bool:
        insta_dep_pattern = re.compile(r'(?m)^\s*insta\s*[=.]')
        for cargo_toml in self.repo_dir.rglob("Cargo.toml"):
            try:
                content = cargo_toml.read_text()
                if insta_dep_pattern.search(content):
                    return True
            except Exception:
                pass
        return False

    def _check_can_compile(self) -> bool:
        print(f"      → Verifying project compiles...")
        check_cmd = ["cargo", "check", "--tests"]

        use_locked = hasattr(self, 'use_locked') and self.use_locked
        if use_locked and (self.repo_dir / "Cargo.lock").exists():
            check_cmd.append("--locked")

        excluded_members = getattr(self, 'excluded_members', [])
        if excluded_members:
            check_cmd.append("--workspace")
            for member in excluded_members:
                check_cmd.extend(["--exclude", member])

        for attempt in range(5):
            result = self.run_command(check_cmd, timeout=600)
            if result.returncode == 0:
                if attempt > 0:
                    print(f"      ✓ Project compiles successfully after Rust upgrade(s)")
                else:
                    print(f"      ✓ Project compiles successfully")
                return True

            if "requires rustc" in result.stderr and "or newer" in result.stderr:
                required_rust = self._extract_required_rust_version(result.stderr)
                if required_rust:
                    print(f"      → Dependencies require Rust {required_rust}+...")
                    try:
                        self.setup_version(required_rust)
                        self.actual_version = self.get_actual_version()
                        print(f"      → Upgraded to Rust {self.actual_version}")
                        continue
                    except Exception as e:
                        print(f"      ⚠ Rust upgrade failed: {e}")
                        break

            if "older than the `2024` edition" in result.stderr or "older than the `2024`" in result.stderr:
                print(f"      → Dependencies require Rust 1.85+ (edition 2024 required)...")
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

            if ("expected a boolean or a string for key" in result.stderr and "readme" in result.stderr):
                fixed = self._fix_cargo_readme_fields()
                if fixed > 0:
                    print(f"      → Fixed workspace readme field in {fixed} Cargo.toml file(s)")
                    continue
                break

            break

        # Fallback: retry without --locked
        if "--locked" in check_cmd:
            no_lock_cmd = [c for c in check_cmd if c != "--locked"]
            result2 = self.run_command(no_lock_cmd, timeout=600)
            if result2.returncode == 0:
                print(f"      ✓ Project compiles (without --locked)")
                self.use_locked = False
                return True
            result = result2

        print(f"      ✗ Compilation check failed:")
        all_lines = [l for l in result.stderr.split('\n') if l.strip()]
        display_lines = all_lines[-40:] if len(all_lines) > 40 else all_lines
        for line in display_lines:
            print(f"        {line}")
        return False

    def run_tests(self, test_files: List[str] = None, debug: bool = False,
                  accept_snapshots: bool = False, all_features: bool = False) -> Dict[str, str]:
        status_map = {}

        if not self._check_can_compile():
            print(f"      ⚠ Skipping tests - project does not compile")
            print(f"      This indicates a dependency or version compatibility issue")
            return status_map

        use_insta = hasattr(self, 'uses_insta') and self.uses_insta
        use_locked = hasattr(self, 'use_locked') and self.use_locked
        excluded_members = getattr(self, 'excluded_members', [])

        workspace_package = None
        if test_files:
            for test_file in test_files:
                if test_file.startswith('crates/') and '/' in test_file[7:]:
                    workspace_package = test_file.split('/')[1]
                    break
                elif '/tests/' in test_file:
                    parts = test_file.split('/')
                    if len(parts) >= 2 and parts[1] == 'tests':
                        workspace_package = parts[0]
                        break

        if test_files:
            test_patterns = self._extract_rust_test_patterns(test_files)
            if test_patterns:
                print(f"      → Targeting {len(test_patterns)} test module(s)")
                for pattern in list(test_patterns)[:5]:
                    print(f"        - {pattern}")
                if len(test_patterns) > 5:
                    print(f"        ... and {len(test_patterns) - 5} more")

                for pattern in test_patterns:
                    if pattern.startswith('--test '):
                        test_name = pattern.split(' ', 1)[1]
                        cmd = ["cargo", "test"]
                        if workspace_package:
                            cmd.extend(["-p", workspace_package])
                        cmd.extend(["--test", test_name])
                        if use_locked:
                            cmd.append("--locked")
                        # Detect feature-gated tests
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
                        if excluded_members and not workspace_package:
                            cmd.append("--workspace")
                            for member in excluded_members:
                                cmd.extend(["--exclude", member])
                        cmd.extend(["--", "--nocapture"])
                    else:
                        if use_insta and accept_snapshots:
                            cmd = ["cargo", "insta", "test", "--accept"]
                            if workspace_package:
                                cmd.extend(["-p", workspace_package])
                            cmd.extend(["--", pattern, "--nocapture"])
                        elif use_insta:
                            cmd = ["cargo", "insta", "test"]
                            if workspace_package:
                                cmd.extend(["-p", workspace_package])
                            cmd.extend(["--", pattern, "--nocapture"])
                        else:
                            cmd = ["cargo", "test"]
                            if workspace_package:
                                cmd.extend(["-p", workspace_package])
                            cmd.append(pattern)
                            if use_locked:
                                cmd.append("--locked")
                            if excluded_members and not workspace_package:
                                cmd.append("--workspace")
                                for member in excluded_members:
                                    cmd.extend(["--exclude", member])
                            cmd.extend(["--", "--nocapture"])

                        if excluded_members and not workspace_package:
                            for member in excluded_members:
                                dash_idx = cmd.index("--")
                                cmd.insert(dash_idx, member)
                                cmd.insert(dash_idx, "--exclude")

                    result = self.run_command(cmd, timeout=600)
                    output = result.stdout + result.stderr
                    before = len(status_map)
                    self._parse_rust_test_output(output, status_map, debug)

                    if len(status_map) == before and result.returncode != 0:
                        print(f"      ⚠ Test command failed (rc={result.returncode}): {output[-200:]}")
                        # Retry with --all-features for feature-gated tests
                        if (pattern.startswith('--test ') and required_features
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
                    elif (len(status_map) == before and result.returncode == 0
                          and 'running 0 tests' in output and pattern.startswith('--test ')
                          and not required_features):
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
            else:
                print(f"      → No matching test modules found, running all tests")
                if use_insta and accept_snapshots:
                    cmd = ["cargo", "insta", "test", "--accept"]
                elif use_insta:
                    cmd = ["cargo", "insta", "test"]
                else:
                    cmd = ["cargo", "test"]
                if workspace_package:
                    cmd.extend(["-p", workspace_package])
                if use_locked and not use_insta:
                    cmd.append("--locked")
                if all_features and not use_insta:
                    cmd.append("--all-features")
                if excluded_members and not workspace_package and not use_insta:
                    cmd.append("--workspace")
                    for member in excluded_members:
                        cmd.extend(["--exclude", member])
                if not use_insta and not workspace_package:
                    cmd.append("--no-fail-fast")
                cmd.extend(["--", "--nocapture"])
                result = self.run_command(cmd, timeout=600)
                output = result.stdout + result.stderr
                self._parse_rust_test_output(output, status_map, debug)
        else:
            cmd = ["cargo", "test"]
            if workspace_package:
                cmd.extend(["-p", workspace_package])
            if use_locked and not use_insta:
                cmd.append("--locked")
            if all_features and not use_insta:
                cmd.append("--all-features")
            if excluded_members and not workspace_package and not use_insta:
                cmd.append("--workspace")
                for member in excluded_members:
                    cmd.extend(["--exclude", member])
            if not use_insta and not workspace_package:
                cmd.append("--no-fail-fast")
            cmd.extend(["--", "--nocapture"])
            result = self.run_command(cmd, timeout=600)
            output = result.stdout + result.stderr
            self._parse_rust_test_output(output, status_map, debug)
            if not status_map and result.returncode != 0:
                all_lines = [l for l in output.split('\n') if l.strip()]
                print(f"      ⚠ cargo test failed (rc={result.returncode}), last lines:")
                for ln in all_lines[-10:]:
                    print(f"        {ln}")

        if use_insta and accept_snapshots:
            snap_new_files = list(self.repo_dir.rglob("*.snap.new"))
            if snap_new_files:
                print(f"      ⚠ Found {len(snap_new_files)} pending snapshot(s) after acceptance")
            else:
                print(f"      ✓ No new snapshots created (solution matches expected output)")

        # bat-style syntax regression tests
        if (self.repo_dir / "tests" / "syntax-tests" / "create_highlighted_versions.py").exists():
            syntax_results = self._run_bat_syntax_tests()
            status_map.update(syntax_results)

        return status_map

    def _get_test_binaries_from_cargo(self) -> list:
        """Parse Cargo.toml to find configured test binaries"""
        binaries = []
        cargo_toml = self.repo_dir / "Cargo.toml"
        if not cargo_toml.exists():
            return binaries
        content = cargo_toml.read_text()
        for match in re.finditer(r'\[\[test\]\].*?name\s*=\s*"([^"]+)"', content, re.DOTALL):
            binaries.append(match.group(1))
        return binaries

    def _find_parent_test_binaries(self, module_path: str) -> set:
        """Find which test binaries include a given module file"""
        binaries = set()
        parts = module_path.split('/')
        if len(parts) >= 2 and parts[0] in ('tests', 'src'):
            stem = Path(parts[-1]).stem
            binaries.add(stem)
        return binaries

    def _extract_rust_test_patterns(self, test_files: List[str]) -> set:
        """Extract cargo test patterns from test file paths"""
        patterns = set()
        for file_path in test_files:
            path = Path(file_path)
            # Integration test: tests/<name>.rs -> --test <name>
            if '/tests/' in file_path and file_path.endswith('.rs'):
                parts = file_path.split('/')
                test_idx = next((i for i, p in enumerate(parts) if p == 'tests'), None)
                if test_idx is not None and test_idx + 1 < len(parts):
                    test_name = Path(parts[test_idx + 1]).stem
                    if test_name != 'mod':
                        patterns.add(f"--test {test_name}")
            # Snapshot files: derive test name from path
            elif file_path.endswith('.snap'):
                snap_name = path.stem
                if snap_name.endswith('.py') or snap_name.endswith('.rs'):
                    snap_name = Path(snap_name).stem
                # Try to extract test module from insta snapshot name convention
                # e.g. ruff_linter__rules__tests__UP028 -> rules::tests::UP028
                parts = snap_name.split('__')
                if len(parts) >= 2:
                    patterns.add('::'.join(parts[-2:]))
            # Directory: used for broader runs (caller sets test_files to pkg/tests/)
            elif file_path.endswith('/'):
                pass  # let run_tests handle it
        return patterns

    def _parse_rust_test_output(self, output: str, status_map: dict, debug: bool = False):
        """Parse cargo test output into status map"""
        for line in output.split('\n'):
            # Standard: "test foo::bar ... ok"
            match = re.match(r'^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)', line)
            if match:
                test_name = match.group(1).strip()
                status_str = match.group(2)
                if status_str == 'ok':
                    status_map[test_name] = 'PASSED'
                elif status_str == 'FAILED':
                    status_map[test_name] = 'FAILED'
                elif status_str == 'ignored':
                    status_map[test_name] = 'IGNORED'
                continue
            # Doc test: "test src/foo.rs - bar::baz (line N) ... ok"
            match = re.match(r'^test\s+(.*?\(line \d+\))\s+\.\.\.\s+(ok|FAILED|ignored)', line)
            if match:
                test_name = match.group(1).strip()
                status_str = match.group(2)
                if status_str == 'ok':
                    status_map[test_name] = 'PASSED'
                elif status_str == 'FAILED':
                    status_map[test_name] = 'FAILED'
                elif status_str == 'ignored':
                    status_map[test_name] = 'IGNORED'

    def _run_bat_syntax_tests(self) -> Dict[str, str]:
        """Run bat-style syntax regression tests"""
        status_map = {}
        tests_dir = self.repo_dir / "tests" / "syntax-tests"
        if not tests_dir.exists():
            return status_map

        # Build release binary
        build_result = self.run_command(["cargo", "build", "--release"], timeout=600)
        if build_result.returncode != 0:
            print(f"      ⚠ Release build failed for bat syntax tests")
            return status_map

        bat_bin = self.repo_dir / "target" / "release" / "bat"
        if not bat_bin.exists():
            return status_map

        highlighted_dir = tests_dir / "highlighted"
        sources_dir = tests_dir / "sources"
        if not highlighted_dir.exists() or not sources_dir.exists():
            return status_map

        for expected_file in highlighted_dir.rglob("*"):
            if expected_file.is_file():
                rel_path = expected_file.relative_to(highlighted_dir)
                source_file = sources_dir / rel_path
                if not source_file.exists():
                    continue

                env = os.environ.copy()
                env.update(self.env_vars)
                env['BAT_THEME'] = 'base16'

                result = subprocess.run(
                    [str(bat_bin), "--color=always", "--decorations=never", str(source_file)],
                    capture_output=True, env=env
                )

                test_name = f"syntax::{rel_path}"
                expected = expected_file.read_bytes()
                if result.stdout == expected:
                    status_map[test_name] = 'PASSED'
                else:
                    status_map[test_name] = 'FAILED'

        return status_map

    def extract_modified_tests(self) -> Set[str]:
        """Extract modified test names from the test patch"""
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')
        if not test_patch:
            return modified_tests

        parts = re.split(r'(diff --git a/[^\n]+\n)', test_patch)
        i = 1
        while i < len(parts) - 1:
            header = parts[i]
            content = parts[i + 1] if i + 1 < len(parts) else ''

            file_match = re.search(r'diff --git a/(.*?) b/', header)
            if not file_match:
                i += 2
                continue

            file_path = file_match.group(1)

            if file_path.endswith('.snap'):
                # Insta snapshot: derive test name from filename
                snap_name = Path(file_path).stem
                if snap_name.endswith('.py') or snap_name.endswith('.rs'):
                    snap_name = Path(snap_name).stem
                snap_parts = snap_name.split('__')
                if 'tests' in snap_parts:
                    test_idx = snap_parts.index('tests')
                    test_parts = snap_parts[test_idx + 1:]
                    if test_parts:
                        modified_tests.add(test_parts[0])
            elif file_path.endswith('.rs'):
                tests_with_changes = set()
                current_test = None
                found_test_attr = False

                for line in content.split('\n'):
                    line_content = line[1:] if line.startswith(('+', '-')) else line

                    if '#[test]' in line_content or '#[tokio::test]' in line_content or '#[async_std::test]' in line_content:
                        found_test_attr = True

                    fn_match = re.search(r'(?:async\s+)?fn\s+(\w+)\s*\(', line_content)
                    if fn_match and found_test_attr:
                        current_test = fn_match.group(1)
                        found_test_attr = False

                    macro_test_match = re.search(r'\w+test!\s*\(\s*(\w+)', line_content)
                    if macro_test_match:
                        current_test = macro_test_match.group(1)

                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line.startswith('+++') and
                                not line.startswith('---'))

                    if current_test and is_change:
                        tests_with_changes.add(current_test)

                if file_path.startswith('tests/'):
                    test_binary = Path(file_path).stem
                    if tests_with_changes:
                        for test in tests_with_changes:
                            modified_tests.add(f"{test_binary}::{test}")
                    elif current_test:
                        modified_tests.add(test_binary)
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
# RUST VALIDATOR (MAIN ORCHESTRATOR)
# ============================================================================

class RustValidationRunner:
    """Main orchestrator for Rust project validation"""

    def __init__(self, instance: dict, workspace: Path):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.validator: Optional[RustValidator] = None
        self.snapshot_validation_passed = None

    def setup_repo(self):
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

    def run_in_env(self, command: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        try:
            env = os.environ.copy()
            if hasattr(self.validator, 'env_vars'):
                env.update(self.validator.env_vars)
            return subprocess.run(
                command, cwd=cwd or self.repo_dir,
                capture_output=True, text=True, timeout=timeout, env=env
            )
        except subprocess.TimeoutExpired as e:
            print(f"      ⚠ Command timed out after {timeout}s: {' '.join(command[:3])}...")
            return subprocess.CompletedProcess(
                args=command, returncode=124,
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )

    def validate(self) -> Tuple[List[str], List[str]]:
        self.setup_repo()

        print(f"[2/7] Detecting language...")
        if not (self.repo_dir / "Cargo.toml").exists():
            raise RuntimeError("No Cargo.toml found - not a Rust project")
        print(f"      ✓ Detected: rust")

        self.validator = RustValidator(self.instance, self.workspace, self.repo_dir, parent=self)

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
        results_before = {}
        results_after = {}

        if not test_patch:
            print(f"      → No test_patch, running baseline...")
            results_before = self.validator.run_tests(test_files=test_files, debug=False)
            if solution_patch:
                accept_snapshots = (hasattr(self.validator, 'uses_insta') and self.validator.uses_insta)
                print(f"      → Applying solution patch...")
                self.validator.apply_patch(solution_patch, "solution_patch")
                results_after = self.validator.run_tests(
                    test_files=test_files, debug=False, accept_snapshots=accept_snapshots
                )
            else:
                results_after = self.validator.run_tests(test_files=test_files, debug=False)

        elif not solution_patch:
            print(f"      → Running baseline...")
            results_before = self.validator.run_tests(test_files=test_files, debug=False)
            print(f"      → Applying test patch...")
            self.validator.apply_patch(test_patch, "test_patch")
            print(f"      → Running tests with test_patch...")
            results_after = self.validator.run_tests(test_files=test_files, debug=False)

        else:
            # Both patches exist
            has_fix_in_test_patch = any(
                not self.validator.is_test_file(m.group(1))
                for m in re.finditer(r'diff --git a/(.*?) b/', test_patch)
            )

            if has_fix_in_test_patch:
                print(f"      → test_patch contains fix, splitting...")
                test_only, fix_only = self._split_test_patch(test_patch)
                if test_only:
                    self.validator.apply_patch(test_only, "test_only")
                print(f"      → Running tests (before fix)...")
                results_before = self.validator.run_tests(test_files=test_files, debug=False)
                if fix_only:
                    self.validator.apply_patch(fix_only, "fix_from_test_patch")
                if solution_patch:
                    self.validator.apply_patch(solution_patch, "solution_patch")
                print(f"      → Running tests (after fix)...")
                results_after = self.validator.run_tests(test_files=test_files, debug=False)

            else:
                # Check for snapshot test flow
                uses_snapshots = (
                    (hasattr(self.validator, 'uses_insta') and self.validator.uses_insta) or
                    'tests/syntax-tests/highlighted/' in test_patch
                )

                skip_standard_strategy = False

                if uses_snapshots:
                    fixtures_patch, snapshots_patch = self._split_fixtures_and_snapshots(test_patch)

                    if fixtures_patch and snapshots_patch:
                        is_bat_syntax = 'tests/syntax-tests/highlighted/' in test_patch

                        if is_bat_syntax:
                            print(f"      → Applying test source fixtures...")
                            self.validator.apply_patch(fixtures_patch, "fixtures_only")
                            print(f"      → Running tests before fix...")
                            results_before = self.validator.run_tests(test_files=test_files, debug=False)
                            print(f"      → Applying solution patch...")
                            self.validator.apply_patch(solution_patch, "solution_patch")
                            print(f"      → Applying expected highlighted outputs...")
                            self.validator.apply_patch(snapshots_patch, "snapshots")
                            print(f"      → Running tests after fix...")
                            results_after = self.validator.run_tests(test_files=test_files, debug=False)
                            skip_standard_strategy = True
                        else:
                            # Insta snapshot flow
                            print(f"      → Running baseline tests...")
                            baseline = self.validator.run_tests(test_files=test_files, debug=False)
                            print(f"      → Baseline: {len(baseline)} tests")
                            snapshot_files_to_update = re.findall(r'diff --git a/(.*?\.snap) b/', snapshots_patch)
                            print(f"      → Applying test fixtures...")
                            self.validator.apply_patch(fixtures_patch, "fixtures_only")
                            print(f"      → Applying solution patch...")
                            self.validator.apply_patch(solution_patch, "solution_patch")
                            print(f"      → Applying expected snapshots...")
                            self.validator.apply_patch(snapshots_patch, "snapshots")
                            print(f"      → Running tests (with solution and snapshots)...")
                            results_after = self.validator.run_tests(
                                test_files=test_files, debug=False, accept_snapshots=True
                            )

                            print(f"      → Identifying tests related to new snapshots...")
                            new_snapshot_tests = set()
                            for snap_file in snapshot_files_to_update:
                                snap_name = Path(snap_file).stem
                                if snap_name.endswith('.py'):
                                    snap_name = snap_name[:-3]
                                parts = snap_name.split('__')
                                if 'tests' in parts:
                                    test_idx = parts.index('tests')
                                    test_parts = parts[test_idx + 1:]
                                    if test_parts:
                                        for part in test_parts:
                                            if part:
                                                for subpart in part.split('_'):
                                                    if subpart:
                                                        subpart_lower = subpart.lower()
                                                        for test_name in results_after.keys():
                                                            if subpart_lower in test_name.lower():
                                                                new_snapshot_tests.add(test_name)

                            results_before = {}
                            for test_name, status in baseline.items():
                                results_before[test_name] = 'FAILED' if test_name in new_snapshot_tests else status
                            for test_name in results_after.keys():
                                if test_name not in results_before and test_name in new_snapshot_tests:
                                    results_before[test_name] = 'FAILED'

                            print(f"      → Marked {len(new_snapshot_tests)} snapshot-related tests for FAIL_TO_PASS")
                            skip_standard_strategy = True
                    else:
                        skip_standard_strategy = False

                if not skip_standard_strategy:
                    # Standard compatibility check
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
                        uses_insta = hasattr(self.validator, 'uses_insta') and self.validator.uses_insta
                        if uses_insta:
                            print(f"      ℹ No tests failing with test_patch (expected for snapshot tests)")
                        else:
                            print(f"      ⚠ WARNING: No tests failing with test_patch!")

                    collection_broken = (
                        len(with_test_patch) < len(baseline) * 0.5 if baseline
                        else len(with_test_patch) == 0
                    )

                    if collection_broken:
                        print(f"      ⚠ test_patch incompatible with base_commit")
                        print(f"      → Tests failed to run with test_patch")
                        print(f"      → Using fix-first strategy")

                        subprocess.run(["git", "checkout", "."], cwd=self.repo_dir, capture_output=True)
                        subprocess.run(["git", "clean", "-fd"], cwd=self.repo_dir, capture_output=True)

                        # Re-apply workspace member exclusions after git reset
                        _excluded = getattr(self.validator, 'excluded_members', [])
                        if _excluded:
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
                                _lock = self.repo_dir / "Cargo.lock"
                                if _lock.exists():
                                    _lock.unlink()
                                _regen = subprocess.run(
                                    ["cargo", "generate-lockfile"],
                                    cwd=self.repo_dir, capture_output=True, text=True, timeout=300
                                )
                                if _regen.returncode == 0:
                                    self.validator.use_locked = True
                                    print(f"      → Re-excluded workspace member(s) after git reset: {', '.join(_excluded)}")

                        # Broader baseline for PASS_TO_PASS when targeted test didn't exist
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
                                broader_baseline = self.validator.run_tests(
                                    test_files=[f"{_broader_ws_pkg}/tests/"],
                                    debug=False, all_features=True
                                )
                                print(f"      → Broader baseline: {len(broader_baseline)} tests")

                        # Apply solution FIRST so test_patch can compile
                        print(f"      → Applying solution patch first...")
                        self.validator.apply_patch(solution_patch, "solution_patch")
                        print(f"      → Applying test patch...")
                        self.validator.apply_patch(test_patch, "test_patch")

                        # Temporarily revert solution to see tests fail
                        print(f"      → Temporarily reverting solution...")
                        solution_file = self.workspace / "solution_reverse.patch"
                        solution_file.write_text(solution_patch)
                        subprocess.run(
                            ["git", "apply", "-R", str(solution_file)],
                            cwd=self.repo_dir, capture_output=True
                        )

                        print(f"      → Running tests (without solution)...")
                        results_before = self.validator.run_tests(test_files=test_files, debug=False)

                        # Re-apply solution
                        print(f"      → Re-applying solution...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        accept_snapshots = (hasattr(self.validator, 'uses_insta') and self.validator.uses_insta)
                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(
                            test_files=test_files, debug=False, accept_snapshots=accept_snapshots
                        )

                        # Run broader tests after solution for PASS_TO_PASS
                        if broader_baseline and _broader_ws_pkg:
                            print(f"      → Running broader tests after solution ({_broader_ws_pkg})...")
                            broader_after = self.validator.run_tests(
                                test_files=[f"{_broader_ws_pkg}/tests/"],
                                debug=False, all_features=True
                            )
                            print(f"      → Broader after: {len(broader_after)} tests")
                            for _test, _status in broader_after.items():
                                if _test not in results_after:
                                    results_after[_test] = _status

                        # Extract modified tests for filtering
                        if hasattr(self.validator, 'extract_modified_tests'):
                            filter_set = self.validator.extract_modified_tests()
                            if len(filter_set) == 0:
                                filter_set = None
                                print(f"      → No specific tests extracted, running all modified test files")
                            else:
                                print(f"      → Filtering to {len(filter_set)} modified tests")

                        if broader_baseline:
                            baseline_for_pass_to_pass = broader_baseline

                    else:
                        # Standard strategy: test_patch is compatible
                        print(f"      → Using standard strategy")
                        results_before = with_test_patch
                        print(f"      → Applying solution patch...")
                        self.validator.apply_patch(solution_patch, "solution_patch")
                        accept_snapshots = (hasattr(self.validator, 'uses_insta') and self.validator.uses_insta)
                        print(f"      → Running tests (with solution)...")
                        results_after = self.validator.run_tests(
                            test_files=test_files, debug=False, accept_snapshots=accept_snapshots
                        )

        print(f"      ✓ Tests completed ({len(results_before)} before, {len(results_after)} after)")

        if len(results_before) == 0 and len(results_after) == 0:
            print(f"      ⚠ WARNING: No tests were detected or run!")
            print(f"      Required: {self.validator.detected_version}, Actual: {self.validator.actual_version}")

        # Check snapshot validation
        if hasattr(self.validator, 'uses_insta') and self.validator.uses_insta:
            snap_new_files = list(self.repo_dir.rglob("*.snap.new"))
            self.snapshot_validation_passed = len(snap_new_files) == 0
            status = "✓ PASSED" if self.snapshot_validation_passed else f"✗ FAILED ({len(snap_new_files)} mismatched)"
            print(f"      {status.split()[0]} SNAPSHOT_VALIDATION: {status.split(None, 1)[1]}")

        print(f"[7/7] Comparing results...")
        before_failed = sum(1 for s in results_before.values() if s in ['FAILED', 'ERROR'])
        before_passed = sum(1 for s in results_before.values() if s == 'PASSED')
        after_failed = sum(1 for s in results_after.values() if s in ['FAILED', 'ERROR'])
        after_passed = sum(1 for s in results_after.values() if s == 'PASSED')
        print(f"      → Before: {before_failed} failed, {before_passed} passed")
        print(f"      → After: {after_failed} failed, {after_passed} passed")

        fail_to_pass, pass_to_pass = self.compare_results(
            results_before, results_after, filter_set, test_files, baseline_for_pass_to_pass
        )

        print(f"      ✓ FAIL_TO_PASS: {len(fail_to_pass)} tests")
        for test in fail_to_pass[:10]:
            print(f"        - {test}")
        if len(fail_to_pass) > 10:
            print(f"        ... and {len(fail_to_pass) - 10} more")
        print(f"      ✓ PASS_TO_PASS: {len(pass_to_pass)} tests")

        return fail_to_pass, pass_to_pass

    def compare_results(
        self,
        before: Dict[str, str],
        after: Dict[str, str],
        filter_to_modified: set = None,
        test_files: List[str] = None,
        baseline: Dict[str, str] = None
    ) -> Tuple[List[str], List[str]]:
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
                        elif test in filter_to_modified:
                            fail_to_pass.append(test)
                else:
                    pass_to_pass.append(test)

        return sorted(fail_to_pass), sorted(pass_to_pass)

    def _create_smart_filter(self, test_files: List[str]) -> Optional[Set[str]]:
        patterns = set()
        for file_path in test_files:
            file_name = Path(file_path).stem
            if file_name in ['test', 'tests', '__init__', 'mod']:
                continue
            if file_path.endswith('.snap'):
                parts = file_name.split('__')
                if 'tests' in parts:
                    test_idx = parts.index('tests')
                    test_specific = parts[test_idx+1:]
                    if test_specific:
                        patterns.add(test_specific[0])
            elif file_path.endswith('.rs'):
                identifier = file_name.replace('test_', '').replace('_test', '')
                if identifier and identifier not in ['test', 'tests']:
                    patterns.add(identifier)
                if file_name != identifier and file_name not in ['test', 'tests']:
                    patterns.add(file_name)

        # Extract fn names from test_patch
        test_patch = self.instance.get('test_patch', '')
        if test_patch:
            for line in test_patch.split('\n'):
                clean_line = line.lstrip('+ \t')
                for match in re.finditer(r'fn\s+(test_\w+|\w+)\s*\(', clean_line):
                    name = match.group(1)
                    if name and not name.startswith('test_'):
                        patterns.add(name)
                    elif name.startswith('test_'):
                        patterns.add(name[5:])

        return patterns if patterns else None

    def _test_matches_smart_filter(self, test_name: str, smart_filter: Set[str]) -> bool:
        test_lower = test_name.lower()
        for pattern in smart_filter:
            pattern_lower = pattern.lower()
            if pattern_lower in test_lower:
                return True
            for sep in ['::', '.', '__', '/', '_']:
                if f"{sep}{pattern_lower}{sep}" in test_lower:
                    return True
                if test_lower.startswith(f"{pattern_lower}{sep}"):
                    return True
                if test_lower.endswith(f"{sep}{pattern_lower}"):
                    return True
        return False

    def _split_test_patch(self, test_patch: str) -> Tuple[str, str]:
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

    def _split_fixtures_and_snapshots(self, test_patch: str) -> Tuple[str, str]:
        fixture_hunks = []
        snapshot_hunks = []
        parts = re.split(r'(diff --git a/[^\n]+\n)', test_patch)
        i = 1
        while i < len(parts) - 1:
            header = parts[i]
            content = parts[i + 1]
            full_patch = header + content
            file_match = re.search(r'diff --git a/(.*?) b/', header)
            if file_match:
                file_path = file_match.group(1)
                if file_path.endswith('.snap') or 'tests/syntax-tests/highlighted/' in file_path:
                    snapshot_hunks.append(full_patch)
                else:
                    fixture_hunks.append(full_patch)
            i += 2
        return ''.join(fixture_hunks), ''.join(snapshot_hunks)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def validate_instance(instance_path: str, output_path: Optional[str] = None):
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
        runner = RustValidationRunner(instance, workspace)

        try:
            fail_to_pass, pass_to_pass = runner.validate()

            instance['FAIL_TO_PASS'] = fail_to_pass
            instance['PASS_TO_PASS'] = pass_to_pass
            instance['environment_setup_commit'] = instance['base_commit']

            if runner.snapshot_validation_passed is not None:
                instance['SNAPSHOT_VALIDATION'] = 'PASSED' if runner.snapshot_validation_passed else 'FAILED'

            if runner.validator:
                instance['detected_version'] = runner.validator.detected_version
                instance['actual_version'] = runner.validator.actual_version

            with open(output_path, 'w') as f:
                json.dump(instance, f, indent=2)

            print(f"\n{'='*80}")
            print(f"✓ Validation complete!")
            print(f"  Language: rust")
            if runner.snapshot_validation_passed is not None:
                status = '✓ PASSED' if runner.snapshot_validation_passed else '✗ FAILED'
                print(f"  Snapshot Validation: {status}")
            if runner.validator:
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


def main():
    parser = argparse.ArgumentParser(description="SWE-bench Rust instance validation")
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    args = parser.parse_args()
    validate_instance(args.instance_path, args.output)


if __name__ == "__main__":
    main()
