#!/usr/bin/env bash
# The traceable build of forgejo-runner for the platforms Forgejo publishes no
# binary for (OPEN-4, design 9.2 and 9.4). Run inside a pinned golang image,
# never on a host:
#
#   docker run --rm -v <out dir>:/out -v <this dir>:/build:ro \
#     -v <images/forgejo-events>:/events:ro \
#     golang:1.26.7 bash /build/build-forgejo-runner.sh v13.1.0
#
# The Go version is the tag's own `toolchain` line (go.mod), so the image is
# pinned to it. CGO off and -trimpath keep the build independent of the
# machine it runs on; the version ldflag makes `--version` print the tag and
# not `dev`, which is what the binary running on BEAST-UNIT today prints.
# Writes each binary, SHA256SUMS, and PROVENANCE: tag, commit, Go version.
set -euo pipefail
TAG="${1:?usage: build-forgejo-runner.sh <tag, e.g. v13.1.0>}"
MAJOR="${TAG%%.*}"

git clone -q --depth 1 --branch "$TAG" https://code.forgejo.org/forgejo/runner.git /src
cd /src
COMMIT="$(git rev-parse HEAD)"
if [ "$TAG" != v13.1.0 ] || [ "$COMMIT" != 6095cb17bfdbded5aa4ea84c18a4b69fd9574cca ]; then
  echo 'job event patch requires the reviewed v13.1.0 source commit' >&2
  exit 1
fi
# Mount images/forgejo-events read-only at /events beside this script.
git apply --check /events/reporter.patch
git apply /events/reporter.patch
cp /events/event.go /events/event_test.go internal/pkg/report/
gofmt -w internal/pkg/report/event.go internal/pkg/report/event_test.go internal/pkg/report/reporter.go
WANT="$(sed -n 's/^toolchain //p' go.mod)"
HAVE="$(go env GOVERSION)"
if [ -n "$WANT" ] && [ "$WANT" != "$HAVE" ]; then
  echo "the tag asks for $WANT; this image has $HAVE" >&2
  exit 1
fi

go test ./internal/pkg/report
for target in linux/amd64 linux/arm64 windows/amd64 windows/arm64 darwin/amd64 darwin/arm64; do
  os="${target%/*}" arch="${target#*/}"
  ext=""; [ "$os" = windows ] && ext=".exe"
  out="/out/forgejo-runner-${TAG}-${os}-${arch}${ext}"
  GOOS="$os" GOARCH="$arch" CGO_ENABLED=0 go build -trimpath \
    -tags 'netgo osusergo' \
    -ldflags "-s -w -X code.forgejo.org/forgejo/runner/${MAJOR}/internal/pkg/ver.version=${TAG}" \
    -o "$out" .
done

cd /out
sha256sum forgejo-runner-"${TAG}"-* > SHA256SUMS
{
  echo "tag=${TAG}"
  echo "commit=${COMMIT}"
  echo 'job_events=1'
  sha256sum /events/reporter.patch /events/event.go /events/event_test.go
  echo "go=${HAVE}"
  echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "flags=CGO_ENABLED=0 -trimpath -tags 'netgo osusergo' -ldflags '-s -w -X .../ver.version=${TAG}'"
} > PROVENANCE
cat PROVENANCE SHA256SUMS
