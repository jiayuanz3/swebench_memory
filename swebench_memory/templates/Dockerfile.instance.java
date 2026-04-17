# SWE-bench Simple: Java Instance Image
# Mirrors full_validation_multilingual_java.py setup logic.
# Uses --override-channels -c conda-forge to avoid Anaconda ToS channel errors
# (same pattern as Dockerfile.instance for Python).

FROM sweb.simple.base:latest

ARG JAVA_VERSION=21

# Install JDK via conda-forge only (--override-channels avoids pkgs/main ToS error).
# Install build tools separately so a single tool failure doesn't abort everything.
RUN conda install -y --override-channels -c conda-forge openjdk=${JAVA_VERSION} && \
    conda clean -afy
RUN conda install -y --override-channels -c conda-forge maven && conda clean -afy || true
RUN conda install -y --override-channels -c conda-forge gradle && conda clean -afy || true
RUN conda install -y --override-channels -c conda-forge ant   && conda clean -afy || true

# Verify Java is available
RUN java -version

# Clone repository
ARG REPO_URL
ARG REPO_NAME
RUN git clone ${REPO_URL} /testbed
WORKDIR /testbed

# Checkout base commit
ARG BASE_COMMIT
RUN git checkout ${BASE_COMMIT}

ARG CREATED_AT=""

# Patch pom.xml files: Java 17 dropped support for --source 1.6 / 1.7.
# Replace source/target < 8 with 8 so the compiler plugin can run.
# This mirrors _fix_pom_xml_for_modern_jdk() in full_validation_multilingual_java.py.
RUN find /testbed -name 'pom.xml' | xargs -r sed -i \
    -e 's|<source>1\.[4-7]</source>|<source>9</source>|g' \
    -e 's|<target>1\.[4-7]</target>|<target>9</target>|g' \
    -e 's|<source>[4-7]</source>|<source>9</source>|g' \
    -e 's|<target>[4-7]</target>|<target>9</target>|g'

# Build the project. Priority mirrors install_dependencies() in the Java validator:
#   1. pom.xml  → Maven (mvn install -DskipTests + compat flags; fall back to test-compile)
#   2. build.xml → Ant  (try dist / compile / build targets in order)
#   3. otherwise → Gradle (prefer ./gradlew, fall back to gradle)
# All steps are best-effort (|| true) — patches applied at eval time may be required.
RUN if [ -f "pom.xml" ]; then \
        echo "→ Building with Maven..."; \
        mvn install -DskipTests \
            -Dmaven.javadoc.skip=true \
            -Denforcer.skip=true \
            -Dproguard.skip=true \
            2>/dev/null || \
        mvn test-compile \
            -Dmaven.javadoc.skip=true \
            -Denforcer.skip=true \
            -Dproguard.skip=true \
            2>/dev/null || true; \
    elif [ -f "build.xml" ]; then \
        echo "→ Building with Ant..."; \
        ant dist    2>/dev/null || \
        ant compile 2>/dev/null || \
        ant build   2>/dev/null || \
        ant         2>/dev/null || true; \
    else \
        echo "→ Building with Gradle..."; \
        GRADLE_CMD="gradle"; \
        if [ -f "gradlew" ]; then chmod +x gradlew && GRADLE_CMD="./gradlew"; fi; \
        $GRADLE_CMD dependencies --no-daemon 2>/dev/null || true; \
        $GRADLE_CMD compileTestJava --no-daemon 2>/dev/null || true; \
    fi

CMD ["/bin/bash"]
