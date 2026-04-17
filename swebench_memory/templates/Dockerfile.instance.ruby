# SWE-bench Simple: Ruby Instance Image
# Mirrors full_validation_multilingual_ruby.py setup logic.
# Installs the requested Ruby version via rbenv + ruby-build, then runs bundle install.

FROM sweb.simple.base:latest

ARG RUBY_VERSION=3.0

# Install build dependencies required by ruby-build
RUN apt-get update && apt-get install -y \
    libssl-dev \
    libreadline-dev \
    zlib1g-dev \
    libyaml-dev \
    libffi-dev \
    libgdbm-dev \
    libncurses5-dev \
    libgmp-dev \
    && rm -rf /var/lib/apt/lists/*

# Install rbenv and ruby-build (mirrors rbenv/ruby-build approach in validator)
# Remove .git from ruby-build to prevent rbenv from auto-pulling updates during install
RUN git clone https://github.com/rbenv/rbenv.git /root/.rbenv && \
    git clone https://github.com/rbenv/ruby-build.git /root/.rbenv/plugins/ruby-build && \
    rm -rf /root/.rbenv/plugins/ruby-build/.git

ENV PATH="/root/.rbenv/bin:/root/.rbenv/shims:$PATH"
RUN echo 'eval "$(rbenv init -)"' >> /root/.bashrc

# Install required Ruby version; resolve partial versions (e.g. "3.0" -> "3.0.7")
RUN DOTS=$(echo "${RUBY_VERSION}" | tr -cd '.' | wc -c | tr -d ' ') && \
    if [ "$DOTS" -lt 2 ]; then \
        FULL=$(rbenv install --list-all 2>/dev/null | grep -E "^[[:space:]]*${RUBY_VERSION}\.[0-9]+[[:space:]]*$" | grep -v -- '-' | tail -1 | tr -d '[:space:]'); \
        if [ -z "$FULL" ]; then \
            echo "ERROR: cannot resolve Ruby version ${RUBY_VERSION}" >&2 && exit 1; \
        fi; \
        rbenv install -s "$FULL" && rbenv global "$FULL"; \
    else \
        rbenv install -s "${RUBY_VERSION}" && rbenv global "${RUBY_VERSION}"; \
    fi

# Install bundler and rake; pin bundler to 2.4.22 for Ruby < 3.2 (latest bundler requires >= 3.2)
RUN ruby_major=$(ruby -e 'puts RUBY_VERSION.split(".")[0].to_i') && \
    ruby_minor=$(ruby -e 'puts RUBY_VERSION.split(".")[1].to_i') && \
    if [ "$ruby_major" -lt 3 ] || { [ "$ruby_major" -eq 3 ] && [ "$ruby_minor" -lt 2 ]; }; then \
        gem install bundler:2.4.22 rake --no-document; \
    else \
        gem install bundler rake --no-document; \
    fi

# Clone repository
ARG REPO_URL
ARG REPO_NAME
RUN git clone ${REPO_URL} /testbed
WORKDIR /testbed

# Checkout base commit
ARG BASE_COMMIT
RUN git checkout ${BASE_COMMIT}

ARG CREATED_AT=""

# Install gem dependencies (mirrors bundle install --path vendor/bundle)
RUN if [ -f "Gemfile" ]; then \
        bundle install --path vendor/bundle 2>/dev/null || \
        bundle install 2>/dev/null || true; \
    fi

CMD ["/bin/bash"]
