import hashlib
import hmac
import json

import pytest

import webhook_jobs
from control.inventory import Inventory
from control.job_events import JobEvents
from store import schema
from store.specs import SpecStore


@pytest.fixture
def hook(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA", str(tmp_path))
    path = str(tmp_path / "control.db")
    monkeypatch.setattr(schema, "DB_PATH", path)
    schema.init(path)
    Inventory(path).register_worker("worker", "hyperv-linux")
    rid = SpecStore(path).create(provider="github", platform="linux", host_id="worker", registration_id="42")
    secret = b"x" * 64
    (tmp_path / "job-webhook-github.key").write_bytes(secret)
    payload = {"action": "completed", "repository": {"full_name": "NoMercy-Entertainment/example"},
               "workflow_job": {"id": 456, "run_id": 123, "runner_id": 42, "runner_name": "unit",
                                "name": "CI / test", "head_sha": "a" * 40,
                                "completed_at": "2026-10-11T01:01:00Z", "conclusion": "success"}}
    raw = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest(),
               "X-GitHub-Event": "workflow_job", "X-GitHub-Delivery": "delivery-1"}
    return path, rid, payload, raw, headers


def test_signed_delivery_is_durable_and_duplicate_does_not_notify_twice(hook, anon_client):
    path, rid, payload, raw, headers = hook
    first = anon_client.post("/hooks/github/jobs", data=raw, headers=headers)
    assert first.status_code == 200
    assert first.get_json()["duplicate"] is False
    assert anon_client.post("/hooks/github/jobs", data=raw, headers=headers).get_json()["duplicate"] is True
    events = JobEvents(path).after(0)
    assert len(events) == 1
    assert events[0]["repo"] == payload["repository"]["full_name"]
    assert events[0]["commit"] == "a" * 40
    assert events[0]["result"] == "success"
    assert events[0]["runner_id"] == rid


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, "sha256=é"])
def test_forged_delivery_is_rejected_before_event_storage(hook, anon_client, signature):
    path, _, _, raw, headers = hook
    headers["X-Hub-Signature-256"] = signature
    assert anon_client.post("/hooks/github/jobs", data=raw, headers=headers).status_code == 403
    assert not JobEvents(path).after(0)


def test_signature_covers_exact_body(hook, anon_client):
    path, _, _, raw, headers = hook
    assert anon_client.post("/hooks/github/jobs", data=raw + b" ", headers=headers).status_code == 403
    assert not JobEvents(path).after(0)


def test_external_runner_is_ignored_and_missing_conclusion_is_not_success(hook):
    path, _, payload, _, _ = hook
    payload["workflow_job"]["runner_id"] = 999
    assert webhook_jobs.accept("github", "delivery-2", payload, path) == {"ignored": True}
    payload["workflow_job"]["runner_id"] = 42
    payload["workflow_job"]["conclusion"] = None
    with pytest.raises(ValueError, match="actual job conclusion"):
        webhook_jobs.accept("github", "delivery-2", payload, path)
    assert not JobEvents(path).after(0)


def test_forgejo_signature_uses_its_own_key(tmp_path):
    secret = b"f" * 64
    (tmp_path / "job-webhook-forgejo.key").write_bytes(secret)
    raw = b'{"action":"completed"}'
    signature = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    assert webhook_jobs.verify("forgejo", raw, signature, tmp_path)
    assert not webhook_jobs.verify("github", raw, "sha256=" + signature, tmp_path)


def test_configuration_preserves_unrelated_org_hook_and_signing_key(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import providers
    calls = []
    class Reply:
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            import io
            return io.BytesIO(json.dumps(self.body).encode())
        def __exit__(self, *args):
            pass
    def request(req, timeout):
        calls.append((req.method, req.full_url))
        if req.method == "GET":
            return Reply([{"id": 5, "config": {"url": "https://other.example/hook"}, "events": ["*"]}])
        body = json.loads(req.data)
        assert req.method == "POST"
        assert body["config"]["secret"].encode() == (tmp_path / "job-webhook-github.key").read_bytes()
        assert body["events"] == ["workflow_job"]
        return Reply({"id": 6, "active": True, "events": ["workflow_job"]})
    monkeypatch.setattr(providers, "by_key", lambda p: SimpleNamespace(forge_client=lambda env:
        SimpleNamespace(org="OurOrg", _headers=lambda: {})))
    monkeypatch.setattr(webhook_jobs.urllib.request, "urlopen", request)
    result = webhook_jobs.configure("github", {}, "https://dashboard.example", tmp_path)
    assert result["hook_id"] == 6
    assert "secret" not in json.dumps(result)
    assert calls == [("GET", "https://api.github.com/orgs/OurOrg/hooks?per_page=100&limit=100&page=1"),
                     ("POST", "https://api.github.com/orgs/OurOrg/hooks")]


def test_forgejo_configuration_uses_api_namespace(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import providers
    import io
    calls = []
    class Reply:
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            return io.BytesIO(json.dumps(self.body).encode())
        def __exit__(self, *args):
            pass
    def request(req, timeout):
        calls.append((req.method, req.full_url))
        if req.method == "GET":
            return Reply([])
        body = json.loads(req.data)
        assert body["type"] == "forgejo"
        return Reply({"id": 7, "active": True, "events": ["workflow_job"]})
    monkeypatch.setattr(providers, "by_key", lambda p: SimpleNamespace(forge_client=lambda env:
        SimpleNamespace(base="https://forgejo.example", token="fake")))
    monkeypatch.setattr(webhook_jobs.urllib.request, "urlopen", request)
    assert webhook_jobs.configure("forgejo", {}, "https://dashboard.example", tmp_path)["hook_id"] == 7
    assert calls == [("GET", "https://forgejo.example/api/v1/user/hooks?per_page=100&limit=100&page=1"),
                     ("POST", "https://forgejo.example/api/v1/user/hooks")]


def test_unsupported_event_is_not_reported_as_configured(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import providers
    import io
    calls = []
    class Reply:
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            return io.BytesIO(json.dumps(self.body).encode())
        def __exit__(self, *args):
            pass
    def request(req, timeout):
        body = json.loads(req.data) if req.data else None
        calls.append((req.method, req.full_url, body))
        if req.method == "GET":
            return Reply([{"id": 3, "config": {"url": "https://unrelated.example/hook"}}])
        return Reply({"id": 7, "active": req.method != "PATCH", "events": []})
    monkeypatch.setattr(providers, "by_key", lambda p: SimpleNamespace(forge_client=lambda env:
        SimpleNamespace(base="https://forgejo.example", token="fake")))
    monkeypatch.setattr(webhook_jobs.urllib.request, "urlopen", request)
    with pytest.raises(ValueError, match="hook was disabled"):
        webhook_jobs.configure("forgejo", {}, "https://dashboard.example", tmp_path)
    assert calls[-1] == ("PATCH", "https://forgejo.example/api/v1/user/hooks/7", {"active": False})
