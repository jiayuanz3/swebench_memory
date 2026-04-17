# SWE-bench Simple: C Instance Image
# Mirrors full_validation_multilingual_c.py setup logic.
# gcc + make are already in the base image (build-essential).
# Adds cmake, autoconf/automake, and common C library headers.

FROM sweb.simple.base:latest

# Install additional C build tools and common libraries
# (gcc, g++, make already provided by build-essential in base image)
RUN apt-get update && apt-get install -y \
    cmake \
    autoconf \
    automake \
    libtool \
    pkg-config \
    bison \
    flex \
    libssl-dev \
    libpcre3-dev \
    libjemalloc-dev \
    libgmp-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone repository including submodules (e.g. jq uses oniguruma submodule)
ARG REPO_URL
ARG REPO_NAME
RUN git clone --recurse-submodules ${REPO_URL} /testbed
WORKDIR /testbed

# Checkout base commit and re-sync submodules
ARG BASE_COMMIT
RUN git checkout ${BASE_COMMIT} && \
    git submodule update --init --recursive 2>/dev/null || true

ARG CREATED_AT=""

# Build project. Priority order mirrors CValidator.install_dependencies():
#   1. autogen.sh   → run it to generate configure
#   2. configure.ac → autoreconf -i to generate configure
#   3. configure    → ./configure (with --disable-maintainer-mode)
#   4. Makefile     → make
# All steps are best-effort (|| true) to keep the image buildable even if
# compilation requires patches not yet applied.
RUN if [ -f "autogen.sh" ]; then \
        chmod +x autogen.sh && ./autogen.sh 2>/dev/null || true; \
    elif [ ! -f "configure" ] && [ -f "configure.ac" ]; then \
        autoreconf -i 2>/dev/null || true; \
    fi && \
    if [ -f "configure" ]; then \
        chmod +x configure && \
        ./configure --disable-maintainer-mode 2>/dev/null || \
        ./configure 2>/dev/null || true; \
    fi && \
    if [ -f "Makefile" ]; then \
        make -j$(nproc) 2>/dev/null || make 2>/dev/null || true; \
    elif [ -f "CMakeLists.txt" ]; then \
        mkdir -p build && cd build && \
        cmake .. -DCMAKE_BUILD_TYPE=Release 2>/dev/null && \
        make -j$(nproc) 2>/dev/null || true; \
    fi

CMD ["/bin/bash"]
