# Forgejo admission

This source patch targets only Forgejo Runner v13.1.0, commit
`6095cb17bfdbded5aa4ea84c18a4b69fd9574cca`. The common release build applies
both native job events and admission. The controller validates the configured
policy and sends the same immutable snapshot to Linux, Windows and macOS.
Existing live templates and registrations require a controlled rollout.

The controller must supply `NOMERCY_FORGEJO_ADMISSION_POLICY` through the
protected daemon/service environment. `NewRunner` captures a strict immutable
snapshot before tasks arrive. Task Context, Secrets, Vars, step environment
and actor names cannot supply or change it. The management layer must protect
the SCM/launchd/Docker service definition and the runner binary; the policy is
not a substitute for protecting those authorities. Runtime propagation and
tamper probes are required before deployment.

The document contains exactly `version: 1`, `instance`, `allowed_owner_ids`
and `trusted_source_owner_ids`. Its instance is compared with the configured
Forgejo HTTPS origin. Destination owners must be explicitly allowed. For any
PR object or `pull_request*` event, base repository identity must match the
destination and head owner must be that owner, another allowed owner or an
explicit trusted source owner. Trusting a source owner alone grants no right
to run workflows from that owner's own repositories.

PR-linked comments (`event.is_pull: true` or `event.issue.pull_request`) also
require the complete PR base/head origin. A default-branch comment workflow
and a PR link stub do not prove that origin; they are refused without remote
lookups. Malformed marker types are refused too.

Policy IDs are Forgejo identities, scoped to this instance. An OIDC role,
actor name or similarly named GitHub account grants nothing. Missing policy,
unknown fields, duplicate fields/IDs, invalid instance, missing/deleted/malformed
PR origin and inconsistent repository metadata all fail closed. Policy size is
bounded to 16 KiB and 100 IDs per list. Metadata reads only bounded relevant
fields; it never serializes the whole event or reads/logs task secrets.

Admission runs at the start of `Runner.run`, after the reporter exists and
before `generateWorkflow`, cache preparation, `runner.New`, plan execution or
workflow steps. A refusal returns a fixed error through normal reporter
closure, yielding a failed result and the existing native finish event. Even
`always()` steps remain unexecuted. A nil context with a valid task ID is
normalized only for safe reporting; it still fails admission. A wholly missing
task cannot identify a forge task and is refused without running anything.

For an isolated source test, apply the existing event patch first, then this
separate helper. Both verify the exact reviewed commit:

```text
python images/forgejo-events/apply.py <isolated-source>
python images/forgejo-admission/apply.py <isolated-source>
gofmt -w <isolated-source>/internal/app/run/{runner.go,admission.go,admission_test.go}
go test ./internal/app/run -run '^TestAdmission' -count=1 -timeout=60s
```

Use the already pinned `golang:1.26.7` toolchain, bounded CPU/memory and private
source/cache mounts only. No Docker socket, runner volumes or deployment
credentials are needed. The new tests use synthetic IDs, exercise immutable
policy, allowed destinations/push/dispatch/schedule, trusted/untrusted PR heads,
actor non-authority, malformed metadata, and real reporter/native failure
closure with zero steps and an absent `always()` sentinel. Upstream tests that
construct context-free runners intentionally do not provide the new policy;
the focused tests are preparation evidence, not a six-platform release proof.

Validation on 2026-10-12 used Go 1.26.7, image ID
`sha256:e30143be198ab04cf7ba25fba83ab3a692ca584c994aad0bf131fa0eb32dd8c1`,
with 2 CPUs, 4 GiB memory and private source/module/build caches. All six
admission test functions and 63 subtests passed; the final run had networking
disabled. Real reporter closure emitted native `failure` events with zero
workflow steps for outside-owner, nil-context, missing-policy and malformed-
policy cases. All six platform binaries built successfully with networking
disabled after the pinned module dependencies had been cached. No live runner
was changed by that build. Artifact hashes are in `PROVENANCE.json`.

Owner IDs must be verified on the configured instance. On the deployment
instance FiLL has ID 1 and NoMercy-Entertainment has organisation ID 7. Other
users remain untrusted until explicitly selected by the operator and verified.
GitHub logins are never substituted for Forgejo identities.

`FORGEJO_RUNNER_SCOPE=user` remains the default. Explicit `instance` scope
uses the administrator endpoint for listing, registration and deletion; it is
refused without a valid protected policy. It requires `read:admin` and
`write:admin` on the controller token. Set this scope only after all affected
units carry the verified guarded binary, protected startup configuration and
the policy. Changing scope does not convert existing user registrations.
Installing a policy alone does not grant server-side scheduling availability.

Primary source: [reviewed Forgejo runner](https://code.forgejo.org/forgejo/runner/src/commit/6095cb17bfdbded5aa4ea84c18a4b69fd9574cca/internal/app/run/runner.go).
Forgejo 16.0.5's [IssueCommentPayload](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.5/modules/structs/hook.go)
contains `issue`, optional `pull_request`, and `is_pull`; its
[Actions notifier](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.5/services/actions/notifier.go)
distinguishes PR comments from regular issue comments. The admission check
requires full origin for these markers even if a server omits the full PR.
