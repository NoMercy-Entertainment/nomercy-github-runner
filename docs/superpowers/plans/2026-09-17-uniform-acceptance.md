# Uniform runner platform: acceptance record

T-2003. One row per acceptance criterion of the design (section 4.5), with
the command that measures it, what it gave, when, and the verdict. Design
19.4 requires it, and says "not run" is a permitted outcome and a required
one where the infrastructure does not exist. No row here is claimed without
the evidence beside it.

**Where things stand.** Every task whose gate is LOCAL has been built and
tested. Nothing has been deployed, and no infrastructure has been created,
changed or removed. There is no Hyper-V worker, the agent runs on no
machine, no runner has registered through the new controller, and the
13 containers on WSL still run the way they did before.
So every criterion that needs a real worker, a real forge or a migration is
**not run**. The criteria that can be shown with local tests are **pass
(local)**: they have been proved against fakes. The platforms were never
reached.

**Verdicts.** *pass* - met and shown. *pass (local)* - met by the code and
shown by tests against fakes or on this machine, not on the platform.
*partial* - part of it is met; the row says which part. *not met* - known
not to hold yet, and why. *not run* - cannot be measured without
infrastructure that does not exist.

Measured 2026-09-18 on BEAST-UNIT (Windows 10 Pro 19045), Python 3.13, from
the repository at the commit this file was added in.

| ID | Criterion | Command | Result | Verdict |
| --- | --- | --- | --- | --- |
| ACC-1 | All six combinations implemented | One runner of each registers and completes a job | Not attempted. Built: provider adapters for all six cells (T-0901, T-1001), runtimes for all three platforms (T-0501, T-0702, T-0801). Registering needs FORGE-LIVE, and a worker needs HYPERV. | not run |
| ACC-2 | GitHub on Linux, Windows, macOS | A workflow per platform on a self-hosted label | Not attempted: no Windows worker, macOS instance or new Linux worker exists | not run |
| ACC-3 | Forgejo on Linux, Windows, macOS | As ACC-2 against Forgejo | Not attempted, for the same reason. The Windows and macOS artefacts have build recipes (T-0705, T-0805) and have not been built. | not run |
| ACC-4 | Hyper-V is the common layer | `Get-VM` lists every worker | No worker VM has been created (phase 5 is HYPERV) | not run |
| ACC-5 | WSL is not part of the runner architecture | `wsl -l -v`; no `/mnt/` path in the operational configuration | The fleet still runs on WSL. `pytest tests/test_wsl_couplings.py`: **3 passed, 9 xfailed**. The nine xfails are the WSL couplings that remain, lifted one by one by T-1708 | not met |
| ACC-6 | One RunnerSpec and state machine | `pytest tests/test_state_machine.py tests/test_provisioning_flow.py` | **75 passed**. One table, one transition table parsed from the design's diagram, one flow for all six cells, and a source check that fails on any branch that names a platform or a forge | pass (local) |
| ACC-7 | One control protocol | `pytest agent/tests/test_verbs.py`; `pytest tests/test_mtls.py tests/test_heartbeat.py tests/test_protocol_version.py` | Agent verb set: **72 passed**. mTLS, heartbeat and protocol versions: **70 passed**. It is one closed verb set behind mutual TLS, and it is **not deployed**. Today's fleet is still controlled through the Docker socket by the v1 dashboard, which is a second control path until T-1407 | partial |
| ACC-8 | Same isolated storage structure | `pytest agent/tests/test_cache_ownership.py agent/tests/test_cache_safety.py tests/test_storage_naming.py` | Agent side: **51 passed**; storage naming on the controller side: **31 passed**. Every area of every runner is derived from its runner_id on all three runtimes, and no two runners share a place. For the two directory runtimes the same was checked on this machine's real NTFS disk, with every file of the other runner hashed before and after | pass (local) |
| ACC-9 | Same API and dashboard components | `pytest tests/test_generic_card.py tests/test_generic_actions.py tests/test_fleet_routes.py tests/test_runner_detail_v2.py` | **95 passed**, including the v2 page and the runner page loaded in headless Edge. Built: one card, one detail page and one route family. The v1 page and routes still serve today's containers beside it (T-1407) | partial |
| ACC-10 | Every fleet scales up and down | `pytest tests/test_desired_capacity.py tests/test_placement.py tests/test_drain_semantics.py` | **36 passed**, against fakes. Scale-down never touches a busy runner, and a full fleet waits in `planned` and says why. Not run on a worker | pass (local) |
| ACC-11 | Lifecycle verbs work for every runner class | `pytest agent/tests/contract agent/tests/test_contract_teeth.py`; the per-class integration test | Contract suite: **35 passed** (all nine scenarios × three runtimes, against fakes). Teeth: **16 passed**: the suite fails a runtime that breaks each rule. Drain is declared unsupported on all three (OPEN-7). The per-class integration test on real workers was not run | partial |
| ACC-12 | No read-only "Elsewhere" runners remain | The section and its template are deleted | Still there. Deleting it is T-1405, which waits for T-0802 (adopting the macOS runner: MACOS-ENV, NEVER-AUTO). The v2 page shows those runners with the same card as every other, each action disabled with the reason | not met |
| ACC-13 | Provider logic is not mixed with platform logic | `pytest tests/test_provider_platforms.py tests/test_github_platforms.py tests/test_forgejo_platforms.py` | **121 passed**. The import graph is checked in both directions, and each provider adapter is checked for a branch that names a platform, with label defaults as the only data allowed to differ | pass |
| ACC-14 | No half instances or orphaned registrations | `pytest tests/test_partial_failure.py tests/test_repair.py tests/test_forgejo_deregistration.py` | **42 passed**, against fakes. A crash at each step converges or is swept. A unit is never removed before its forge record. Repair makes no second unit and no second record | pass (local) |
| ACC-15 | Existing history is preserved | Row count and spot checks before and after migration | No migration has run. The backfill is built and tested (T-0204) | not run |
| ACC-16 | No secrets leak | `pytest tests/test_redaction.py tests/test_secret_redaction.py tests/test_secret_store.py` | **42 passed**. Sentinel tokens were checked across API responses, websocket frames, log lines, audit rows, the database file, and every GET route | pass (local) |
| ACC-17 | Regression tests pass | `cd dashboard && python -m pytest tests/ -q`; `cd agent && python -m pytest tests/ -q` | Dashboard: **1619 passed, 9 xfailed**; agent: **336 passed**; none skipped. 2026-09-18T06:46Z | pass |
| ACC-18 | Platform integration tests run where the infrastructure exists | Recorded per platform | None of the infrastructure exists, so none was run. The real-platform tests that could run on this machine did: the Windows Job Object cap against the real kernel (`-k ForReal`: **4 passed**), the directory runtimes' cache safety on a real disk, and both v2 pages in a real browser | not run |
| ACC-19 | Tests not run are reported as not run | This file, and the final report's list | Every "not run" row above, and the list in `2026-09-17-uniform-final-report.md` | pass |
