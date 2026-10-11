# forgejo-runner/Dockerfile
#
# A slim runner, not the GitHub runner image. Forgejo jobs run inside job
# containers named by the ubuntu-*:docker:// labels, so the toolchain in the
# GitHub image would be gigabytes this container never executes.
#
# git is the one tool the runner itself needs: it materialises actions from
# its cache with `git worktree add`, shelled out to the git CLI. Without it
# every job using actions/checkout fails before its first step with
# `exec: "git": executable file not found in $PATH`.
FROM ubuntu:24.04

ARG FORGEJO_RUNNER_VERSION=13.1.0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git procps tzdata \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu noble stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
        # fuse-overlayfs for nested Docker (overlay-on-overlay)
        fuse-overlayfs fuse3 \
    && curl -fsSL -o /usr/local/bin/forgejo-runner \
        "https://code.forgejo.org/forgejo/runner/releases/download/v${FORGEJO_RUNNER_VERSION}/forgejo-runner-${FORGEJO_RUNNER_VERSION}-linux-amd64" \
    && chmod +x /usr/local/bin/forgejo-runner \
    && forgejo-runner --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY scripts/start-forgejo.sh /root/start-forgejo.sh
RUN chmod +x /root/start-forgejo.sh

VOLUME /data
ENTRYPOINT ["/root/start-forgejo.sh"]
