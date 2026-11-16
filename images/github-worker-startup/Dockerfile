# Build from the exact deployed unit, which includes SDK 8.0.427.
ARG BASE=nomercy/runner-unit-github:20261029@sha256:859b66a6d6a5acf2f9a87b088754c53eb9a3c4751dc4f66b9a700dce844dc576
FROM ${BASE} AS build
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 DOTNET_PROCESSOR_COUNT=2
COPY build.py /tmp/build-worker.py
RUN curl -fsSL --retry 3 -o /tmp/runner-source.tar.gz \
      https://codeload.github.com/actions/runner/tar.gz/refs/tags/v2.338.0 \
    && python3 /tmp/build-worker.py /tmp/runner-source.tar.gz

FROM ${BASE}
COPY --from=build /out/Runner.Worker.dll /opt/actions-runner-image/bin/Runner.Worker.dll
COPY --from=build /out/worker-startup.json /runner/worker-startup.json
LABEL nomercy.worker_startup_patch="1"
