#!/usr/bin/env python3
"""
SWE-bench Multilingual Instance Validation Script - Java Only

Simplified version of full_validation_multilingual.py that supports Java only.

Usage:
    python3 full_validation_multilingual_java.py instance.json [--output validated.json]
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
    """Detect required Java version from project files and dates"""

    @staticmethod
    def parse_version(version_str: str) -> Tuple[int, ...]:
        """Parse version string '17' -> (17,)"""
        try:
            return tuple(int(x) for x in version_str.split('.'))
        except (ValueError, AttributeError):
            return (0,)

    @staticmethod
    def get_version_from_date(created_at: str, language: str) -> str:
        """Determine appropriate Java version based on creation date"""
        try:
            year = int(created_at.split('-')[0])
            month = int(created_at.split('-')[1])
        except (ValueError, IndexError):
            year = 2020

        if language == "java":
            if year < 2018:
                return "8"
            elif year < 2021:
                return "11"
            elif year < 2024:
                return "17"
            else:
                return "21"

        return "17"


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

class LanguageDetector:
    """Detect programming language from repository structure"""

    @staticmethod
    def detect(repo_dir: Path) -> str:
        """Detect language - returns 'java' or 'unknown'"""
        # Java - pom.xml, build.gradle, or build.xml (Apache Ant)
        if (repo_dir / "pom.xml").exists() or \
           (repo_dir / "build.gradle").exists() or \
           (repo_dir / "build.gradle.kts").exists() or \
           (repo_dir / "build.xml").exists():
            return "java"

        # Java - fallback: any .java source files in src/
        if list(repo_dir.glob("src/**/*.java")) or list(repo_dir.glob("**/src/**/*.java")):
            return "java"

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

        if "binary patch" in result.stderr and "without full index" in result.stderr:
            print(f"      ⚠ Binary files in patch cannot be applied (missing full index)")
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

        # Strategy 3: Reject (apply what we can)
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

        # Strategy 4: patch command with fuzz factor
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
        """Run command with environment"""
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

        # Check pom.xml
        for pom_candidate in [self.repo_dir / "pom.xml"] + list(self.repo_dir.glob("*/pom.xml")):
            if pom_candidate.exists():
                content = pom_candidate.read_text()
                patterns = [
                    r'<maven\.compiler\.source>([\d.]+)',
                    r'<maven\.compiler\.target>([\d.]+)',
                    r'<java\.version>([\d.]+)',
                    r'<source>([\d.]+)</source>',
                    r'<target>([\d.]+)</target>',
                ]
                for pattern in patterns:
                    match = re.search(pattern, content)
                    if match:
                        ver = self._normalize_java_version(match.group(1))
                        if ver.isdigit() and int(ver) >= 5:
                            return ver

        # Fallback to date-based
        created_at = self.instance.get('created_at', '')
        if created_at:
            return VersionDetector.get_version_from_date(created_at, "java")

        return "17"

    def _detect_maven_module(self, test_files: List[str]) -> Optional[str]:
        """
        Detect which Maven submodule to use, given the test file paths.
        e.g. 'gson/src/test/java/...' -> 'gson'  (if gson/pom.xml exists)
        Returns module path string or None if not a multi-module project.
        """
        if not test_files:
            return None
        root_pom = self.repo_dir / "pom.xml"
        if not root_pom.exists():
            return None
        root_content = root_pom.read_text()
        if '<modules>' not in root_content:
            return None

        modules: Dict[str, int] = {}
        for f in test_files:
            parts = f.split('/src/test/')
            if len(parts) == 2:
                candidate = parts[0]
                if (self.repo_dir / candidate / 'pom.xml').exists():
                    modules[candidate] = modules.get(candidate, 0) + 1
        if not modules:
            return None
        return max(modules, key=lambda k: modules[k])

    def get_actual_version(self) -> str:
        """Get current Java version"""
        env = self.env_vars if hasattr(self, 'env_vars') else None
        java_bin = "java"
        if env and env.get('JAVA_HOME'):
            java_bin = str(Path(env['JAVA_HOME']) / "bin" / "java")
        result = subprocess.run([java_bin, "-version"], capture_output=True, text=True, env=env)
        if result.returncode == 0:
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
            print(f"      → Java {current_version} satisfies requirement ({required_version}), skipping install")
        elif required_int > current_int > 0:
            print(f"      → Attempting to install Java {required_version}...")
            if not self._install_java_via_corretto(required_int):
                print(f"      ⚠ Corretto unavailable; trying Azul Zulu...")
                self._install_java_via_zulu(required_int)
        else:
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
        """Return extra Maven -D flags to keep compilation working on modern JDKs."""
        flags = [
            "-Dmaven.javadoc.skip=true",
            "-Denforcer.skip=true",
            "-Dproguard.skip=true",
        ]
        try:
            req_int = int(self.detected_version)
            actual_int = int(self.actual_version)
            MIN_SUPPORTED = 8
            if req_int < MIN_SUPPORTED and actual_int >= MIN_SUPPORTED:
                flags += [
                    f"-Dmaven.compiler.source={MIN_SUPPORTED}",
                    f"-Dmaven.compiler.target={MIN_SUPPORTED}",
                ]
        except (ValueError, TypeError, AttributeError):
            pass
        return flags

    def _fix_pom_xml_for_modern_jdk(self):
        """Patch all pom.xml files to compile with modern JDK (>= 8)."""
        OLD_VERSIONS = ['1.4', '1.5', '1.6', '1.7', '4', '5', '6', '7']
        changed_any = False

        has_module_info = bool(list(self.repo_dir.rglob('module-info.java')))
        RELEASE = '11' if has_module_info else '8'

        pom_files = [self.repo_dir / 'pom.xml'] + list(self.repo_dir.glob('*/pom.xml'))
        for pom_file in pom_files:
            if not pom_file.exists():
                continue
            content = pom_file.read_text()
            original = content

            for old_ver in OLD_VERSIONS:
                if f'<source>{old_ver}</source>' in content:
                    content = content.replace(
                        f'<source>{old_ver}</source>',
                        f'<release>{RELEASE}</release>'
                    )
                    content = content.replace(f'<target>{old_ver}</target>', '')

            if has_module_info:
                for old_rel in ['5', '6', '7', '8']:
                    content = content.replace(
                        f'<release>{old_rel}</release>',
                        f'<release>{RELEASE}</release>'
                    )

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
        if (self.repo_dir / "pom.xml").exists():
            self.build_tool = "maven"
            self._fix_pom_xml_for_modern_jdk()
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
            self.build_tool = "ant"
            print(f"      → Building with Ant...")
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
            parts = test_file.split('/src/test/')
            if len(parts) == 2:
                modules.add(parts[0])
            parts = test_file.split('/src/main/')
            if len(parts) == 2:
                modules.add(parts[0])
        return list(modules)

    def _parse_junit_xml(self, xml_path: Path) -> Dict[str, str]:
        """Parse JUnit XML test results"""
        status_map = {}
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
            root = tree.getroot()

            testsuites = root.findall('.//testsuite')
            if not testsuites:
                testsuites = [root] if root.tag == 'testsuite' else []

            for testsuite in testsuites:
                for testcase in testsuite.findall('testcase'):
                    classname = testcase.get('classname', '')
                    name = testcase.get('name', '')

                    if classname:
                        test_name = f"{classname}.{name}"
                    else:
                        test_name = name

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
        """Parse JUnit test results from ant stdout/stderr text output."""
        status_map = {}
        current_class = None

        for line in output.split('\n'):
            stripped = re.sub(r'^\s*\[\w+\]\s*', '', line).strip()
            if not stripped:
                continue

            # Format 1: SimpleTestFormatter
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

            # Format 2a: "Running <ClassName>" header
            m = re.match(r'Running\s+([\w.$]+)', stripped)
            if m:
                current_class = m.group(1)
                continue

            # Format 2b: "Tests run: X, Failures: Y, Errors: Z" summary
            m = re.match(
                r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)',
                stripped
            )
            if m and current_class:
                total = int(m.group(1))
                failures = int(m.group(2))
                errors = int(m.group(3))
                passed = total - failures - errors
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
        Extract specific test methods modified in test_patch for Java.
        Returns: {'org.apache.lucene.queries.TestClass.testMethod', ...}
        """
        modified_tests = set()
        test_patch = self.instance.get('test_patch', '')

        file_sections = re.split(r'diff --git a/(.*?) b/', test_patch)

        i = 1
        while i < len(file_sections) - 1:
            file_path = file_sections[i].strip()
            patch_content = file_sections[i + 1]

            if 'test' in file_path.lower() and file_path.endswith('.java'):
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

                    test_match = re.search(r'public\s+void\s+(test\w+)\s*\(', line_content)
                    if test_match:
                        current_test = test_match.group(1)

                    is_change = (line.startswith(('+', '-')) and
                                line[1:].strip() and
                                not line.startswith('+++') and
                                not line.startswith('---') and
                                not line[1:].strip().startswith('//'))

                    if current_test and is_change:
                        tests_with_changes.add(current_test)

                for test_name in tests_with_changes:
                    modified_tests.add(f"{class_path}.{test_name}")

            i += 2

        return modified_tests

    def _is_lombok_style_project(self) -> bool:
        """Return True for Lombok-style projects."""
        return (self.repo_dir / "buildScripts" / "tests.ant.xml").exists()

    def _detect_required_test_java_version(self) -> Optional[int]:
        """Parse '// version X:' from test_patch to find required Java version."""
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
        """Install a specific Java version."""
        if not hasattr(self, '_java_install_cache'):
            self._java_install_cache: dict = {}
        if version in self._java_install_cache:
            return self._java_install_cache[version]

        current = self.get_actual_version()
        if current.isdigit() and int(current) == version:
            self._java_install_cache[version] = True
            return True

        print(f"      → Installing Java {version} for version-specific tests...")

        # 1. conda-forge
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

        # 2. Amazon Corretto
        if self._install_java_via_corretto(version):
            self._java_install_cache[version] = True
            return True

        print(f"      ⚠ Corretto unavailable; trying Azul Zulu...")

        # 3. Azul Zulu
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
        """Download Amazon Corretto and wire it into self.env_vars."""
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
        """Download Azul Zulu JDK via the Azul metadata API."""
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
        """Find JAVA_HOME inside an extracted JDK archive."""
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
        """Return a prioritised list of ant test targets."""
        generic = ["test", "junit", "tests", "check"]
        current_targets = ["test.javacCurrent"]

        java_home = self.env_vars.get('JAVA_HOME', '')
        host_is_arm = platform.machine().lower() in ('arm64', 'aarch64')
        jdk_is_x86 = any(s in java_home for s in ('x86_64', 'x64', 'amd64'))
        rosetta = host_is_arm and jdk_is_x86

        if required_version:
            versioned = [f"test.javac{required_version}"]
            for delta in [1, -1, 2, -2]:
                versioned.append(f"test.javac{required_version + delta}")
            if rosetta:
                print(f"      → Rosetta detected: using test.javacCurrent before versioned targets")
                return current_targets + versioned + generic
            return versioned + current_targets + generic

        return current_targets + ["test.javac17", "test.javac16", "test.javac14",
                                   "test.javac11"] + generic

    def run_tests(self, test_files: List[str] = None, debug: bool = False, accept_snapshots: bool = False) -> Dict[str, str]:
        """Run Java tests and parse XML results"""
        status_map = {}

        if self.build_tool == "maven":
            maven_module = self._detect_maven_module(test_files) if test_files else None
            if maven_module:
                print(f"      → Using Maven module: {maven_module}")

            compat = self._maven_compat_flags()
            cmd = ["mvn", "test"] + compat
            if maven_module:
                cmd = ["mvn", "test", "-pl", maven_module, "--also-make"] + compat
            if debug:
                cmd.append("-X")
            result = self.run_command(cmd, timeout=900)

            surefire_dirs = list(self.repo_dir.rglob("target/surefire-reports"))
            for surefire_dir in surefire_dirs:
                xml_files = list(surefire_dir.glob("TEST-*.xml"))
                for xml_file in xml_files:
                    results = self._parse_junit_xml(xml_file)
                    status_map.update(results)

            build_failed = result.returncode != 0 and not status_map
            if debug or build_failed:
                print(f"      [DEBUG] Maven test command: {' '.join(cmd)}")
                print(f"      [DEBUG] Return code: {result.returncode}")
                print(f"      [DEBUG] Output (last 500 chars): {result.stdout[-500:]}")
                if debug:
                    print(f"      [DEBUG] Found {len(surefire_dirs)} surefire-reports directories")

        elif self.build_tool == "ant":
            required_test_version = None
            if self._is_lombok_style_project():
                required_test_version = self._detect_required_test_java_version()
                if required_test_version:
                    print(f"      → Test files require Java {required_test_version}")
                    self._install_java_version_only(required_test_version)

            ant_test_targets = self._build_ant_test_targets(required_test_version)
            ran = False
            result = None
            java_home_flag: List[str] = []
            if required_test_version and 'JAVA_HOME' in self.env_vars:
                java_home_flag = [
                    f"-Djvm.loc.{required_test_version}={self.env_vars['JAVA_HOME']}"
                ]
            for target in ant_test_targets:
                ant_cmd = ["ant", target] + java_home_flag
                result = self.run_command(ant_cmd, timeout=900)
                output = result.stdout + result.stderr

                target_unusable = (
                    result.returncode == 124
                    or "does not exist" in output
                    or "Unknown target" in output
                    or "No such target" in output
                    or "[input]" in output
                )
                if target_unusable:
                    continue

                if result.returncode in (0, 1):
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
                result = self.run_command(["ant"], timeout=900)

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

            if not status_map:
                for xml_file in self.repo_dir.rglob("TEST-*.xml"):
                    results = self._parse_junit_xml(xml_file)
                    status_map.update(results)

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
            # Gradle
            gradle_cmd = "./gradlew" if (self.repo_dir / "gradlew").exists() else "gradle"

            if test_files:
                modules = self._detect_gradle_modules_from_tests(test_files)
            else:
                modules = []

            if modules:
                for module in modules:
                    module_dir = self.repo_dir / module
                    test_results_dir = module_dir / "build" / "test-results" / "test"
                    if test_results_dir.exists():
                        shutil.rmtree(test_results_dir)

                for module in modules:
                    gradle_module = ':' + module.replace('/', ':')
                    print(f"      → Running tests in module: {gradle_module}")

                    cmd = [gradle_cmd, f"{gradle_module}:test", "--no-daemon"]
                    result = self.run_command(cmd, timeout=900)

                    module_dir = self.repo_dir / module
                    test_results_dir = module_dir / "build" / "test-results" / "test"

                    if test_results_dir.exists():
                        for xml_file in test_results_dir.glob("TEST-*.xml"):
                            results = self._parse_junit_xml(xml_file)
                            status_map.update(results)
            else:
                result = self.run_command([gradle_cmd, "test", "--no-daemon"], timeout=900)

                for xml_file in self.repo_dir.rglob("build/test-results/test/TEST-*.xml"):
                    results = self._parse_junit_xml(xml_file)
                    status_map.update(results)

        return status_map


# ============================================================================
# JAVA VALIDATOR ORCHESTRATOR
# ============================================================================

class JavaMultilingualValidator:
    """Main validator that delegates to JavaValidator"""

    def __init__(self, instance: dict, workspace: Path, keep_env: bool = False):
        self.instance = instance
        self.workspace = workspace
        self.repo_dir = workspace / "repo"
        self.keep_env = keep_env
        self.language = None
        self.validator = None
        self.env_name = f"swe_temp_{instance['instance_id'].replace('/', '_').replace('-', '_')}"

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
        if self.language != "java":
            raise RuntimeError(f"This validator only supports Java, detected: {self.language}")

    def run_in_env(self, command: List[str], cwd: Path = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run command with language-specific environment variables"""
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
        """Create Java validator"""
        self.validator = JavaValidator(self.instance, self.workspace, self.repo_dir, parent=self)

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
                        print(f"      → Before-fix build failed; using baseline for PASS_TO_PASS")
                        baseline_for_pass_to_pass = _baseline_before_test_only

                    if fix_only:
                        self.validator.apply_patch(fix_only, "fix_from_test_patch")
                    if solution_patch:
                        self.validator.apply_patch(solution_patch, "solution_patch")
                    print(f"      → Running tests (after fix)...")
                    results_after = self.validator.run_tests(test_files=test_files, debug=False)

                    filter_set = None

                else:
                    # Standard/fix-first strategy
                    print(f"      → Checking test_patch compatibility...")
                    print(f"      → Running baseline tests...")
                    baseline = self.validator.run_tests(test_files=test_files, debug=False)
                    print(f"      → Baseline: {len(baseline)} tests")

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

                    collection_broken = (len(with_test_patch) < len(baseline) * 0.5
                                        if baseline else len(with_test_patch) == 0)

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

                        baseline_for_pass_to_pass = baseline if len(baseline) > 0 else None

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

                        self._clean_build_cache()

                        print(f"      → Running tests (without solution)...")
                        results_before = self.validator.run_tests(test_files=test_files, debug=False)

                        print(f"      → Re-applying solution...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        self._clean_build_cache()

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

                    else:
                        # STANDARD STRATEGY
                        print(f"      → Using standard strategy")
                        results_before = with_test_patch

                        print(f"      → Applying solution patch...")
                        self.validator.apply_patch(solution_patch, "solution_patch")

                        self._clean_build_cache()

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

            # Step 7: Compare results
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
                        if filter_to_modified and self._test_in_filter(test, filter_to_modified):
                            fail_to_pass.append(test)
                        else:
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
        """Check if test matches any pattern in filter set"""
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

    def _create_smart_filter(self, test_files: List[str]) -> Optional[Set[str]]:
        """Create smart filter patterns from test files."""
        if not test_files:
            return None

        patterns = set()

        for file_path in test_files:
            file_name = Path(file_path).stem

            if file_name in ['test', 'tests', '__init__', 'mod']:
                continue

            if file_path.endswith('.java'):
                identifier = file_name.replace('test_', '').replace('_test', '').replace('_spec', '')
                if identifier and identifier not in ['test', 'tests']:
                    patterns.add(identifier)
                if file_name != identifier and file_name not in ['test', 'tests']:
                    patterns.add(file_name)

                if '/fixtures/' in file_path or '/test/' in file_path:
                    path_parts = file_path.split('/')
                    for i, part in enumerate(path_parts):
                        if part in ['fixtures', 'test'] and i + 1 < len(path_parts):
                            category = path_parts[i + 1]
                            if category:
                                patterns.add(category)

        # Also extract class names from test_patch content
        test_patch = self.instance.get('test_patch', '')
        if test_patch:
            for line in test_patch.split('\n'):
                clean_line = line.lstrip('+ \t')
                for match in re.finditer(r'class\s+(\w+Test|\w+Tests?)', clean_line):
                    name = match.group(1).replace('Test', '').replace('Tests', '')
                    if name:
                        patterns.add(name)

        return patterns if patterns else None

    def _test_matches_smart_filter(self, test_name: str, smart_filter: Set[str]) -> bool:
        """Check if test name matches any pattern in smart filter."""
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

    def _clean_build_cache(self):
        """Clean build cache to force recompilation"""
        if hasattr(self.validator, 'build_tool') and self.validator.build_tool == "gradle":
            gradle_cmd = "./gradlew" if (self.repo_dir / "gradlew").exists() else "gradle"
            subprocess.run(
                [gradle_cmd, "clean", "--no-daemon"],
                cwd=self.repo_dir,
                capture_output=True,
                timeout=120
            )

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
        validator = JavaMultilingualValidator(instance, workspace, keep_env)

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
            import traceback
            traceback.print_exc()
            print(f"{'='*80}")
            raise
        finally:
            validator.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench Java instance validation with version detection"
    )
    parser.add_argument("instance_path", help="Path to instance JSON file")
    parser.add_argument("--output", "-o", default=None, help="Output path (default: *_part2.json)")
    parser.add_argument("--keep-env", action="store_true", help="Keep environment for debugging")

    args = parser.parse_args()
    validate_instance(args.instance_path, args.output, args.keep_env)


if __name__ == "__main__":
    main()
