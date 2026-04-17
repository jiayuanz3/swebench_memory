#!/usr/bin/env python3
"""
Build the base Docker image (Ubuntu + Miniconda)
This image is reusable across ALL instances

Usage:
    python -m swebench_memory.harness.build_base
"""

import argparse
import subprocess
import sys
from pathlib import Path


BASE_IMAGE_TAG = "sweb.simple.base:latest"


def build_base_image(force_rebuild: bool = False, arch: str = "x86_64") -> bool:
    """
    Build the base Docker image

    Args:
        force_rebuild: If True, rebuild even if image exists
        arch: Target architecture ("x86_64" or "arm64"), default "x86_64"

    Returns:
        True if successful, False otherwise
    """
    print("=" * 60)
    print("Building SWE-bench Memory Base Image")
    print("=" * 60)

    # Check if image already exists
    if not force_rebuild:
        result = subprocess.run(
            ["docker", "images", "-q", BASE_IMAGE_TAG],
            capture_output=True,
            text=True
        )
        if result.stdout.strip():
            print(f"✓ Base image '{BASE_IMAGE_TAG}' already exists")
            print(f"  Use --force-rebuild to rebuild")
            return True

    # Get Dockerfile path
    script_dir = Path(__file__).parent.parent
    dockerfile_path = script_dir / "templates" / "Dockerfile.base"

    if not dockerfile_path.exists():
        print(f"✗ Dockerfile not found: {dockerfile_path}")
        return False

    print(f"Building base image from: {dockerfile_path}")
    print(f"This may take 5-10 minutes on first build...")
    print()

    # Build the image
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
            "-t", BASE_IMAGE_TAG,
            str(script_dir / "templates")
        ],
        text=True
    )

    if result.returncode != 0:
        print(f"✗ Failed to build base image")
        return False

    print()
    print("=" * 60)
    print(f"✓ Base image built successfully: {BASE_IMAGE_TAG}")
    print("=" * 60)
    print()
    print("This image is now reusable for all instance builds!")
    print(f"Image size:")
    subprocess.run(["docker", "images", BASE_IMAGE_TAG])

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Build SWE-bench Memory base Docker image"
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild even if image exists"
    )
    parser.add_argument(
        "--arch",
        default="x86_64",
        choices=["x86_64", "arm64"],
        help="Target architecture (default: x86_64)"
    )

    args = parser.parse_args()

    success = build_base_image(force_rebuild=args.force_rebuild, arch=args.arch)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
