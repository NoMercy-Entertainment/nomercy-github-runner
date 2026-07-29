import github_api

RUNNERS_PAGE = {
    "total_count": 3,
    "runners": [
        {"id": 41, "name": "nomercy-kvzz9", "status": "online", "busy": False,
         "os": "linux", "version": "2.319.0",
         "labels": [{"name": "self-hosted"}, {"name": "Linux"}]},
        {"id": 42, "name": "nomercy-ab12", "status": "offline", "busy": True,
         "os": "macos", "version": "2.318.0",
         "labels": [{"name": "X64"}]},
        {"id": 43, "name": "nomercy-nomatch", "status": "online", "busy": False,
         "os": "linux", "version": "2.319.0",
         "labels": [{"name": "self-hosted"}]},
    ],
}

GROUPS_PAGE = {
    "total_count": 2,
    "runner_groups": [
        {"id": 1, "name": "Default"},
        {"id": 3, "name": "Stoney"},
    ],
}

GROUP_1_RUNNERS = {
    "total_count": 2,
    "runners": [
        {"id": 41, "name": "nomercy-kvzz9"},
        {"id": 42, "name": "nomercy-ab12"},
    ],
}

GROUP_3_RUNNERS = {
    "total_count": 1,
    "runners": [
        {"id": 43, "name": "nomercy-nomatch"},
    ],
}


def _get_mock_with_groups(path, params=None):
    """Mock _get that dispatches on path to handle runners and groups."""
    if path == "/orgs/NoMercy-Entertainment/actions/runners":
        return RUNNERS_PAGE
    elif path == "/orgs/NoMercy-Entertainment/actions/runner-groups":
        return GROUPS_PAGE
    elif path == "/orgs/NoMercy-Entertainment/actions/runner-groups/1/runners":
        return GROUP_1_RUNNERS
    elif path == "/orgs/NoMercy-Entertainment/actions/runner-groups/3/runners":
        return GROUP_3_RUNNERS
    return None


def test_runners_flattens_labels_and_maps_os_version(monkeypatch):
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", _get_mock_with_groups)
    out = gh.runners()
    assert len(out) == 3
    assert out[0]["id"] == 41
    assert out[0]["name"] == "nomercy-kvzz9"
    assert out[0]["status"] == "online"
    assert out[0]["busy"] is False
    assert out[0]["os"] == "linux"
    assert out[0]["version"] == "2.319.0"
    assert out[0]["labels"] == ["self-hosted", "Linux"]
    assert out[1]["os"] == "macos"
    assert out[1]["version"] == "2.318.0"


def test_runners_resolves_group_names(monkeypatch):
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", _get_mock_with_groups)
    out = gh.runners()
    assert out[0]["runner_group"] == "Default"  # id 41 in group 1
    assert out[1]["runner_group"] == "Default"  # id 42 in group 1
    assert out[2]["runner_group"] == "Stoney"   # id 43 in group 3


def test_runners_unmapped_runners_get_empty_group(monkeypatch):
    """A runner not in any group gets runner_group: ''."""
    def _get_mock_partial(path, params=None):
        """Only group 1, so runner 43 (in group 3) has no group."""
        if path == "/orgs/NoMercy-Entertainment/actions/runners":
            return RUNNERS_PAGE
        elif path == "/orgs/NoMercy-Entertainment/actions/runner-groups":
            return {"total_count": 1, "runner_groups": [{"id": 1, "name": "Default"}]}
        elif path == "/orgs/NoMercy-Entertainment/actions/runner-groups/1/runners":
            return GROUP_1_RUNNERS
        return None

    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", _get_mock_partial)
    out = gh.runners()
    assert out[2]["runner_group"] == ""  # id 43 not mapped


def test_runners_graceful_when_groups_call_fails(monkeypatch):
    """If groups endpoint fails, still return runners with empty group names."""
    def _get_mock_groups_fail(path, params=None):
        if path == "/orgs/NoMercy-Entertainment/actions/runners":
            return RUNNERS_PAGE
        # groups call returns None (failure)
        return None

    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", _get_mock_groups_fail)
    out = gh.runners()
    assert len(out) == 3
    assert out[0]["runner_group"] == ""
    assert out[1]["runner_group"] == ""
    assert out[2]["runner_group"] == ""


def test_runners_returns_empty_on_runners_api_failure(monkeypatch):
    """If the runners endpoint itself fails, return empty list."""
    gh = github_api.GitHub("token", "NoMercy-Entertainment")
    monkeypatch.setattr(gh, "_get", lambda path, params=None: None)
    assert gh.runners() == []
