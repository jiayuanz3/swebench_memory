# SWE-bench Simple: JavaScript/Node.js Instance Image
# Mirrors full_validation_multilingual_JavaScript.py setup logic.
# Installs the requested Node.js version via conda-forge, then runs npm/yarn/pnpm install.

FROM sweb.simple.base:latest

ARG NODE_VERSION=18

# Install Node.js via conda-forge only (--override-channels avoids pkgs/main ToS error)
RUN conda install -y --override-channels -c conda-forge nodejs=${NODE_VERSION} && \
    conda clean -afy

# Install Chrome/Chromium for Karma browser tests (AMD64 only; ARM64 uses runtime fallback)
RUN ARCH=$(dpkg --print-architecture 2>/dev/null) && \
    if [ "$ARCH" = "amd64" ]; then \
        apt-get update -qq && \
        apt-get install -y --no-install-recommends wget gnupg2 && \
        wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
            | gpg --dearmor > /usr/share/keyrings/google-chrome.gpg && \
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list && \
        apt-get update -qq && \
        apt-get install -y --no-install-recommends google-chrome-stable && \
        rm -f /etc/apt/sources.list.d/google-chrome.list && \
        rm -rf /var/lib/apt/lists/*; \
    fi

ENV CHROME_BIN=/usr/bin/google-chrome

# Disable npm telemetry / audit noise (mirrors _setup_npm_isolation)
ENV npm_config_audit=false
ENV npm_config_fund=false
ENV npm_config_update_notifier=false

# Clone repository
ARG REPO_URL
ARG REPO_NAME
RUN git clone ${REPO_URL} /testbed
WORKDIR /testbed

# Checkout base commit
ARG BASE_COMMIT
RUN git checkout ${BASE_COMMIT}

ARG CREATED_AT=""

# Auto-detect package manager from lockfile (mirrors JavaScriptValidator logic):
#   yarn.lock       → yarn
#   pnpm-lock.yaml  → pnpm
#   otherwise       → npm
RUN if [ -f "yarn.lock" ]; then \
        (npm install -g yarn && yarn install 2>/dev/null) || true; \
    elif [ -f "pnpm-lock.yaml" ]; then \
        (npm install -g pnpm && pnpm install 2>/dev/null) || true; \
    elif [ -f "package.json" ]; then \
        npm install 2>/dev/null || true; \
    fi

# Run build script if present and not the test script (mirrors JS validator)
RUN if [ -f "package.json" ] && \
       node -e "const p=require('./package.json'); process.exit(p.scripts&&p.scripts.build?0:1)" 2>/dev/null; then \
        npm run build 2>/dev/null || true; \
    fi

CMD ["/bin/bash"]
