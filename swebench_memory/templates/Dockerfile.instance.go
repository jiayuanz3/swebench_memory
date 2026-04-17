# SWE-bench Simple: Go Instance Image
# Mirrors full_validation_multilingual_go.py setup logic.
# Downloads the official Go tarball; tries go{VERSION} then go{VERSION}.0 (Go 1.21+ naming).

FROM sweb.simple.base:latest

ARG GO_VERSION=1.21

# Download and install Go from the official release page.
# Go < 1.21 releases: go1.18.linux-amd64.tar.gz  (no trailing .0)
# Go >= 1.21 releases: go1.21.0.linux-amd64.tar.gz (explicit .0)
RUN set -e; \
    BASE_URL="https://go.dev/dl"; \
    TARBALL="go${GO_VERSION}.linux-amd64.tar.gz"; \
    wget -q "${BASE_URL}/${TARBALL}" -O /tmp/go.tar.gz 2>/dev/null || \
    wget -q "${BASE_URL}/go${GO_VERSION}.0.linux-amd64.tar.gz" -O /tmp/go.tar.gz; \
    tar -C /usr/local -xzf /tmp/go.tar.gz; \
    rm /tmp/go.tar.gz

ENV PATH="/usr/local/go/bin:$PATH"
ENV GOPATH="/go"
ENV GOROOT="/usr/local/go"
# CGO_ENABLED=0 avoids dyld / glibc issues on cross-platform builds
ENV CGO_ENABLED=0

# Clone repository
ARG REPO_URL
ARG REPO_NAME
RUN git clone ${REPO_URL} /testbed
WORKDIR /testbed

# Checkout base commit
ARG BASE_COMMIT
RUN git checkout ${BASE_COMMIT}

ARG CREATED_AT=""

# Download module dependencies (mirrors go mod download step)
RUN if [ -f "go.mod" ]; then \
        go mod download 2>/dev/null || \
        go mod tidy 2>/dev/null || true; \
    fi

# Verify compilation
RUN go build ./... 2>/dev/null || true

CMD ["/bin/bash"]
