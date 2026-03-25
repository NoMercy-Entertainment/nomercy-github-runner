# =============================================================================
# NoMercy Self-Hosted GitHub Actions Runner
# Mirrors the GitHub-hosted ubuntu-24.04 runner image as closely as possible.
# =============================================================================
FROM ubuntu:24.04

ARG RUNNER_VERSION=2.322.0
ARG TARGETARCH=x64

ENV DEBIAN_FRONTEND=noninteractive
ENV ImageOS=ubuntu24

LABEL Author="Stoney_Eagle"
LABEL Email="stoney@nomercy.tv"
LABEL GitHub="https://github.com/StoneyEagle"
LABEL BaseImage="ubuntu:24.04"
LABEL RunnerVersion=${RUNNER_VERSION}

WORKDIR /root

# =============================================================================
# 1. Base system packages
# =============================================================================
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        # Core utilities
        ca-certificates curl wget git git-lfs gnupg sudo lsb-release \
        software-properties-common apt-transport-https \
        openssh-client locales tzdata \
        # Build essentials
        build-essential gcc g++ gfortran make autoconf automake \
        libtool bison flex pkg-config gettext \
        # Compression
        zip unzip p7zip-full p7zip-rar tar zstd pigz aria2 \
        # Text / data tools
        vim jq rsync parallel patchelf \
        shellcheck yamllint \
        # Libs commonly needed by builds
        libssl-dev libffi-dev libcurl4-openssl-dev libxml2-dev \
        libsqlite3-dev libpq-dev libmysqlclient-dev \
        libreadline-dev libyaml-dev libgdbm-dev libncurses5-dev \
        libz-dev libbz2-dev liblzma-dev libgmp-dev \
        libgd-dev libzip-dev libonig-dev libicu-dev \
        # Media / misc
        mediainfo imagemagick fakeroot rpm xvfb \
        # Python
        python3 python3-venv python3-dev python3-pip python3-setuptools \
        # Networking
        net-tools dnsutils iproute2 iputils-ping telnet \
        # Database clients
        sqlite3 postgresql-client mysql-client && \
    # Generate locale
    locale-gen en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

# =============================================================================
# 2. Git (latest from PPA)
# =============================================================================
RUN add-apt-repository ppa:git-core/ppa -y && \
    apt-get update && apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 3. Docker CE + Compose + Buildx
# =============================================================================
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

# Container tools (buildah, podman, skopeo)
RUN apt-get update && \
    apt-get install -y buildah podman skopeo && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 4. Node.js 20 + 22 via NodeSource
# =============================================================================
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g yarn corepack n && \
    corepack enable && \
    n 22 && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 5. PHP 8.3 + Composer + PHPUnit
# =============================================================================
RUN add-apt-repository ppa:ondrej/php -y && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        php8.3 php8.3-cli php8.3-fpm php8.3-common \
        php8.3-curl php8.3-mbstring php8.3-xml php8.3-zip \
        php8.3-pgsql php8.3-sqlite3 php8.3-mysql \
        php8.3-bcmath php8.3-gd php8.3-intl php8.3-readline \
        php8.3-xdebug php8.3-pcov && \
    curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 6. Java (8, 11, 17, 21)
# =============================================================================
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openjdk-8-jdk \
        openjdk-11-jdk \
        openjdk-17-jdk \
        openjdk-21-jdk && \
    rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV JAVA_HOME_8_X64=/usr/lib/jvm/java-8-openjdk-amd64
ENV JAVA_HOME_11_X64=/usr/lib/jvm/java-11-openjdk-amd64
ENV JAVA_HOME_17_X64=/usr/lib/jvm/java-17-openjdk-amd64
ENV JAVA_HOME_21_X64=/usr/lib/jvm/java-21-openjdk-amd64

# =============================================================================
# 7. .NET SDK (8 + 9)
# =============================================================================
RUN curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb \
        -o /tmp/packages-microsoft-prod.deb && \
    dpkg -i /tmp/packages-microsoft-prod.deb && \
    rm /tmp/packages-microsoft-prod.deb && \
    apt-get update && \
    apt-get install -y dotnet-sdk-8.0 dotnet-sdk-9.0 && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 8. Go
# =============================================================================
ARG GO_VERSION=1.23.12
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tar.gz && \
    tar -C /usr/local -xzf /tmp/go.tar.gz && \
    rm /tmp/go.tar.gz

ENV GOROOT=/usr/local/go
ENV GOPATH=/root/go
ENV PATH="${PATH}:${GOROOT}/bin:${GOPATH}/bin"

# =============================================================================
# 9. Ruby
# =============================================================================
RUN apt-get update && \
    apt-get install -y ruby-full && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 10. Rust
# =============================================================================
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

# =============================================================================
# 11. Build tools (CMake, Ninja, Gradle, Maven, Ant)
# =============================================================================
ARG CMAKE_VERSION=3.31.6
RUN curl -fsSL "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.tar.gz" \
        -o /tmp/cmake.tar.gz && \
    tar -C /usr/local --strip-components=1 -xzf /tmp/cmake.tar.gz && \
    rm /tmp/cmake.tar.gz

RUN apt-get update && \
    apt-get install -y ninja-build ant && \
    rm -rf /var/lib/apt/lists/*

ARG GRADLE_VERSION=9.4
RUN curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
        -o /tmp/gradle.zip && \
    unzip -q /tmp/gradle.zip -d /opt && \
    ln -s "/opt/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle && \
    rm /tmp/gradle.zip

ARG MAVEN_VERSION=3.9.13
RUN curl -fsSL "https://dlcdn.apache.org/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
        -o /tmp/maven.tar.gz && \
    tar -C /opt -xzf /tmp/maven.tar.gz && \
    ln -s "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn && \
    rm /tmp/maven.tar.gz

# =============================================================================
# 12. Android SDK
# =============================================================================
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
        "platforms;android-34" \
        "platforms;android-35" \
        "build-tools;34.0.0" \
        "build-tools;35.0.0" \
        "build-tools;35.0.1" \
        "ndk;27.3.13750724" && \
    rm -rf "${ANDROID_HOME}/.temp"

ENV NDK_HOME="${ANDROID_HOME}/ndk/27.3.13750724"

# =============================================================================
# 13. CLI tools
# =============================================================================

# GitHub CLI
RUN (type -p wget >/dev/null || apt-get install wget -y) && \
    mkdir -p -m 755 /etc/apt/keyrings && \
    wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli-stable.list > /dev/null && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Azure CLI (already has install script)
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    rm -rf /var/lib/apt/lists/*

# AWS CLI v2
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip && \
    unzip -q /tmp/awscli.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscli.zip /tmp/aws

# Kubectl
RUN curl -fsSL "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

# Helm
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Packer
RUN curl -fsSL https://releases.hashicorp.com/packer/1.15.0/packer_1.15.0_linux_amd64.zip \
        -o /tmp/packer.zip && \
    unzip -q /tmp/packer.zip -d /usr/local/bin && \
    rm /tmp/packer.zip

# Fastlane (for mobile CI)
RUN gem install fastlane --no-document

# =============================================================================
# 14. Browsers + Drivers (for E2E testing)
# =============================================================================

# Google Chrome
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        | tee /etc/apt/sources.list.d/google-chrome.list > /dev/null && \
    apt-get update && \
    apt-get install -y google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# ChromeDriver (matching Chrome version)
RUN CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') && \
    DRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION%%.*}") && \
    curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/${DRIVER_URL}/linux64/chromedriver-linux64.zip" \
        -o /tmp/chromedriver.zip && \
    unzip -q /tmp/chromedriver.zip -d /tmp && \
    mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chromedriver && \
    rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64

ENV CHROMEWEBDRIVER=/usr/local/bin

# Firefox + Geckodriver
RUN apt-get update && \
    apt-get install -y firefox && \
    rm -rf /var/lib/apt/lists/*

ARG GECKODRIVER_VERSION=0.36.0
RUN curl -fsSL "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-linux64.tar.gz" \
        -o /tmp/geckodriver.tar.gz && \
    tar -C /usr/local/bin -xzf /tmp/geckodriver.tar.gz && \
    chmod +x /usr/local/bin/geckodriver && \
    rm /tmp/geckodriver.tar.gz

ENV GECKOWEBDRIVER=/usr/local/bin

# =============================================================================
# 15. Web servers (disabled by default, workflows can start them)
# =============================================================================
RUN apt-get update && \
    apt-get install -y apache2 nginx && \
    systemctl disable apache2 || true && \
    systemctl disable nginx || true && \
    rm -rf /var/lib/apt/lists/*

# =============================================================================
# 16. Python extras (pipx, common tools)
# =============================================================================
RUN pip3 install --break-system-packages pipx ansible && \
    pipx ensurepath

# =============================================================================
# 17. GitHub Actions runner
# =============================================================================
RUN mkdir -p actions-runner && cd actions-runner \
    && curl -O -L "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && tar xzf "./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && rm "./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"

# Install runner dependencies
RUN chmod +x actions-runner/bin/installdependencies.sh && \
    actions-runner/bin/installdependencies.sh

# Add scripts
ADD scripts/start.sh /root/start.sh
ADD scripts/cleanup.sh /root/cleanup.sh
RUN chmod +x /root/start.sh /root/cleanup.sh

# Cron: cleanup stale docker data every 6 hours
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/* && \
    echo "0 */6 * * * /root/cleanup.sh >> /var/log/docker-cleanup.log 2>&1" | crontab -

ENTRYPOINT ["/root/start.sh"]
