#!/usr/bin/env bash
# The traceable build of forgejo-runner for the platforms Forgejo publishes no
# binary for (OPEN-4, design 9.2 and 9.4). Run inside a pinned golang image,
# never on a host:
#
#   docker run --rm -v <out dir>:/out -v <this dir>:/build:ro \
#     -v <images/forgejo-events>:/events:ro \
#     -v <images/forgejo-security>:/security:ro \
#     golang:1.26.7 bash /build/build-forgejo-runner.sh v13.1.0
# Add the admission mount and second argument 'guarded' only for a deployment
# with a protected admission policy. Both modes include native events and
# security corrections.
#
# The Go version is the tag's own `toolchain` line (go.mod), so the image is
# pinned to it. CGO off and -trimpath keep the build independent of the
# machine it runs on; the version ldflag makes `--version` print the tag and
# not `dev`, which is what the binary running on BEAST-UNIT today prints.
# Writes each binary, SHA256SUMS, and PROVENANCE: tag, commit, Go version.
set -euo pipefail
TAG="${1:?usage: build-forgejo-runner.sh <tag, e.g. v13.1.0> [native-events|guarded]}"
MODE="${2:-native-events}"
case "$MODE" in native-events|guarded) ;; *) echo 'unknown Forgejo build mode' >&2; exit 1 ;; esac
MAJOR="${TAG%%.*}"
SOURCE="${FORGEJO_BUILD_SOURCE:-/src}"
OUTPUT="${FORGEJO_BUILD_OUTPUT:-/out}"
EVENTS="${FORGEJO_BUILD_EVENTS:-/events}"
ADMISSION="${FORGEJO_BUILD_ADMISSION:-/admission}"
SECURITY="${FORGEJO_BUILD_SECURITY:-/security}"
# Let the selected Go executable resolve its own standard library and tools.
# A runner image's GOROOT may still name its preinstalled older SDK.
unset GOROOT

git clone -q --depth 1 --branch "$TAG" https://code.forgejo.org/forgejo/runner.git "$SOURCE"
cd "$SOURCE"
COMMIT="$(git rev-parse HEAD)"
if [ "$TAG" != v13.1.0 ] || [ "$COMMIT" != 6095cb17bfdbded5aa4ea84c18a4b69fd9574cca ]; then
  echo 'job event patch requires the reviewed v13.1.0 source commit' >&2
  exit 1
fi
# Mount images/forgejo-events read-only at /events beside this script.
git apply --check "$EVENTS/reporter.patch"
git apply "$EVENTS/reporter.patch"
cp "$EVENTS/event.go" "$EVENTS/event_test.go" internal/pkg/report/
gofmt -w internal/pkg/report/event.go internal/pkg/report/event_test.go internal/pkg/report/reporter.go
VERSION="$TAG"
ADMISSION_VERSION=0
BUILD_FLAGS=()
if [ "$MODE" = guarded ]; then
  git apply --check "$ADMISSION/runner.patch"
  git apply "$ADMISSION/runner.patch"
  cp "$ADMISSION/admission.go" "$ADMISSION/admission_test.go" internal/app/run/
  gofmt -w internal/app/run/runner.go internal/app/run/admission.go internal/app/run/admission_test.go
  VERSION="${TAG}-nomercy-admission.1"
  ADMISSION_VERSION=1
  BUILD_FLAGS=(-buildvcs=false)
fi
python3 "$SECURITY/apply.py" "$SOURCE"
gofmt -w act/filecollector/file_collector.go act/filecollector/copy_security_test.go \
  act/common/git/git.go act/common/git/hostname_security_test.go \
  act/runner/step.go act/runner/symlink_security_test.go
WANT="$(sed -n 's/^toolchain //p' go.mod)"
HAVE="$(go env GOVERSION)"
if [ "$(go tool compile -V)" != "compile version ${HAVE}" ]; then
  echo 'the selected Go command and compiler do not match' >&2
  exit 1
fi
if [ -n "$WANT" ] && [ "$WANT" != "$HAVE" ]; then
  echo "the tag asks for $WANT; this image has $HAVE" >&2
  exit 1
fi

go test ./internal/pkg/report
go test ./act/filecollector -run '^TestNoMercy' -count=1
go test ./act/common/git -run '^TestFindSlug$|^TestNoMercy' -count=1
go test ./act/runner -run '^TestNoMercy' -count=1
if [ "$MODE" = guarded ]; then
  go test ./internal/app/run -run '^TestAdmission' -count=1 -timeout=60s
fi
for target in linux/amd64 linux/arm64 windows/amd64 windows/arm64 darwin/amd64 darwin/arm64; do
  os="${target%/*}" arch="${target#*/}"
  ext=""; [ "$os" = windows ] && ext=".exe"
  out="${OUTPUT}/forgejo-runner-${VERSION}-${os}-${arch}${ext}"
  GOOS="$os" GOARCH="$arch" CGO_ENABLED=0 go build "${BUILD_FLAGS[@]}" -trimpath \
    -tags 'netgo osusergo' \
    -ldflags "-s -w -X code.forgejo.org/forgejo/runner/${MAJOR}/internal/pkg/ver.version=${VERSION}" \
    -o "$out" .
done

cd "$OUTPUT"
sha256sum forgejo-runner-"${VERSION}"-* > SHA256SUMS
{
  echo "tag=${TAG}"
  echo "commit=${COMMIT}"
  echo 'job_events=1'
  echo 'security_overlay=1'
  echo "admission_version=${ADMISSION_VERSION}"
  echo "build_mode=${MODE}"
  if [ "$MODE" = guarded ]; then echo 'buildvcs=false'; else echo 'buildvcs=auto'; fi
  echo "version=${VERSION}"
  sha256sum "$EVENTS/reporter.patch" "$EVENTS/event.go" "$EVENTS/event_test.go"
  if [ "$MODE" = guarded ]; then
    sha256sum "$ADMISSION/runner.patch" "$ADMISSION/admission.go" "$ADMISSION/admission_test.go"
  fi
  sha256sum "$SECURITY/apply.py" "$SECURITY/copy_security_test.go" \
    "$SECURITY/hostname_security_test.go" "$SECURITY/symlink_security_test.go"
  echo "go=${HAVE}"
  echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "flags=CGO_ENABLED=0 ${BUILD_FLAGS[*]} -trimpath -tags 'netgo osusergo' -ldflags '-s -w -X .../ver.version=${VERSION}'"
} > PROVENANCE
cat PROVENANCE SHA256SUMS
