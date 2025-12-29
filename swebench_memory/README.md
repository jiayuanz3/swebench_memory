# SWE-bench Simple: Docker Evaluation Following full_validation.py

This is a simplified Docker-based evaluation system that follows `full_validation.py` workflow exactly.

## Architecture

**2-Layer Docker System:**

```
┌─────────────────────────────────────────────────┐
│ Layer 1: BASE IMAGE (sweb.simple.base:latest)  │
│ - Ubuntu 22.04                                   │
│ - Miniconda                                      │
│ - Git, build tools                               │
│ - Reusable across ALL instances ✅              │
├─────────────────────────────────────────────────┤
│ Layer 2: INSTANCE IMAGE (per instance)         │
│ Following full_validation.py exactly:            │
│ 1. conda create -n testbed python=X.X            │
│ 2. git clone {repo}                              │
│ 3. git checkout {base_commit}                    │
│ 4. pip install -e .                              │
│ 5. pip install pytest                            │
└─────────────────────────────────────────────────┘
```

## Key Features

- ✅ **Simple**: Only 2 layers instead of complex 3-layer system
- ✅ **Reliable**: Follows full_validation.py workflow exactly
- ✅ **Fast**: Base layer reused across all instances
- ✅ **Isolated**: Clean Docker environment per instance
- ✅ **Shareable**: Push/pull images to registries

## Usage

### Step 1: Build Base Image (Once)

```bash
python3.11 -m swebench_simple.harness.build_base
```

This builds the base image with Ubuntu + Miniconda. Only needs to be run once.

### Step 2: Build Instance Image

```bash
python3.11 -m swebench_simple.harness.build_instance \
    --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json \
    --force-rebuild
```

This:
- Clones the repo to detect Python version
- Builds a Docker image following full_validation.py workflow
- Tags as `sweb.simple.sympy.sympy-9123:latest`

### Step 3: Run Evaluation

```bash
python3.11 -m swebench_simple.harness.run_evaluation \
    --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json \
    --predictions_path cases/sympy__sympy-9123/sympy__sympy-9123_GT_pred.json \
    --run_id sympy_9123_gt
```

This:
- Runs tests before patch (identifies PASS_TO_PASS tests)
- Applies the patch
- Runs tests after patch (identifies FAIL_TO_PASS tests)
- Generates report: `GT.sympy_9123_gt.json`

## Comparison with Other Systems

| Feature | full_validation.py | swebench_smart | **swebench_simple** |
|---------|-------------------|----------------|---------------------|
| Environment | Local conda | Docker (3 layers) | **Docker (2 layers)** |
| Setup | Conda on host | Complex build | **Simple build** |
| Test execution | pytest direct | pytest direct | **pytest direct** |
| Reliability | ✅ High | ⚠️ Medium | **✅ High** |
| Speed (first run) | Fast | Slow (complex build) | **Medium** |
| Speed (rerun) | Fast | Fast (cached) | **Fast (cached)** |
| Isolation | ❌ No | ✅ Yes | **✅ Yes** |
| Complexity | Low | High | **Low** |

## Example: Complete Workflow

```bash
# 1. Build base image (once)
python3.11 -m swebench_simple.harness.build_base

# 2. Build instance
python3.11 -m swebench_simple.harness.build_instance \
    --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json

# 3. Run evaluation
python3.11 -m swebench_simple.harness.run_evaluation \
    --dataset_name cases/sympy__sympy-9123/sympy__sympy-9123.json \
    --predictions_path cases/sympy__sympy-9123/sympy__sympy-9123_GT_pred.json \
    --run_id sympy_9123_gt

# 4. Check results
cat GT.sympy_9123_gt.json
```

## What Makes This "Simple"

1. **2 layers instead of 3**: Just base + instance
2. **No complex pre-install logic**: Follows full_validation.py exactly
3. **No conda vs system Python detection**: Always uses conda like full_validation.py
4. **No special environment image**: Each instance has its own dependencies
5. **Clear workflow**: Clone → Detect → Build → Evaluate

## Advantages Over swebench_smart

- **More reliable**: Follows proven full_validation.py workflow
- **Easier to debug**: Simpler architecture
- **Easier to modify**: Less code to understand
- **Less likely to break**: Fewer moving parts

## When to Use This

Use `swebench_simple` when:
- You want full_validation.py reliability with Docker isolation
- You're having issues with swebench_smart
- You want a simple, understandable evaluation system
- You're evaluating a small-medium number of instances

Use `swebench_smart` when:
- You need maximum speed for large-scale evaluation
- You're comfortable with complex 3-layer system
- You have hundreds of instances to evaluate

## Technical Details

### Python Version Detection

Follows full_validation.py logic (lines 276-334):
1. Check `.python-version`
2. Check `pyproject.toml`
3. Check `setup.py`
4. Check `setup.cfg`
5. Check `tox.ini`
6. Fallback to date-based detection

### Dependency Detection

Follows full_validation.py RepoConfig class (lines 33-91):
- SymPy: wheel
- Scikit-learn: numpy, scipy, cython
- Matplotlib: numpy, pillow, etc.
- Django: pytz, sqlparse, asgiref
- And more...

### Test Execution

Uses exact same pytest flags as full_validation.py (line 1180):
```bash
python -m pytest {test_file} -v --tb=short --no-header --color=no
```

With fallback for old pytest:
```bash
python -m pytest {test_file} -v --tb=short --color=no
```
