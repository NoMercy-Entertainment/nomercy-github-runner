"""Forgejo logs a task starting and never logs it finishing.

Captured verbatim from `docker logs forgejo_runner` on 2026-08-25. The absence
of a completion line is the whole reason the Forgejo path differs: starts come
from the log, ends come from the API.
"""
import history

REAL_LOG = (
    'time="2026-08-25T14:33:15Z" level=info msg="task 829 repo is '
    'FiLL/nomercy-torrent-plugin https://data.forgejo.org http://forgejo:3000"\n'
    'time="2026-08-25T14:19:09Z" level=info msg="UpdateTask returned task '
    'result RESULT_CANCELLED for a task that was in local state '
    'RESULT_UNSPECIFIED - beginning local task termination" task_id=825\n'
    'time="2026-08-25T14:38:55Z" level=info msg="task 830 repo is '
    'FiLL/nomercy-torrent-plugin https://data.forgejo.org http://forgejo:3000"\n'
)


def test_starts_are_extracted_with_task_and_repo():
    got = history.parse_forgejo_events(REAL_LOG)
    assert got == [
        ("start", "2026-08-25T14:33:15Z", "FiLL/nomercy-torrent-plugin", 829),
        ("start", "2026-08-25T14:38:55Z", "FiLL/nomercy-torrent-plugin", 830),
    ]


def test_no_line_is_ever_read_as_an_end():
    """If a future runner version adds one, this test is where that gets
    noticed - rather than the API path silently going unused."""
    assert all(k == "start" for k, _, _, _ in
               history.parse_forgejo_events(REAL_LOG))


def test_unrelated_chatter_is_ignored():
    noise = ('time="2026-08-25T14:19:09Z" level=info msg="runner: '
             'beaststack-runner, with version: v12.0.1, with labels: [...]"\n')
    assert history.parse_forgejo_events(noise) == []


def test_a_github_log_yields_nothing_here():
    """The two parsers must not both claim the same line."""
    gh = "2026-08-20 15:02:11Z: Running job: build-base / docker-build\n"
    assert history.parse_forgejo_events(gh) == []
    assert history.parse_events(REAL_LOG) == []
