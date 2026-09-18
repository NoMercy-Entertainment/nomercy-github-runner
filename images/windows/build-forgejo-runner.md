# Forgejo runner for Windows: a self-built artefact

Forgejo publishes no Windows runner. Release v13.1.0 has twelve assets, all
`linux-amd64` or `linux-arm64` (design 9.2, 9.4). A Windows runner therefore
has to be cross-compiled from source and maintained here. OPEN-4 in the
design is the decision whether to keep doing that, and **it is still open**.
This file records what runs today, and how a traceable build would be made
if OPEN-4 says yes.

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

## How a traceable build is made

**Not run here.** This machine has no Go toolchain, and installing one is a
change to the host that this task was not asked to make. The recipe below
follows upstream's own `Makefile` (fetched 2026-09-18 from
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
