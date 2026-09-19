# Forgejo runner for macOS: the self-built darwin artefact

Forgejo publishes no darwin runner (design 9.4: release v13.1.0 ships only
`linux-amd64` and `linux-arm64`). The Forgejo runner that is online in the
macOS appliance today runs a locally cross-compiled binary. Design 9.4
records, as measured when it was written, that `~/macos_runner/` in the
`macos-runner` VM holds `forgejo-runner-darwin-amd64` and
`forgejo-runner-darwin-arm64`, and that `bootstrap.sh` there documents them
as cross-compiled. This file makes that artefact traceable (risk R-4).

## What runs today

**Not measured by this task.** Reading the binary's hash and `--version`
means logging in to the appliance, which is the MACOS-ENV gate. When that
gate is open, record these in `manifest.json`, read-only:

```sh
shasum -a 256 ~/macos_runner/forgejo-runner-darwin-amd64
~/macos_runner/forgejo-runner-darwin-amd64 --version
```

If `--version` prints `dev`, the binary has the same gap as the Windows one
(`images/windows/build-forgejo-runner.md`): it was built without the version
flag, and its tag and commit cannot be recovered from it.

## How a traceable build is made

The same recipe as for Windows, with `GOOS=darwin`. Upstream's `Makefile`
names `DARWIN_ARCHS ?= darwin-12/amd64,darwin-12/arm64`, but no release
target uses it (design 9.4). The appliance is x86_64 (QEMU on a Xeon), so
`amd64` is the binary that runs.

```sh
TAG=v13.1.0                      # pin it; never build a branch
MAJOR=${TAG%%.*}
git clone --depth 1 --branch "$TAG" https://code.forgejo.org/forgejo/runner.git
cd runner
COMMIT=$(git rev-parse HEAD)

GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 \
  go build -trimpath -tags 'netgo osusergo' \
  -ldflags "-s -w -X code.forgejo.org/forgejo/runner/${MAJOR}/internal/pkg/ver.version=${TAG}" \
  -o forgejo-runner-darwin-amd64

go version
shasum -a 256 forgejo-runner-darwin-amd64
```

**Built on 2026-09-19**, by the script that recipe became:
`images/windows/build-forgejo-runner.sh`, which builds windows/amd64 and both
darwin architectures from one pinned tag, in a `golang` container on the WSL
engine. Nothing was installed on the host.

```
docker run --rm -v <out>:/out -v images/windows:/build:ro golang:1.26.7   bash /build/build-forgejo-runner.sh v13.1.0
```

From tag v13.1.0, commit `6095cb17bfdbded5aa4ea84c18a4b69fd9574cca`, with
go1.26.7 - the `toolchain` line of that tag's `go.mod`, which the script
refuses to build without:

| Binary | SHA-256 |
| --- | --- |
| `forgejo-runner-v13.1.0-darwin-amd64` | `f9f9ed421d6d1e71436b92e9b8891fb03b44c713e87e545c6e86423be2edb07a` |
| `forgejo-runner-v13.1.0-darwin-arm64` | `c5f4bff75398edc6961f21a8f6c5916008a3b9ce9895fd627b95bf9d4fc5706b` |

A second build in a fresh container gave the same hashes. Both builds ran on
the same machine, so reproducibility across machines is still a claim. The
binaries are kept in `D:/HyperV/runner-platform/artefacts/`, not in the
repository, and recorded in `manifest.json`.

**Not installed.** The appliance still runs the binary it has always run.
Replacing it restarts the runner inside the guest, which is MACOS-ENV and
waits for an idle moment and an operator's go.

## Becoming a template

For the appliance runtime (`agent/runtimes/macos_appliance.py`), a runner's
software is a template directory under `/Users/runner/templates/<name>/`
with three entry points: `run`, `register` (the plan as JSON on standard
input, the forge's ids as JSON on standard output) and `deregister`. The
binary above becomes the template `forgejo-runner-darwin-amd64-<tag>`, and a
runner's `runtime_template` names it. That is what `status()` reports. The
three scripts wrap `forgejo-runner register` and `forgejo-runner daemon`. They
are written when the appliance is adopted (T-0802, MACOS-ENV), against the
runner's real configuration, and not before.

## Keeping it alive

There is no release feed for darwin. At every Forgejo runner release,
rebuild from the new tag and add a line to the manifest. This belongs in the
version-deprecation runbook (`docs/operations/runner-platform.md`), next to
the Windows artefact and the GitHub runner's `RUNNER_VERSION`.
