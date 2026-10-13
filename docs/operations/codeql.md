# CodeQL build context

The repository contains Go overlays for Forgejo runner v13.1.0, not an independent Go application. Default setup invents a root module, cannot resolve the pinned dependencies, and cannot resolve the upstream Runner type. A successful job with missing-package diagnostics is incomplete coverage.

The advanced CodeQL workflow retains actions, JavaScript/TypeScript, Python and Go. Go uses 1.26.7 and assembles the reviewed Forgejo commit 6095cb17bfdbded5aa4ea84c18a4b69fd9574cca with both existing overlays before compiling the actual report and run packages. Source preparation refuses a different commit or toolchain and fails if dependencies cannot be downloaded.

Analysis results are kept in full. Exact copied overlay locations are mapped to their tracked repository paths without removing results, changing lines or suppressing diagnostics. The mapper verifies those copies are byte-identical first. Upstream findings retain their original locations.

The workflow first runs and validates its analyses with upload disabled. After merging, disable default setup, set the repository Actions variable `CODEQL_ADVANCED_ENABLED=true`, and dispatch the workflow. Verify all four completed uploads plus the Go SARIF diagnostics. GitHub does not accept advanced uploads while default setup is enabled. Retain the previous analysis history; do not dismiss missing-package warnings or remove Go from scanning.

References: [CodeQL build modes](https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-for-compiled-languages), [advanced setup](https://docs.github.com/en/code-security/how-tos/scan-code-for-vulnerabilities/configure-code-scanning/setting-up-advanced-setup-for-code-scanning).
