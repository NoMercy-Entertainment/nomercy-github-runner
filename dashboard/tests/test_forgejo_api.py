"""Shapes captured from Forgejo 16.0.2's own swagger, not invented.

Two of them are easy to get wrong by assuming they mirror GitHub:
  - /admin/actions/runners returns a BARE ARRAY, not {"runners": [...]}
  - /actions/tasks puts the tasks under "workflow_runs", despite the name
Getting either wrong yields an empty result rather than an error, which is
exactly the failure that survives review.
"""
import forgejo_api

RUNNERS = [
    {"id": 3, "uuid": "edfa80e4-9f11-4757-8626-a707af9be520",
     "name": "beaststack-runner", "status": "idle",
     "labels": ["ubuntu-latest:docker://data.forgejo.org/oci/node:lts"],
     "version": "12.0.0", "ephemeral": False},
    {"id": 4, "uuid": "aa11bb22-0000-4757-8626-000000000000",
     "name": "nomercy-forgejo-1", "status": "active",
     "labels": [], "version": "12.0.0", "ephemeral": False},
]

TASKS = {
    "total_count": 2,
    "workflow_runs": [
        {"id": 830, "name": "build", "status": "success",
         "head_branch": "main", "head_sha": "abcdef1234567890",
         "event": "push", "display_title": "Build the plugin",
         "url": "https://forgejo.example/FiLL/p/actions/runs/12",
         "run_number": 12,
         "run_started_at": "2026-08-25T14:38:55Z",
         "updated_at": "2026-08-25T14:44:02Z",
         "workflow_id": "build.yml"},
        {"id": 829, "name": "test", "status": "failure",
         "head_branch": "main", "head_sha": "0000000000000000",
         "event": "push", "display_title": "Test",
         "url": "https://forgejo.example/FiLL/p/actions/runs/11",
         "run_number": 11,
         "run_started_at": "2026-08-25T14:33:15Z",
         "updated_at": "2026-08-25T14:35:00Z",
         "workflow_id": "test.yml"},
    ],
}


def _client(routes):
    fj = forgejo_api.Forgejo("https://forgejo.example/", "tok")
    fj._get = lambda path, params=None: routes.get(path)
    return fj


def test_runner_statuses_reads_a_bare_array():
    fj = _client({"/api/v1/admin/actions/runners": RUNNERS})
    assert fj.runner_statuses() == {
        "edfa80e4-9f11-4757-8626-a707af9be520": "idle",
        "aa11bb22-0000-4757-8626-000000000000": "active",
    }


def test_a_failed_call_is_not_an_empty_fleet():
    """None and {} must stay distinguishable: {} would mark every runner
    unknown-but-answered, None says the API could not be reached."""
    fj = _client({})
    assert fj.runner_statuses() is None


def test_registration_token_is_unwrapped():
    fj = _client({"/api/v1/admin/actions/runners/registration-token":
                  {"token": "REG-123"}})
    assert fj.registration_token() == "REG-123"


def test_registration_token_survives_a_failure():
    assert _client({}).registration_token() is None


def test_find_task_matches_on_id_and_maps_to_the_runs_columns():
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    got = fj.find_task("FiLL/p", 830, "2026-08-25T14:38:55Z")
    assert got == {
        "run_id": 830,
        "repo": "FiLL/p",
        "workflow": "build.yml",
        "branch": "main",
        "sha": "abcdef12",
        "actor": None,
        "url": "https://forgejo.example/FiLL/p/actions/runs/12",
        "conclusion": "success",
        "ended_at": "2026-08-25T14:44:02Z",
    }


def test_find_task_falls_back_to_the_start_time():
    """If ActionTask.id turns out not to be the runner's task number, the
    exact start timestamp from the log still identifies the task."""
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    got = fj.find_task("FiLL/p", 999999, "2026-08-25T14:33:15Z")
    assert got["run_id"] == 829
    assert got["conclusion"] == "failure"


def test_find_task_gives_up_rather_than_guessing():
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": TASKS})
    assert fj.find_task("FiLL/p", 12345, "2020-01-01T00:00:00Z") is None


def test_a_still_running_task_is_not_reported_as_finished():
    running = {"total_count": 1, "workflow_runs": [
        dict(TASKS["workflow_runs"][0], status="running", updated_at=None)]}
    fj = _client({"/api/v1/repos/FiLL/p/actions/tasks": running})
    assert fj.find_task("FiLL/p", 830, "2026-08-25T14:38:55Z") is None
