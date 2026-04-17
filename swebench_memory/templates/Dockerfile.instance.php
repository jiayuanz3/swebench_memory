# SWE-bench Simple: PHP Instance Image
# Mirrors full_validation_multilingual_PHP.py setup logic.
# Installs the requested PHP version via ondrej/php PPA, then runs composer install.

FROM sweb.simple.base:latest

ARG PHP_VERSION=8.1

# Add ondrej/php PPA (provides PHP 7.2 – 8.3+ on Ubuntu)
RUN apt-get update && apt-get install -y software-properties-common && \
    add-apt-repository ppa:ondrej/php -y && \
    apt-get update && \
    apt-get install -y \
        php${PHP_VERSION}-cli \
        php${PHP_VERSION}-common \
        php${PHP_VERSION}-xml \
        php${PHP_VERSION}-zip \
        php${PHP_VERSION}-mbstring \
        php${PHP_VERSION}-curl \
        php${PHP_VERSION}-gmp \
        php${PHP_VERSION}-intl \
        php${PHP_VERSION}-tokenizer \
        unzip \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install Composer (mirrors PHP validation script: curl installer → /usr/local/bin/composer)
RUN curl -sS https://getcomposer.org/installer | \
    php -- --install-dir=/usr/local/bin --filename=composer --quiet

# Clone repository
ARG REPO_URL
ARG REPO_NAME
RUN git clone ${REPO_URL} /testbed
WORKDIR /testbed

# Checkout base commit
ARG BASE_COMMIT
RUN git checkout ${BASE_COMMIT}

ARG CREATED_AT=""

# Install PHP dependencies.
# Try: (1) composer install, (2) composer update, (3) ignore-platform-reqs fallback.
# Mirrors the _install_dependencies_docker / run_command composer logic.
RUN if [ -f "composer.json" ]; then \
        (composer install --no-interaction --no-progress --prefer-dist 2>/dev/null || \
         composer update --no-interaction --no-progress --prefer-dist 2>/dev/null || \
         composer install --no-interaction --no-progress --prefer-dist --ignore-platform-reqs 2>/dev/null || \
         composer update --no-interaction --no-progress --prefer-dist --ignore-platform-reqs 2>/dev/null) || true; \
    fi

CMD ["/bin/bash"]
