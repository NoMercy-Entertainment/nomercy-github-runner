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


def test_inspect_reports_stop_timeout(monkeypatch):
    """Lives under Config, not HostConfig - `docker inspect -f
    '{{.Config.StopTimeout}}'` is how start.sh itself reads it, and asking
    HostConfig for this key errors ("map has no entry for key
    \"StopTimeout\"")."""
    payload = json.loads(FAKE_INSPECT)
    payload[0]["Config"]["StopTimeout"] = 60
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, json.dumps(payload), "")))
    d = runner_detail.inspect("github-runner-1")["data"]
    assert d["stop_timeout"] == 60


def test_inspect_reports_stop_timeout_when_absent(monkeypatch):
    """FAKE_INSPECT's Config has no StopTimeout key at all - must return
    None, not raise, for a container inspected without one."""
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, FAKE_INSPECT, "")))
    d = runner_detail.inspect("github-runner-1")["data"]
    assert d["stop_timeout"] is None


def test_inspect_passes_through_a_low_stop_timeout_value(monkeypatch):
    """The collector only reports the raw value from Config.StopTimeout - it
    is the page's job to judge whether a given number is dangerously low, not
    this collector's. A one-off Docker Engine regression once pinned this at
    1s fleet-wide; that is fixed now, so the collector must not treat any
    particular number specially, only pass it through untouched."""
    payload = json.loads(FAKE_INSPECT)
    payload[0]["Config"]["StopTimeout"] = 1
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, json.dumps(payload), "")))
    d = runner_detail.inspect("github-runner-1")["data"]
    assert d["stop_timeout"] == 1


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
        "system": (True, '{"Type":"Build Cache","Size":"12.3GB","Reclaimable":"1GB","TotalCount":"5"}\n'
                         '{"Type":"Images","Size":"4.1GB","Reclaimable":"2GB","TotalCount":"3"}\n'
                         '{"Type":"Containers","Size":"0B","Reclaimable":"0B","TotalCount":"0"}\n'
                         '{"Type":"Local Volumes","Size":"0B","Reclaimable":"0B","TotalCount":"0"}', ""),
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
    assert d["df"]["build_cache"]["size"] == "12.3GB"
    assert d["df"]["images"]["size"] == "4.1GB"
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
            return (True, '{"Type":"Build Cache","Size":"1GB","Reclaimable":"0.1GB","TotalCount":"10"}', "")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    d = runner_detail.engine("github-runner-1")["data"]
    assert d["images"]["error"] == "daemon not responding"
    assert d["df"]["build_cache"]["size"] == "1GB"      # unaffected


def test_engine_handles_empty_output(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, "", "")))
    d = runner_detail.engine("github-runner-1")["data"]
    assert d["images"] == []
    assert d["containers"] == []


def test_engine_flags_output_it_cannot_parse_at_all(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, "WARNING: bad\nWARNING: worse\n", "")))
    d = runner_detail.engine("github-runner-1")["data"]
    assert "error" in d["images"]
    assert "2" in d["images"]["error"]


LOG_BATCH_1 = (
    "2026-07-28T10:00:01.100000000Z Runner listening for jobs\n"
    "2026-07-28T10:00:02.200000000Z Running job: build\n"
)
LOG_BATCH_2 = (
    "2026-07-28T10:00:02.200000000Z Running job: build\n"   # docker --since is
    "2026-07-28T10:00:03.300000000Z Step 1 of 4\n"          # inclusive: overlap
)


def test_logs_parses_timestamp_and_text(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_1, "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert len(d["lines"]) == 2
    assert d["lines"][0]["t"] == "2026-07-28T10:00:01.100000000Z"
    assert d["lines"][0]["text"] == "Runner listening for jobs"
    assert d["cursor"] == "2026-07-28T10:00:02.200000000Z"


def test_logs_second_call_skips_the_overlap(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_1, "")))
    first = runner_detail.logs("github-runner-1")["data"]

    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_2, "")))
    second = runner_detail.logs("github-runner-1", since=first["cursor"])["data"]

    texts = [ln["text"] for ln in second["lines"]]
    assert texts == ["Step 1 of 4"]           # no duplicate, nothing skipped
    assert second["cursor"] == "2026-07-28T10:00:03.300000000Z"


def test_logs_keeps_cursor_when_nothing_new(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, "", "")))
    d = runner_detail.logs("github-runner-1", since="2026-07-28T10:00:05Z")["data"]
    assert d["lines"] == []
    assert d["cursor"] == "2026-07-28T10:00:05Z"


def test_logs_tolerates_lines_without_a_timestamp(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, "no timestamp here\n", "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert d["lines"][0]["text"] == "no timestamp here"
    assert d["lines"][0]["t"] == ""


def test_logs_passes_a_tail_cap_alongside_since(monkeypatch):
    """`--since` alone is unbounded in the worst case (an hour-old cursor on a
    verbose runner, into one uncapped subprocess.run) - `--tail` must always
    ride along as a hard ceiling."""
    seen = {}

    def fake(*args, **kwargs):
        seen["args"] = args
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    runner_detail.logs("github-runner-1")
    args = seen["args"]
    assert "--tail" in args
    assert args[args.index("--tail") + 1] == str(runner_detail.LOG_TAIL_CAP)


def test_logs_surfaces_truncation_when_the_cap_is_hit(monkeypatch):
    """Silent truncation is the failure mode the cap must avoid - hitting it
    has to show up as a visible line, not just fewer lines than expected."""
    cap = runner_detail.LOG_TAIL_CAP
    batch = "\n".join(
        f"2026-07-28T10:00:00.{i:09d}Z line {i}" for i in range(cap)
    ) + "\n"
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, batch, "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert "truncat" in d["lines"][0]["text"].lower()


def test_logs_does_not_flag_truncation_under_the_cap(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_1, "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert not any("truncat" in ln["text"].lower() for ln in d["lines"])


def test_logs_redacts_the_configured_gh_token(monkeypatch, tmp_path):
    """logs() proxies container stdout, and start.sh is only convention away
    from ever echoing the token - this must hold even if that convention
    breaks (a stray `set -x`, for instance)."""
    token = "ghp_abcdefghijklmnopqrstuvwxyz1234"
    env_file = tmp_path / ".env"
    env_file.write_text(f"GH_TOKEN={token}\nGITHUB_ORG=NoMercy-Entertainment\n")
    monkeypatch.setattr(runner_detail, "ENV_PATH", str(env_file))
    line = f"2026-07-28T10:00:01.000000000Z using token {token} to deregister\n"
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, line, "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert token not in d["lines"][0]["text"]
    assert token not in json.dumps(d)


def test_logs_do_nothing_when_no_token_is_configured(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GITHUB_ORG=NoMercy-Entertainment\n")
    monkeypatch.setattr(runner_detail, "ENV_PATH", str(env_file))
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((True, LOG_BATCH_1, "")))
    d = runner_detail.logs("github-runner-1")["data"]
    assert d["lines"][0]["text"] == "Runner listening for jobs"


def test_logs_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((False, "", "No such container")))
    r = runner_detail.logs("github-runner-9")
    assert r["ok"] is False


def test_logs_does_not_redeliver_an_unstamped_line(monkeypatch):
    batch = ("2026-07-28T10:00:01.100000000Z Traceback:\n"
             "    File \"x.py\", line 1\n")
    monkeypatch.setattr(docker_ops, "_docker", _fake_docker((True, batch, "")))
    first = runner_detail.logs("github-runner-1")["data"]
    assert len(first["lines"]) == 2               # both delivered once

    # docker --since is inclusive, so the same entry comes back next poll
    second = runner_detail.logs("github-runner-1", since=first["cursor"])["data"]
    assert second["lines"] == []                  # neither line repeats


def test_cached_returns_the_same_value_within_ttl():
    runner_detail._cache.clear()
    calls = []

    def fn():
        calls.append(1)
        return {"ok": True, "data": len(calls)}

    assert runner_detail.cached("k", 60, fn) == runner_detail.cached("k", 60, fn)
    assert len(calls) == 1


def test_cached_refetches_after_ttl(monkeypatch):
    runner_detail._cache.clear()
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(runner_detail.time, "monotonic", lambda: clock[0])

    def fn():
        calls.append(1)
        return len(calls)

    runner_detail.cached("k", 10, fn)
    clock[0] = 1005.0
    runner_detail.cached("k", 10, fn)
    assert len(calls) == 1          # still inside the window
    clock[0] = 1011.0
    runner_detail.cached("k", 10, fn)
    assert len(calls) == 2          # window expired


def test_cached_keys_do_not_collide():
    runner_detail._cache.clear()
    runner_detail.cached("a", 60, lambda: 1)
    runner_detail.cached("b", 60, lambda: 2)
    assert runner_detail.cached("a", 60, lambda: 99) == 1
    assert runner_detail.cached("b", 60, lambda: 99) == 2


def test_cached_does_not_store_a_none_result():
    """None means the collector could not get an answer at all - caching it
    would replay a transient outage as though it were the truth."""
    runner_detail._cache.clear()
    calls = []

    def fn():
        calls.append(1)
        return None

    runner_detail.cached("k", 60, fn)
    runner_detail.cached("k", 60, fn)
    assert len(calls) == 2


def test_cached_does_not_store_a_failure_result():
    runner_detail._cache.clear()
    calls = []

    def fn():
        calls.append(1)
        return {"ok": False, "error": "boom"}

    runner_detail.cached("k", 60, fn)
    runner_detail.cached("k", 60, fn)
    assert len(calls) == 2


def test_cached_still_stores_and_replays_a_successful_result():
    runner_detail._cache.clear()
    calls = []

    def fn():
        calls.append(1)
        return {"ok": True, "data": "fine"}

    runner_detail.cached("k", 60, fn)
    runner_detail.cached("k", 60, fn)
    assert len(calls) == 1


REAL_DF_LINES = (
    '{"Active":"0","Reclaimable":"758.8MB (98%)","Size":"767.2MB","TotalCount":"4","Type":"Images"}\n'
    '{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Containers"}\n'
    '{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Local Volumes"}\n'
    '{"Active":"0","Reclaimable":"35.49GB","Size":"36.24GB","TotalCount":"310","Type":"Build Cache"}\n'
)


def test_engine_df_is_keyed_by_resource_type(monkeypatch):
    def fake(*args, **kwargs):
        if " system" in " " + " ".join(args):
            return (True, REAL_DF_LINES, "")
        return (True, "", "")
    monkeypatch.setattr(docker_ops, "_docker", fake)
    df = runner_detail.engine("github-runner-1")["data"]["df"]
    assert df["build_cache"]["size"] == "36.24GB"
    assert df["build_cache"]["reclaimable"] == "35.49GB"
    assert df["build_cache"]["count"] == "310"
    assert df["images"]["size"] == "767.2MB"
    assert df["containers"]["size"] == "0B"
    assert df["volumes"]["size"] == "0B"


def test_engine_df_still_reports_command_failure(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        _fake_docker((False, "", "daemon gone")))
    df = runner_detail.engine("github-runner-1")["data"]["df"]
    assert "error" in df


def test_engine_df_survives_scalar_json_lines(monkeypatch):
    # Bare scalars in JSON output: valid JSON but not dicts
    mixed_df = ('42\n'
                '{"Type":"Build Cache","Size":"10GB","Reclaimable":"2GB","TotalCount":"20"}\n'
                '"hello"\n'
                '{"Type":"Images","Size":"5GB","Reclaimable":"1GB","TotalCount":"10"}\n')

    def fake(*args, **kwargs):
        if " system" in " " + " ".join(args):
            return (True, mixed_df, "")
        return (True, "", "")
    monkeypatch.setattr(docker_ops, "_docker", fake)
    df = runner_detail.engine("github-runner-1")["data"]["df"]
    assert df["build_cache"]["size"] == "10GB"  # scalar lines are skipped
    assert df["images"]["size"] == "5GB"


def test_exec_json_lines_survives_scalar_json_lines(monkeypatch):
    # Bare scalars in JSON output: valid JSON but not dicts
    mixed_images = ('42\n'
                    '{"Repository":"alpine","Tag":"3.19","Size":"7.8MB"}\n'
                    '"hello"\n'
                    '{"Repository":"node","Tag":"20","Size":"1.1GB"}\n')

    def fake(*args, **kwargs):
        if " images" in " " + " ".join(args):
            return (True, mixed_images, "")
        return (True, "", "")
    monkeypatch.setattr(docker_ops, "_docker", fake)
    images = runner_detail.engine("github-runner-1")["data"]["images"]
    # Scalar lines skipped, only dict objects returned
    assert len(images) == 2
    assert images[0]["Repository"] == "alpine"
    assert images[1]["Repository"] == "node"
