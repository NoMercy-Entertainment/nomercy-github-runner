"""Listing must cover both fleets and must not lose the unlabelled one.

`docker ps --format` exposes a single label through the `.Label` function, so
one call gets names and providers together. Containers that are neither fleet
must not be listed at all: the dashboard's action routes check membership of
this list before touching anything.
"""
import docker_ops
import providers

# Verbatim shape of `docker ps -a --format '{{.Names}}\t{{.Label "..."}}'`.
# The github rows have an empty label because the containers deployed today
# predate it; docker prints an empty field, not the literal "<no value>",
# for a label a container does not carry.
PS_OUTPUT = (
    "github-runner-1\t\n"
    "github-runner-2\t\n"
    "forgejo-runner-1\tforgejo\n"
    "runner-dashboard\t\n"
    "immich-server\t\n"
    "unrelated-box\tgithub\n"
)


def _fake_ps(out):
    def call(*args, **kwargs):
        return (True, out, "")
    return call


def test_both_fleets_are_listed(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_ps(PS_OUTPUT))
    got = docker_ops.list_runners()
    assert got == [
        ("github-runner-1", providers.GITHUB),
        ("github-runner-2", providers.GITHUB),
        ("forgejo-runner-1", providers.FORGEJO),
    ]


def test_non_runner_containers_are_excluded(monkeypatch):
    """A label alone must not be enough to enrol a container into a fleet.

    The action routes act on membership of list_runners(), so a container that
    wrongly enrols becomes a target for destructive actions. Both the name
    prefix and the label must match; a stray label on a mismatched name is
    rejected."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_ps(PS_OUTPUT))
    names = docker_ops.list_runner_names()
    assert "runner-dashboard" not in names
    assert "immich-server" not in names
    assert "unrelated-box" not in names  # has github label but not github-runner- prefix

    runners = docker_ops.list_runners()
    assert all(name != "unrelated-box" for name, _ in runners)


def test_a_failed_ps_lists_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (False, "", "daemon down"))
    assert docker_ops.list_runners() == []


def test_next_free_index_is_per_provider(monkeypatch):
    """A forgejo runner must not be pushed to index 3 because two github
    runners exist. The two fleets number independently."""
    monkeypatch.setattr(docker_ops, "_docker", _fake_ps(PS_OUTPUT))
    assert docker_ops.next_free_index(providers.GITHUB) == 3
    assert docker_ops.next_free_index(providers.FORGEJO) == 2
