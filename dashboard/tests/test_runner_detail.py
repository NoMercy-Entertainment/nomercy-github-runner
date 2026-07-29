import json

import docker_ops
import runner_detail

FAKE_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz1234"

FAKE_INSPECT = json.dumps([{
    "Id": "abc123def456",
    "Created": "2026-07-28T10:00:00.000000000Z",
    "Name": "/github-runner-1",
    "RestartCount": 2,
    "Image": "sha256:deadbeefcafe",
    "State": {
        "Status": "running",
        "ExitCode": 0,
        "Pid": 1234,
        "StartedAt": "2026-07-28T10:00:01.000000000Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
    },
    "Config": {
        "Image": "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest",
        "Env": [f"GH_TOKEN={FAKE_TOKEN}", "GITHUB_ORG=NoMercy-Entertainment"],
    },
    "HostConfig": {
        "NanoCpus": 4_000_000_000,
        "Memory": 17_179_869_184,
        "RestartPolicy": {"Name": "unless-stopped"},
        "Privileged": True,
        "NetworkMode": "bridge",
    },
    "Mounts": [{
        "Source": "/mnt/d/docker-compose/GithubRunners/scripts/start.sh",
        "Destination": "/root/start.sh",
        "Mode": "ro",
        "RW": False,
    }],
    "NetworkSettings": {"IPAddress": "172.17.0.2"},
}])


def _fake_docker(result):
    """Replace docker_ops._docker with something that returns a canned result."""
    def fake(*args, **kwargs):
        return result
    return fake


def test_inspect_parses_the_fields(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, FAKE_INSPECT, "")))
    r = runner_detail.inspect("github-runner-1")
    assert r["ok"] is True
    d = r["data"]
    assert d["image"] == "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest"
    assert d["digest"] == "sha256:deadbeefcafe"
    assert d["restart_count"] == 2
    assert d["status"] == "running"
    assert d["pid"] == 1234
    assert d["cpu_limit"] == 4.0
    assert d["mem_limit_bytes"] == 17_179_869_184
    assert d["restart_policy"] == "unless-stopped"
    assert d["privileged"] is True
    assert d["ip"] == "172.17.0.2"
    assert d["mounts"][0]["destination"] == "/root/start.sh"
    assert d["mounts"][0]["rw"] is False


def test_inspect_masks_the_token_out_of_the_payload(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, FAKE_INSPECT, "")))
    r = runner_detail.inspect("github-runner-1")
    # The whole serialized response, not just the env dict: this is the check
    # that matters, because anything reachable in JSON reaches the browser.
    assert FAKE_TOKEN not in json.dumps(r)
    assert r["data"]["env"]["GH_TOKEN"].startswith("ghp_")
    assert r["data"]["env"]["GITHUB_ORG"] == "NoMercy-Entertainment"


def test_inspect_reports_unlimited_as_none(monkeypatch):
    payload = json.loads(FAKE_INSPECT)
    payload[0]["HostConfig"]["NanoCpus"] = 0
    payload[0]["HostConfig"]["Memory"] = 0
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, json.dumps(payload), "")))
    d = runner_detail.inspect("github-runner-1")["data"]
    assert d["cpu_limit"] is None
    assert d["mem_limit_bytes"] is None


def test_inspect_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((False, "", "No such object")))
    r = runner_detail.inspect("github-runner-9")
    assert r["ok"] is False
    assert "No such object" in r["error"]


def test_inspect_survives_malformed_json(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, "not json at all", "")))
    r = runner_detail.inspect("github-runner-1")
    assert r["ok"] is False


def test_engine_collects_all_four_sections(monkeypatch):
    responses = {
        "system": (True, '{"BuildCache":"12.3GB","Images":"4.1GB",'
                         '"Containers":"0B","Volumes":"0B"}', ""),
        "images": (True, '{"Repository":"alpine","Tag":"3.19","Size":"7.8MB"}\n'
                         '{"Repository":"node","Tag":"20","Size":"1.1GB"}', ""),
        "ps": (True, '{"Names":"buildx_buildkit","Status":"Up 2 hours"}', ""),
        "volume": (True, '{"Name":"cache-vol","Driver":"local"}', ""),
    }

    def fake(*args, **kwargs):
        joined = " ".join(args)
        for key, resp in responses.items():
            if f" {key}" in f" {joined}":
                return resp
        return (False, "", "unexpected call: " + joined)

    monkeypatch.setattr(docker_ops, "_docker", fake)
    r = runner_detail.engine("github-runner-1")
    assert r["ok"] is True
    d = r["data"]
    assert d["df"]["BuildCache"] == "12.3GB"
    assert len(d["images"]) == 2
    assert d["images"][1]["Repository"] == "node"
    assert d["containers"][0]["Names"] == "buildx_buildkit"
    assert d["volumes"][0]["Name"] == "cache-vol"


def test_engine_degrades_one_section_at_a_time(monkeypatch):
    def fake(*args, **kwargs):
        joined = " ".join(args)
        if " images" in f" {joined}":
            return (False, "", "daemon not responding")
        if " system" in f" {joined}":
            return (True, '{"BuildCache":"1GB"}', "")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    d = runner_detail.engine("github-runner-1")["data"]
    assert d["images"]["error"] == "daemon not responding"
    assert d["df"]["BuildCache"] == "1GB"      # unaffected


def test_engine_handles_empty_output(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, "", "")))
    d = runner_detail.engine("github-runner-1")["data"]
    assert d["images"] == []
    assert d["containers"] == []
