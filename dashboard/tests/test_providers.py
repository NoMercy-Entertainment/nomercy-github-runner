"""The provider seam, and the one property the live fleet depends on.

The eight github-runner-N containers running today were created by compose
before the nomercy.provider label existed. If provider resolution insisted on
the label, the entire existing fleet would vanish from the dashboard until it
was rebuilt - so the prefix fallback is not a nicety, it is what keeps the
current deployment visible.
"""
import providers


def test_the_two_providers_are_distinguishable():
    assert providers.GITHUB.key == "github"
    assert providers.FORGEJO.key == "forgejo"
    assert providers.GITHUB.prefix == "github-runner-"
    assert providers.FORGEJO.prefix == "forgejo-runner-"


def test_a_label_names_the_provider():
    assert providers.from_label("forgejo", "anything") is providers.FORGEJO


def test_an_unlabelled_container_falls_back_to_its_name():
    """The live fleet has no label. It must still resolve."""
    assert providers.from_label("", "github-runner-3") is providers.GITHUB
    assert providers.from_label(None, "forgejo-runner-1") is providers.FORGEJO


def test_a_label_beats_a_misleading_name():
    assert providers.from_label("forgejo", "github-runner-1") is providers.FORGEJO


def test_an_unknown_container_resolves_to_nothing():
    assert providers.from_label("", "immich-server") is None
    assert providers.by_key("gitlab") is None


def test_valid_name_is_an_allowlist_not_a_filter():
    assert providers.valid_name("github-runner-1")
    assert providers.valid_name("forgejo-runner-12")
    for bad in ("github-runner-", "github-runner-1;rm -rf /", "../etc",
                "forgejo-runner-1 ", "gitlab-runner-1", ""):
        assert not providers.valid_name(bad), bad


def test_name_for_builds_the_container_name():
    assert providers.FORGEJO.name_for(4) == "forgejo-runner-4"


def test_github_container_env_needs_no_network(monkeypatch):
    env, err = providers.GITHUB.container_env({"GH_TOKEN": "t"})
    assert err is None
    assert env["GH_TOKEN"] == "t"
    assert env["GITHUB_ORG"] == "NoMercy-Entertainment"


def test_github_container_env_ignores_a_name(monkeypatch):
    """GitHub has no use for the container name - it must not choke on one."""
    env, err = providers.GITHUB.container_env(
        {"GH_TOKEN": "t"}, "github-runner-9")
    assert err is None
    assert "FORGEJO_RUNNER_NAME" not in env


def test_forgejo_container_env_registers_under_the_container_name(monkeypatch):
    """A dashboard-created runner must register under its container name, or
    Forgejo's runner list and `docker ps` disagree about which is which - see
    the fix in docs/forgejo-runner-migration.md."""
    class _FakeForge:
        def registration_token(self):
            return "REG-1"

    monkeypatch.setattr(providers.FORGEJO, "forge_client",
                        lambda env: _FakeForge())
    env, err = providers.FORGEJO.container_env(
        {"FORGEJO_INSTANCE_URL": "https://forgejo.example"},
        "forgejo-runner-7")
    assert err is None
    assert env["FORGEJO_RUNNER_NAME"] == "forgejo-runner-7"


def test_forgejo_container_env_tolerates_no_name(monkeypatch):
    """container_env is called directly in a couple of tests without a name;
    it must degrade to an empty string rather than raise or emit "None"."""
    class _FakeForge:
        def registration_token(self):
            return "REG-1"

    monkeypatch.setattr(providers.FORGEJO, "forge_client",
                        lambda env: _FakeForge())
    env, err = providers.FORGEJO.container_env(
        {"FORGEJO_INSTANCE_URL": "https://forgejo.example"})
    assert err is None
    assert env["FORGEJO_RUNNER_NAME"] == ""
