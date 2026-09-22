"""Which job a unit is running, read from what its runner prints.

A forge says a runner is busy; neither says with what through the API the
controller reads. The runner itself does, in its own output, and the
dashboard this platform replaces showed it from there. The cards that
replaced it said "running a job - the forge does not say which", which is
true of the forge and no help to whoever is looking at a busy fleet
(2026-09-21).

The rules are the old dashboard's, which paid for them: the last event of
either kind decides, because a "Running job" whose completion has scrolled
out of the tail would otherwise be running for ever.
"""
from agent.jobs import current_job

GITHUB = """\
√ Connected to GitHub
2026-09-20 15:17:42Z: Listening for Jobs
2026-09-20 15:17:50Z: Running job: test / coverage
"""

FORGEJO = (
    'time="2026-09-20T22:31:05Z" level=info msg="Starting runner daemon"\n'
    'time="2026-09-20T22:40:11Z" level=info msg="task 412 repo is '
    'NoMercy/app https://forgejo.example"\n')


class TestGitHub:
    def test_a_job_that_started_and_has_not_finished(self):
        assert current_job(GITHUB) == "test / coverage"

    def test_one_that_finished_is_not_running(self):
        done = GITHUB + ("2026-09-20 15:31:57Z: Job test / coverage "
                         "completed with result: Succeeded\n")
        assert current_job(done) is None

    def test_the_last_event_decides(self):
        text = GITHUB + (
            "2026-09-20 15:31:57Z: Job test / coverage completed with "
            "result: Canceled\n"
            "2026-09-20 15:41:24Z: Running job: test / test\n")
        assert current_job(text) == "test / test"


class TestForgejo:
    def test_the_task_and_its_repository(self):
        assert current_job(FORGEJO) == "task 412 - NoMercy/app"

    def test_the_last_task_is_the_one(self):
        text = FORGEJO + ('time="2026-09-20T22:55:00Z" level=info '
                          'msg="task 413 repo is NoMercy/web x"\n')
        assert current_job(text) == "task 413 - NoMercy/web"


class TestWhatIsNotThere:
    def test_a_runner_that_only_listens(self):
        assert current_job("2026-09-20 15:17:42Z: Listening for Jobs\n") \
            is None

    def test_nothing_at_all(self):
        assert current_job("") is None
        assert current_job(None) is None

    def test_a_name_is_kept_to_what_a_card_can_show(self):
        long = "Running job: " + "x" * 500
        assert len(current_job("2026-09-20 15:17:50Z: " + long)) <= 200
