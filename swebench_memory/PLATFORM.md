# Platform Compatibility: swebench_simple vs full_validation.py

## Platform Differences

### full_validation.py
- **OS**: macOS (Darwin)
- **Compiler**: clang (Apple LLVM)
- **Environment**: Native macOS system

### swebench_simple
- **OS**: Ubuntu 22.04 (Linux)
- **Compiler**: gcc 11.4.0
- **Environment**: Docker containers

---

## Compiler Flag Differences

### CFLAGS for Old C Code

**full_validation.py (macOS clang):**
```python
if 'astropy' in self.repo:
    env['CFLAGS'] = '-std=gnu89 -Wno-implicit-function-declaration'

if 'scikit-learn' in self.repo or 'astropy' in self.repo:
    env['CFLAGS'] += ' -Wno-error=incompatible-function-pointer-types'
```

**swebench_simple (Linux gcc 11):**
```python
if 'scikit-learn' in self.repo or 'astropy' in self.repo:
    env['CFLAGS'] = '-Wno-error=incompatible-pointer-types'
```

**Why different:**
- gcc 11 doesn't recognize `-Wincompatible-function-pointer-types`
- gcc 11 uses `-Wincompatible-pointer-types` instead
- `-std=gnu89` not needed on Linux gcc (handled automatically)

---

## Compiler Settings

### Scikit-learn on macOS (full_validation.py)
```python
env.update({
    'SKLEARN_NO_OPENMP': '1',
    'CC': 'clang',
    'CXX': 'clang++',
})
```

### Scikit-learn on Linux (swebench_simple)
- **No special settings needed** - uses system gcc by default
- OpenMP works fine on Linux gcc (no need to disable)

---

## Verification

### Check gcc version in Docker:
```bash
docker run --rm sweb.simple.base:latest gcc --version
# Expected: gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0
```

### Verify CFLAGS work:
```bash
docker run --rm sweb.simple.base:latest bash -c \
    "echo 'int main() { return 0; }' > /tmp/test.c && \
     gcc -Wno-error=incompatible-pointer-types /tmp/test.c -o /tmp/test && \
     echo 'CFLAGS work!'"
```

### Test astropy build:
```bash
docker run --rm sweb.simple.astropy.astropy-4973:latest \
    python -c "import astropy; print(astropy.__version__)"
# Expected: 3.1.dev22336
```

---

## Summary

| Feature | full_validation.py | swebench_simple | Status |
|---------|-------------------|-----------------|--------|
| Base OS | macOS | Ubuntu 22.04 | ✅ Adapted |
| Compiler | clang | gcc 11 | ✅ Adapted |
| Python detection | ✅ | ✅ | ✅ Same |
| setuptools detection | ✅ | ✅ | ✅ Same |
| Build requirements | ✅ | ✅ | ✅ Same |
| Constraints file | ✅ | ✅ | ✅ Same |
| --no-build-isolation | ✅ | ✅ | ✅ Same |
| Infrastructure fixes | ✅ | ✅ | ✅ Same |
| CFLAGS | macOS clang | Linux gcc 11 | ✅ Adapted |

**All logic is identical - only compiler flags differ for platform compatibility!**
