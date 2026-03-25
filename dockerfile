# base image
FROM ubuntu:24.04

# input GitHub runner version argument
ARG RUNNER_VERSION=2.322.0
ENV DEBIAN_FRONTEND=noninteractive

LABEL Author="Stoney_Eagle"
LABEL Email="stoney@nomercy.tv"
LABEL GitHub="https://github.com/StoneyEagle"
LABEL BaseImage="ubuntu:24.04"
LABEL RunnerVersion=${RUNNER_VERSION}

WORKDIR /root

# ── base system packages ───────────────────────────────────────────────────────
RUN apt-get update -y && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        curl wget unzip zip vim git jq rsync \
        build-essential libssl-dev libffi-dev \
        python3 python3-venv python3-dev python3-pip \
        ca-certificates gnupg sudo openssh-client \
        software-properties-common apt-transport-https && \
    rm -rf /var/lib/apt/lists/*

# ── Azure CLI ──────────────────────────────────────────────────────────────────
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    rm -rf /var/lib/apt/lists/*

# ── Docker CE ─────────────────────────────────────────────────────────────────
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update -y && \
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

# ── Node.js 22 LTS ────────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g yarn && \
    rm -rf /var/lib/apt/lists/*

# ── PHP 8.3 + Composer ────────────────────────────────────────────────────────
RUN add-apt-repository ppa:ondrej/php -y && \
    apt-get update -y && \
    apt-get install -y --no-install-recommends \
        php8.3 php8.3-cli php8.3-fpm \
        php8.3-curl php8.3-mbstring php8.3-xml php8.3-zip \
        php8.3-pgsql php8.3-sqlite3 php8.3-bcmath php8.3-gd && \
    curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer && \
    rm -rf /var/lib/apt/lists/*

# ── Java 21 JDK ───────────────────────────────────────────────────────────────
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends openjdk-21-jdk && \
    rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# ── .NET 9 SDK ────────────────────────────────────────────────────────────────
RUN curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb -o /tmp/packages-microsoft-prod.deb && \
    dpkg -i /tmp/packages-microsoft-prod.deb && \
    rm /tmp/packages-microsoft-prod.deb && \
    apt-get update -y && \
    apt-get install -y dotnet-sdk-9.0 && \
    rm -rf /var/lib/apt/lists/*

# ── Android SDK (command-line tools only) ─────────────────────────────────────
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH="${PATH}:${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools"

RUN mkdir -p /opt/android-sdk/cmdline-tools && \
    curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
        -o /tmp/cmdline-tools.zip && \
    unzip -q /tmp/cmdline-tools.zip -d /tmp/cmdline-tools-extract && \
    mv /tmp/cmdline-tools-extract/cmdline-tools /opt/android-sdk/cmdline-tools/latest && \
    rm -rf /tmp/cmdline-tools.zip /tmp/cmdline-tools-extract && \
    yes | sdkmanager --licenses && \
    sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0"

# ── GitHub Actions runner ─────────────────────────────────────────────────────
RUN mkdir -p actions-runner && cd actions-runner \
    && curl -O -L https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    && tar xzf ./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    && rm ./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz

# install runner dependencies
RUN chmod +x actions-runner/bin/installdependencies.sh && actions-runner/bin/installdependencies.sh

# add the start script
ADD scripts/start.sh /root/start.sh

# make the script executable
RUN chmod +x /root/start.sh

# set the entrypoint to the start.sh script
ENTRYPOINT ["/root/start.sh"]
