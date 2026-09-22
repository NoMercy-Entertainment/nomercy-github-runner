"""A label edit is never proof that GitHub cannot assign new jobs."""
from types import SimpleNamespace

import pytest

from control.forges import LiveForges, ForgeUnreachable
from github_api import GitHub


@pytest.mark.parametrize("group", [None, {}, {"visibility": "all"},
    {"visibility": "selected", "inherited": True}])
def test_unknown_or_shared_group_is_not_closed(monkeypatch, group):
    client = GitHub("token", "org")
    monkeypatch.setattr(client, "runner_group_id", lambda _: 7)
    monkeypatch.setattr(client, "_get", lambda *a, **k: group)
    assert not client.drain_group_closed("drain")


@pytest.mark.parametrize("repos", [None, {}, {"total_count": 1, "repositories": [{}]},
                                    {"total_count": 0, "repositories": [{}]}])
def test_nonempty_or_unknown_access_is_refused(monkeypatch, repos):
    client = GitHub("token", "org")
    monkeypatch.setattr(client, "runner_group_id", lambda _: 7)
    monkeypatch.setattr(client, "_get", lambda path, **k: repos if path.endswith(
        "/repositories") else {"visibility": "selected", "inherited": False})
    assert not client.drain_group_closed("drain")


def test_membership_requires_readback_and_supports_pagination(monkeypatch):
    client = GitHub("token", "org")
    monkeypatch.setattr(client, "runner_group_id", lambda _: 7)
    def get(path, params=None):
        if path.endswith("/repositories"):
            return {"total_count": 0, "repositories": []}
        if path.endswith("/runners"):
            return {"runners": [{"id": n} for n in range(100)] if params[
                "page"] == 1 else [{"id": 101}]}
        return {"visibility": "selected", "inherited": False}
    monkeypatch.setattr(client, "_get", get)
    assert client.drain_group_closed("drain", "101")
    assert not client.drain_group_closed("drain", "999")


def test_live_path_refuses_label_fallback_and_unconfirmed_membership():
    calls = []
    client = SimpleNamespace(drain_group_closed=lambda group, rid=None: rid is None)
    provider = SimpleNamespace(key="github", forge_client=lambda env: client,
        drain_at_forge=lambda env, spec: calls.append("moved"))
    with pytest.raises(ForgeUnreachable):
        LiveForges({}).drain(provider, {"registration_id": "41"})
    assert calls == []
    with pytest.raises(ForgeUnreachable):
        LiveForges({"GITHUB_DRAIN_GROUP": "drain"}).drain(provider,
                                                          {"registration_id": "41"})
    assert calls == ["moved"]
