# Forgejo runner for Windows: a self-built artefact

Forgejo publishes no Windows runner. Release v13.1.0 has twelve assets, all
`linux-amd64` or `linux-arm64` (design 9.2, 9.4). A Windows runner therefore
has to be cross-compiled from source and maintained here. OPEN-4 in the
design decided on 2026-09-18 to keep doing that, built traceably in a pinned
`golang` container on the existing engine rather than with Go installed on
the host. This file records what runs today, and how the traceable build is
made.

## What runs today

**Measured on BEAST-UNIT, 2026-09-18, read-only:**

| | |
| --- | --- |
| Path | `C:\forgejo-runner\forgejo-runner.exe`, run as the `forgejo-runner` service through NSSM |
| Size | 19,201,024 bytes |
| Written | 2026-05-30 05:36:47 |
| SHA-256 | `7F2B3DECAA25F32B401B04B45C307397208E716D63F2B5F58E99791254881ADB` |
| `--version` | `forgejo-runner version dev` |

`dev` is the value the version variable has when the build does not set it,
so this binary was made with a plain `go build`. **Nothing in it records the
tag or commit it came from**, and nothing in this repository does either.
That is risk R-4 at its worst: when Forgejo deprecates the runner protocol
this binary speaks, there is no way to tell from the binary whether it is
affected, and no recorded way to build its successor.

`images/windows/manifest.json` records it as it is: hash known, provenance
unknown.

## Built, 2026-09-18

`build-forgejo-runner.sh` (next to this file) is the recipe below, made into
a script. It runs in `golang:1.26.7` on the WSL engine, so no Go toolchain is
installed on the host. Go 1.26.7 is the `toolchain` line of tag v13.1.0's
`go.mod`, and the script refuses to build with any other version.

```
docker run --rm -v <out>:/out -v images/windows:/build:ro golang:1.26.7 \
  bash /build/build-forgejo-runner.sh v13.1.0
```

Built from tag v13.1.0, commit `6095cb17bfdbded5aa4ea84c18a4b69fd9574cca`:

| Binary | SHA-256 |
| --- | --- |
| `forgejo-runner-v13.1.0-windows-amd64.exe` | `82ea01bc63c3ba60526576f3d8ac491a1e77db0f8d3d55cc37bd39666a5f04c8` |
| `forgejo-runner-v13.1.0-darwin-amd64` | `f9f9ed421d6d1e71436b92e9b8891fb03b44c713e87e545c6e86423be2edb07a` |
| `forgejo-runner-v13.1.0-darwin-arm64` | `c5f4bff75398edc6961f21a8f6c5916008a3b9ce9895fd627b95bf9d4fc5706b` |

- **MEASURED:** the Windows binary prints `forgejo-runner version v13.1.0` on
  this host. The binary running today prints `dev`.
- **MEASURED:** a second build in a fresh container gave the same three
  hashes. Both builds ran on the same machine; a build on a second machine has
  not been done yet.
- The binaries are kept under `D:\HyperV\runner-platform\artefacts\`, not in
  the repository. Their record is `manifest.json`.
- **Not yet deployed.** The running Windows runner is still the `dev` binary.
  Replacing it means restarting its service, which cancels a job it is
  running, so it waits for an idle moment and an operator's go.

## How a traceable build is made

The recipe follows upstream's own `Makefile` (fetched 2026-09-18 from
`code.forgejo.org/forgejo/runner`, branch `main`), whose build line is:

```
go build -v -tags 'netgo osusergo $(TAGS)' -ldflags '$(EXTLDFLAGS)-s -w $(LDFLAGS)'
LDFLAGS ?= -X "code.forgejo.org/forgejo/runner/v13/internal/pkg/ver.version=v$(RELEASE_VERSION)"
```

The module path carries the major version (`/v13` on `main`). Use the major
of the tag you build.

```sh
TAG=v13.1.0                      # pin it; never build a branch
MAJOR=${TAG%%.*}                 # v13
git clone --depth 1 --branch "$TAG" https://code.forgejo.org/forgejo/runner.git
cd runner
COMMIT=$(git rev-parse HEAD)

GOOS=windows GOARCH=amd64 CGO_ENABLED=0 \
  go build -trimpath -tags 'netgo osusergo' \
  -ldflags "-s -w -X code.forgejo.org/forgejo/runner/${MAJOR}/internal/pkg/ver.version=${TAG}" \
  -o forgejo-runner-windows-amd64.exe

go version                       # record it
sha256sum forgejo-runner-windows-amd64.exe
```

Two flags go beyond the Makefile, for reproducibility. `CGO_ENABLED=0` keeps
the build independent of a C toolchain, and `-trimpath` keeps the build
machine's paths out of the binary. With those two flags set, the same tag and
the same Go version should give the same hash.
**Verify:** build twice, on two machines, and compare. Until that has been
done, reproducibility is a claim, not a fact.

After a build, record in `manifest.json`: the tag, the commit, the Go
version, the SHA-256, the date and who built it. Then check that
`forgejo-runner-windows-amd64.exe --version` prints the tag and not `dev`.

## What the runner can do on Windows

Only the `host` executor. Forgejo's labels are `<name>:<type>://<image>`
with types `docker`, `lxc` and `host`. A Windows instance registers with
`host`, because it has no container engine (design 9.2, T-1003). The host
executor forks a shell, and upstream neither builds nor tests it on Windows.
A workflow that assumes a POSIX shell will fail there.

## Keeping it alive

There is no upstream release feed for this artefact, so nothing announces
when it has to be rebuilt. It belongs in the version-deprecation runbook
beside the GitHub runner's `RUNNER_VERSION`: at every Forgejo runner release,
check the release notes for protocol changes, rebuild from the new tag, and
record the new line in the manifest. The runbook is in
`docs/operations/runner-platform.md` (T-2201).
