# syntax=docker/dockerfile:1
# =============================================================================
# NoMercy Self-Hosted GitHub Actions Runner
# Mirrors the GitHub-hosted ubuntu-24.04 runner image.
# Multi-stage build — BuildKit runs download stages in parallel.
# =============================================================================

ARG UBUNTU_MIRROR=mirror.nl.leaseweb.net
ARG RUNNER_VERSION=2.333.1
ARG GO_VERSION=1.24.13
ARG CMAKE_VERSION=3.31.6
ARG GRADLE_VERSION=8.14
ARG MAVEN_VERSION=3.9.14
ARG GECKODRIVER_VERSION=0.36.0

# =============================================================================
# PARALLEL DOWNLOAD STAGES — these all run concurrently
# =============================================================================

# ── Go ──────────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-go
ARG GO_VERSION
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -C /opt -xz && \
    mkdir -p /opt/go-cache/1.23.12 && \
    curl -fsSL "https://go.dev/dl/go1.23.12.linux-amd64.tar.gz" | tar -C /opt/go-cache/1.23.12 --strip-components=1 -xz

# ── .NET SDK ────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-dotnet
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh && \
    chmod +x /tmp/dotnet-install.sh && \
    /tmp/dotnet-install.sh --channel 8.0 --install-dir /opt/dotnet && \
    /tmp/dotnet-install.sh --channel 9.0 --install-dir /opt/dotnet && \
    /tmp/dotnet-install.sh --channel 10.0 --install-dir /opt/dotnet

# ── Java 25 (Adoptium) ─────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-java25
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    mkdir -p /opt/jdk25 && \
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jdk/hotspot/normal/eclipse" \
        | tar -C /opt/jdk25 --strip-components=1 -xz

# ── Rust ────────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-rust
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl gcc && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable

# ── CMake ───────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-cmake
ARG CMAKE_VERSION
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    mkdir -p /opt/cmake && \
    curl -fsSL "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.tar.gz" \
    | tar -C /opt/cmake --strip-components=1 -xz && \
    rm -rf /opt/cmake/man

# ── Gradle ──────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-gradle
ARG GRADLE_VERSION
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl unzip && \
    curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o /tmp/gradle.zip && \
    unzip -q /tmp/gradle.zip -d /opt

# ── Maven ───────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-maven
ARG MAVEN_VERSION
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    curl -fsSL "https://dlcdn.apache.org/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
        | tar -C /opt -xz

# ── AWS CLI v2 ──────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-aws
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl unzip && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip && \
    unzip -q /tmp/awscli.zip -d /tmp && \
    /tmp/aws/install --install-dir /opt/aws-cli --bin-dir /opt/aws-bin

# ── Packer ──────────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-packer
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl unzip && \
    curl -fsSL https://releases.hashicorp.com/packer/1.15.0/packer_1.15.0_linux_amd64.zip -o /tmp/packer.zip && \
    unzip -q /tmp/packer.zip -d /opt

# ── Geckodriver ─────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-geckodriver
ARG GECKODRIVER_VERSION
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    curl -fsSL "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-linux64.tar.gz" \
        | tar -C /opt -xz

# ── GitHub Actions runner ──────────────────────────────────────────────────
FROM ubuntu:24.04 AS stage-runner
ARG RUNNER_VERSION
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y curl && \
    mkdir -p /opt/actions-runner && cd /opt/actions-runner && \
    curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
        | tar -xz

# =============================================================================
# BASE STAGE — all apt-based installs (runs in parallel with downloads above)
# =============================================================================
FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV ImageOS=ubuntu24

WORKDIR /root

# ── Step 1: Add all third-party repos ────────────────────────────────────────
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg lsb-release software-properties-common && \
    add-apt-repository ppa:git-core/ppa -y && \
    add-apt-repository ppa:ondrej/php -y && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] \
        https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli-stable.list > /dev/null && \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] \
        https://dl.google.com/linux/chrome/deb/ stable main" \
        | tee /etc/apt/sources.list.d/google-chrome.list > /dev/null && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] \
        https://packages.microsoft.com/repos/azure-cli/ $(. /etc/os-release && echo $VERSION_CODENAME) main" \
        | tee /etc/apt/sources.list.d/azure-cli.list > /dev/null && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] \
        https://packages.microsoft.com/ubuntu/24.04/prod $(. /etc/os-release && echo $VERSION_CODENAME) main" \
        | tee /etc/apt/sources.list.d/microsoft-prod.list > /dev/null && \
    rm -rf /var/lib/apt/lists/*

# ── Step 2: One single apt install ───────────────────────────────────────────
ARG UBUNTU_MIRROR
RUN sed -i "s|http://archive.ubuntu.com|http://${UBUNTU_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get upgrade -y && \
    apt-get install -y \
        # Core
        git git-lfs sudo openssh-client locales tzdata apt-transport-https \
        # Build
        build-essential gcc g++ gfortran make autoconf automake \
        libtool bison flex pkg-config gettext ninja-build ant \
        # Compression
        zip unzip p7zip-full p7zip-rar tar zstd pigz aria2 \
        # Tools
        vim jq rsync parallel patchelf shellcheck yamllint \
        # Libs
        libssl-dev libffi-dev libcurl4-openssl-dev libxml2-dev \
        libsqlite3-dev libpq-dev libmysqlclient-dev \
        libreadline-dev libyaml-dev libgdbm-dev libncurses5-dev \
        libz-dev libbz2-dev liblzma-dev libgmp-dev \
        libgd-dev libzip-dev libonig-dev libicu-dev \
        # Media
        mediainfo imagemagick fakeroot rpm xvfb \
        # Python
        python3 python3-venv python3-dev python3-pip python3-setuptools \
        # Network
        net-tools dnsutils iproute2 iputils-ping telnet \
        # Databases
        sqlite3 postgresql-client mysql-client \
        # Docker
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin buildah podman skopeo \
        #
        # PHP 8.3 + 8.4
        php8.3 php8.3-cli php8.3-common \
        php8.3-curl php8.3-mbstring php8.3-xml php8.3-zip \
        php8.3-pgsql php8.3-sqlite3 php8.3-mysql \
        php8.3-bcmath php8.3-gd php8.3-intl php8.3-readline \
        php8.4 php8.4-cli php8.4-common \
        php8.4-curl php8.4-mbstring php8.4-xml php8.4-zip \
        php8.4-pgsql php8.4-sqlite3 php8.4-mysql \
        php8.4-bcmath php8.4-gd php8.4-intl php8.4-readline \
        # Java
        openjdk-8-jdk openjdk-11-jdk openjdk-17-jdk openjdk-21-jdk \
        # Ruby
        ruby-full \
        # Browsers
        google-chrome-stable firefox \
        # GitHub CLI
        gh \
        # Web servers
        apache2 nginx \
        # Azure CLI + PowerShell
        azure-cli powershell \
        # Wine (for Inno Setup cross-compilation)
        wine64 \
        # macOS pkg building on Linux
        libxml2-utils cpio && \
    locale-gen en_US.UTF-8 && \
    systemctl disable apache2 || true && \
    systemctl disable nginx || true && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

# ── Step 3: Non-apt tools ────────────────────────────────────────────────────
# Install Node.js directly from official tarball (no nodesource dependency)
RUN curl -fsSL https://nodejs.org/dist/v22.22.2/node-v22.22.2-linux-x64.tar.gz \
        | tar -xz -C /usr/local --strip-components=1 && \
    npm install -g yarn corepack n && corepack enable && \
    curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer && \
    # bomutils + xar for macOS .pkg building (not in Ubuntu 24.04 repos)
    cd /tmp && git clone https://github.com/hogliux/bomutils.git && \
    cd bomutils && make && make install && cd / && rm -rf /tmp/bomutils && \
    curl -fsSL https://github.com/mackyle/xar/archive/refs/heads/master.tar.gz | tar -xz -C /tmp && \
    cd /tmp/xar-master/xar && \
    sed -i 's/OpenSSL_add_all_ciphers/OPENSSL_init_crypto/' configure.ac && \
    ./autogen.sh --noconfigure && ./configure && make && make install && \
    ldconfig && cd / && rm -rf /tmp/xar-master && \
    curl -fsSL "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl && \
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash && \
    gem install fastlane --no-document && \
    pip3 install --break-system-packages pipx ansible && pipx ensurepath && \
    rm -rf /var/lib/apt/lists/*


# ── ChromeDriver (matching installed Chrome) ─────────────────────────────────
RUN CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') && \
    DRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION%%.*}") && \
    curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/${DRIVER_URL}/linux64/chromedriver-linux64.zip" \
        -o /tmp/chromedriver.zip && \
    unzip -q /tmp/chromedriver.zip -d /tmp && \
    mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chromedriver && \
    rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64

# =============================================================================
# FINAL STAGE — merge base + all parallel downloads
# =============================================================================
FROM base AS final

ARG GRADLE_VERSION=8.14
ARG MAVEN_VERSION=3.9.14

LABEL Author="Stoney_Eagle"
LABEL Email="stoney@nomercy.tv"
LABEL GitHub="https://github.com/StoneyEagle"
LABEL BaseImage="ubuntu:24.04"

# ── Go ───────────────────────────────────────────────────────────────────────
COPY --from=stage-go /opt/go /usr/local/go
COPY --from=stage-go /opt/go-cache /opt/hostedtoolcache/go
ENV GOROOT=/usr/local/go
ENV GOPATH=/root/go
ENV PATH="${PATH}:${GOROOT}/bin:${GOPATH}/bin"

# ── .NET ─────────────────────────────────────────────────────────────────────
COPY --from=stage-dotnet /opt/dotnet /usr/share/dotnet
RUN ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet
ENV DOTNET_ROOT=/usr/share/dotnet

# ── Java 25 ──────────────────────────────────────────────────────────────────
COPY --from=stage-java25 /opt/jdk25 /usr/lib/jvm/java-25-temurin-amd64
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV JAVA_HOME_8_X64=/usr/lib/jvm/java-8-openjdk-amd64
ENV JAVA_HOME_11_X64=/usr/lib/jvm/java-11-openjdk-amd64
ENV JAVA_HOME_17_X64=/usr/lib/jvm/java-17-openjdk-amd64
ENV JAVA_HOME_21_X64=/usr/lib/jvm/java-21-openjdk-amd64
ENV JAVA_HOME_25_X64=/usr/lib/jvm/java-25-temurin-amd64

# ── Rust ─────────────────────────────────────────────────────────────────────
COPY --from=stage-rust /root/.cargo /root/.cargo
COPY --from=stage-rust /root/.rustup /root/.rustup
ENV PATH="/root/.cargo/bin:${PATH}"

# ── CMake ────────────────────────────────────────────────────────────────────
COPY --from=stage-cmake /opt/cmake/bin/ /usr/local/bin/
COPY --from=stage-cmake /opt/cmake/share/ /usr/local/share/

# ── Gradle ───────────────────────────────────────────────────────────────────
COPY --from=stage-gradle /opt/gradle-${GRADLE_VERSION} /opt/gradle-${GRADLE_VERSION}
RUN ln -s /opt/gradle-${GRADLE_VERSION}/bin/gradle /usr/local/bin/gradle

# ── Maven ────────────────────────────────────────────────────────────────────
COPY --from=stage-maven /opt/apache-maven-${MAVEN_VERSION} /opt/apache-maven-${MAVEN_VERSION}
RUN ln -s /opt/apache-maven-${MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn

# ── AWS CLI ──────────────────────────────────────────────────────────────────
COPY --from=stage-aws /opt/aws-cli /usr/local/aws-cli
COPY --from=stage-aws /opt/aws-bin /usr/local/bin

# ── Packer ───────────────────────────────────────────────────────────────────
COPY --from=stage-packer /opt/packer /usr/local/bin/packer

# ── Geckodriver ──────────────────────────────────────────────────────────────
COPY --from=stage-geckodriver /opt/geckodriver /usr/local/bin/geckodriver
ENV CHROMEWEBDRIVER=/usr/local/bin
ENV GECKOWEBDRIVER=/usr/local/bin

# ── Android SDK (needs Java from base) ───────────────────────────────────────
ENV ANDROID_HOME=/usr/local/lib/android/sdk
ENV ANDROID_SDK_ROOT=${ANDROID_HOME}
ENV PATH="${PATH}:${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${ANDROID_HOME}/build-tools/35.0.0"

RUN mkdir -p "${ANDROID_HOME}/cmdline-tools" && \
    curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
        -o /tmp/cmdline-tools.zip && \
    unzip -q /tmp/cmdline-tools.zip -d /tmp/cmdline-tools-extract && \
    mv /tmp/cmdline-tools-extract/cmdline-tools "${ANDROID_HOME}/cmdline-tools/latest" && \
    rm -rf /tmp/cmdline-tools.zip /tmp/cmdline-tools-extract && \
    yes | sdkmanager --licenses > /dev/null 2>&1 && \
    sdkmanager \
        "platform-tools" \
        "platforms;android-35" \
        "platforms;android-36" \
        "build-tools;35.0.0" \
        "build-tools;36.0.0" && \
    rm -rf "${ANDROID_HOME}/.temp"

# ── GitHub Actions runner ────────────────────────────────────────────────────
COPY --from=stage-runner /opt/actions-runner /root/actions-runner
RUN chmod +x actions-runner/bin/installdependencies.sh && \
    actions-runner/bin/installdependencies.sh
ENV RUNNER_VERSION=${RUNNER_VERSION}

# ── Entrypoint ───────────────────────────────────────────────────────────────
ADD scripts/start.sh /root/start.sh
RUN chmod +x /root/start.sh

# Fix file permissions for all shells: Yarn 4 .bin/ shims use #!/bin/sh
# (dash on Ubuntu) which doesn't inherit bash umask settings.
# Wrapping both sh and bash ensures umask propagates everywhere.
RUN mv /usr/bin/bash /usr/bin/bash.real && \
    printf '#!/usr/bin/bash.real\numask 0000\nexec /usr/bin/bash.real "$@"\n' > /usr/bin/bash && \
    chmod +x /usr/bin/bash && \
    mv /usr/bin/dash /usr/bin/dash.real && \
    printf '#!/usr/bin/bash.real\numask 0000\nexec /usr/bin/dash.real "$@"\n' > /usr/bin/dash && \
    chmod +x /usr/bin/dash

ENTRYPOINT ["/root/start.sh"]
