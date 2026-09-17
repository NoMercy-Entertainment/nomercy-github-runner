"""What happens to a runner's docker volume when the runner goes away.

The nested engine's data root is now a named volume (see
test_runner_memory.py). That changes two flows that used to be identical:

  Recreate is remove + create under the same name, and exists to apply
  settings changes. It must keep the volume, or every settings change throws
  away the whole build cache - the opposite of what the volume was added for.

  Remove is the operator deleting a runner. Leaving tens of GB behind under a
  name nothing will ever mount again is exactly the silent disk growth this
  whole change is meant to end.

So the two paths must differ, explicitly, rather than by accident.
"""
import docker_ops
import providers


def _calls(monkeypatch):
    seen = []

    def fake(*a, **k):
        seen.append(a)
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    return seen


def test_remove_deletes_the_runners_volume(monkeypatch):
    seen = _calls(monkeypatch)
    docker_ops.remove("github-runner-7")
    flat = [" ".join(a) for a in seen]
    assert any("volume rm github-runner-7-docker" in f for f in flat), flat


def test_recreate_keeps_it(monkeypatch):
    """Otherwise 'Recreate applies saved settings changes' also means
    'and deletes your build cache'."""
    seen = _calls(monkeypatch)
    docker_ops.remove("github-runner-7", keep_data=True)
    flat = [" ".join(a) for a in seen]
    assert not any("volume rm" in f for f in flat), flat


def test_a_volume_that_will_not_delete_does_not_block_the_removal(monkeypatch):
    """Same rule the forge deregistration already follows: the operator asked
    for the container to be gone, and nothing may veto that."""
    def fake(*a, **k):
        if "volume" in a:
            return (False, "", "volume is in use")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    ok, _, _ = docker_ops.remove("github-runner-7")
    assert ok


def test_the_recreate_route_asks_to_keep_the_data():
    """Read from the route rather than mocked, so a future edit that drops the
    flag fails here instead of quietly wiping caches in production."""
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "app.py"), encoding="utf-8") as fh:
        app_src = fh.read()
    assert "ops.remove_runner(name, provider, env, keep_data=True)" in app_src


def test_removing_one_runner_from_the_page_does_not_keep_it():
    """The other side of the same coin: the Remove button is a delete."""
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "app.py"), encoding="utf-8") as fh:
        app_src = fh.read()
    single = app_src[app_src.index("ops.remove(name, provider, read_env())"):]
    assert "keep_data" not in single[:120]
