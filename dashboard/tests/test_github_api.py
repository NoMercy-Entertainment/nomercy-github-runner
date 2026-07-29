import github_api

PAGE = {
    "total_count": 2,
    "runners": [
        {"id": 41, "name": "nomercy-kvzz9", "status": "online", "busy": False,
         "runner_group_name": "Default",
         "labels": [{"name": "self-hosted"}, {"name": "Linux"}]},
        {"id": 42, "name": "nomercy-ab12", "status": "offline", "busy": True,
         "runner_group_name": "lent",
         "labels": [{"name": "X64"}]},
    ],
}


def test_runners_flattens_labels_and_group(monkeypatch):
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", lambda path, params=None: PAGE)
    out = gh.runners()
    assert len(out) == 2
    assert out[0]["id"] == 41
    assert out[0]["name"] == "nomercy-kvzz9"
    assert out[0]["status"] == "online"
    assert out[0]["busy"] is False
    assert out[0]["labels"] == ["self-hosted", "Linux"]
    assert out[0]["runner_group"] == "Default"
    assert out[1]["busy"] is True


def test_runners_returns_empty_on_api_failure(monkeypatch):
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", lambda path, params=None: None)
    assert gh.runners() == []
