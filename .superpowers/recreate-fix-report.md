# Recreate-fleet cascade fix

## Status
Complete. Fix implemented, tested, and committed on `master` (no branch created, per instructions). No docker commands were run and no deployment was performed — the fleet was never touched.

## Commit
`d18b692` — fix: stop recreate-fleet cascade that destroyed six runners

## Changes
- `dashboard/app.py` — `api_recreate()` rewritten: after `ops.remove(name)`, ground truth is `name not in ops.list_runner_names()`, not the command's exit status. Gone-but-reported-failed still gets a replacement created. Still-present (or a failing `create()`) aborts the sweep immediately, returns HTTP 500 with `aborted_at` naming the runner, and does not touch the rest of the fleet. Response shape `{"ok": bool, "results": [...]}` preserved; `aborted_at` added only on abort.
- `dashboard/docker_ops.py` — `remove()` default timeout raised 120s → 180s, with a comment explaining the nested Docker-in-Docker teardown cost.
- `dashboard/tests/test_routes.py` — 4 new tests, all monkeypatched (no real Docker daemon touched):
  - `test_recreate_happy_path_removes_and_recreates_every_runner`
  - `test_removal_reported_as_failed_but_container_gone_still_creates_replacement`
  - `test_removal_that_leaves_the_container_present_aborts_the_sweep` (regression test for the actual incident; asserts `ops.remove` is never called again after the runner that stayed present)
  - `test_failing_create_also_aborts_the_sweep`

## Test summary
`python -m pytest dashboard/tests -v` → **91 passed** (87 pre-existing + 4 new), 0 failed.

## Concerns
- None outstanding. The dashboard's `.env` was not read, printed, or modified. No files touched outside `dashboard/app.py`, `dashboard/docker_ops.py`, and `dashboard/tests/test_routes.py`; nothing under `BeastStack/` was touched. Staged and committed those three files by name only.
- The controller will need to actually exercise `/api/recreate` against real containers to confirm the 180s timeout is sufficient in practice — this was verified only via unit tests with a mocked `docker_ops`, as instructed.
