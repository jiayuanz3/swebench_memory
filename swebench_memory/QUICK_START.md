# Quick Start Guide

## What is swebench_simple?

A simplified 2-layer Docker evaluation system that:
- ✅ Follows `full_validation.py` workflow **exactly**
- ✅ Uses conda environments like `full_validation.py`
- ✅ Runs pytest directly like `full_validation.py`
- ✅ Has Docker isolation + speed benefits
- ✅ Much simpler than `swebench_smart` (no 3-layer complexity)

## Quick Start (3 Commands)

```bash
# 1. Build base image (ONCE - reusable for all instances)
python3.11 -m swebench_simple.harness.build_base

# 2. Build instance image
python3.11 -m swebench_simple.harness.build_instance \
    --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json

# 3. Run evaluation
python3.11 -m swebench_simple.harness.run_evaluation \
    --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json \
    --predictions_path cases/sympy__sympy-9123/sympy__sympy-9123_GT_pred.json \
    --run_id sympy_9123_simple
```

## What Makes It Different?

### vs full_validation.py
- **Same**: Uses conda, pytest direct, same dependency detection
- **Different**: Runs in Docker (isolation), images are shareable

### vs swebench_smart
- **Simpler**: 2 layers instead of 3
- **More reliable**: Follows full_validation.py exactly
- **Easier to debug**: Less complexity

## Architecture

```
Base Image (sweb.simple.base:latest)
├── Ubuntu 22.04
├── Miniconda
└── Build tools

Instance Image (sweb.simple.sympy.sympy-9123:latest)
├── FROM base image ↑
├── conda create -n testbed python=3.6
├── git clone sympy
├── git checkout base_commit
├── pip install -e .
└── pip install pytest
```

## Why Use This?

- full_validation.py works but swebench_smart fails → Use this!
- Want Docker isolation + full_validation.py reliability → Use this!
- Need simple, understandable evaluation system → Use this!

## File Structure

```
swebench_simple/
├── README.md              # Full documentation
├── QUICK_START.md         # This file
├── harness/
│   ├── build_base.py      # Build base image
│   ├── build_instance.py  # Build instance images
│   └── run_evaluation.py  # Run evaluation
└── templates/
    ├── Dockerfile.base    # Base image template
    └── Dockerfile.instance # Instance image template
```

## Tips

1. **Base image is reusable** - Build once, use for all instances
2. **Force rebuild** - Use `--force-rebuild` to rebuild existing images
3. **Share images** - Push to Docker registry for teammates
4. **Check logs** - Images show what command failed during build
