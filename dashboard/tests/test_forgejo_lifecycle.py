"""Creating and removing a Forgejo runner.

Two properties matter. A created container must carry the provider label, or
it resolves only by prefix and the label is dead weight. And a removed runner
must be deregistered, or Forgejo keeps showing a runner that no longer exists -
the same ghost the GitHub side deregisters on SIGTERM to avoid.
"""
import docker_ops
import providers


class _FakeForge:
    def __init__(self, fail_delete=False):
        self.deleted = []
        self.fail_delete = fail_delete

    def registration_token(self):
        return "REG-123"

    def runner_ids(self):
        return {"uuid-of-1": 7}

    def delete_runner(self, runner_id):
        self.deleted.append(runner_id)
        return not self.fail_delete


def _capture(monkeypatch, ok=True):
    seen = []

    def call(*args, **kwargs):
        seen.append(args)
        return (ok, "", "" if ok else "boom")

    monkeypatch.setattr(docker_ops, "_docker", call)
    return seen


def test_a_created_forgejo_runner_is_labelled(monkeypatch):
    forge = _FakeForge()
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    seen = _capture(monkeypatch)
    ok, name, _ = docker_ops.create(
        2, {"FORGEJO_INSTANCE_URL": "https://forgejo.example",
            "FORGEJO_ADMIN_TOKEN": "t"}, providers.FORGEJO)
    assert ok and name == "forgejo-runner-2"
    args = seen[0]
    assert "--label" in args
    assert f"{providers.LABEL_PROVIDER}=forgejo" in args
    assert "FORGEJO_RUNNER_REGISTRATION_TOKEN=REG-123" in args


def test_a_created_forgejo_runner_registers_under_its_container_name(monkeypatch):
    """scripts/start-forgejo.sh falls back to $(hostname) - the container ID,
    not its name - when FORGEJO_RUNNER_NAME is absent. A dashboard-created
    runner must pass it explicitly so it registers with Forgejo under the
    same name docker ps shows, matching the statically-declared
    forgejo-runner-1 in docker-compose.runners.yml."""
    forge = _FakeForge()
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    seen = _capture(monkeypatch)
    ok, name, _ = docker_ops.create(
        5, {"FORGEJO_INSTANCE_URL": "https://forgejo.example",
            "FORGEJO_ADMIN_TOKEN": "t"}, providers.FORGEJO)
    assert ok and name == "forgejo-runner-5"
    assert f"FORGEJO_RUNNER_NAME={name}" in seen[0]


def test_a_created_github_runner_is_labelled_too(monkeypatch):
    seen = _capture(monkeypatch)
    ok, name, _ = docker_ops.create(3, {"GH_TOKEN": "t"}, providers.GITHUB)
    assert ok and name == "github-runner-3"
    assert f"{providers.LABEL_PROVIDER}=github" in seen[0]


def test_create_reports_a_forge_that_will_not_mint_a_token(monkeypatch):
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)
    seen = _capture(monkeypatch)
    ok, _, err = docker_ops.create(2, {}, providers.FORGEJO)
    assert ok is False
    assert "FORGEJO" in err
    assert seen == [], "no container may be created without a token"


def test_remove_deregisters_before_deleting(monkeypatch):
    forge = _FakeForge()
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    monkeypatch.setattr(docker_ops, "_runner_file",
                        lambda n, p: {"uuid": "uuid-of-1", "id": 7})
    _capture(monkeypatch)
    ok, _, _ = docker_ops.remove("forgejo-runner-1", providers.FORGEJO, {})
    assert ok is True
    assert forge.deleted == [7]


def test_removal_proceeds_even_when_forgejo_will_not_answer(monkeypatch):
    """A container the operator wants gone must be removable regardless."""
    forge = _FakeForge(fail_delete=True)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: forge)
    monkeypatch.setattr(docker_ops, "_runner_file",
                        lambda n, p: {"uuid": "uuid-of-1", "id": 7})
    seen = _capture(monkeypatch)
    ok, _, _ = docker_ops.remove("forgejo-runner-1", providers.FORGEJO, {})
    assert ok is True
    assert any(a[0] == "rm" for a in seen), "the container must still be removed"


def test_removing_a_github_runner_asks_no_forge(monkeypatch):
    def boom(env):
        raise AssertionError("the GitHub path must not build a forge client")

    monkeypatch.setattr(providers.FORGEJO, "forge_client", boom)
    _capture(monkeypatch)
    assert docker_ops.remove("github-runner-1")[0] is True
