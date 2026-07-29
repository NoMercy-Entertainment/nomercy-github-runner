import docker_ops

# Captured verbatim from `docker exec github-runner-1 docker system df
# --format json` on 2026-07-28. Do NOT hand-edit this into a shape that is
# easier to parse - an invented fixture is exactly why this bug survived.
REAL_DF = (
    '{"Active":"0","Reclaimable":"758.8MB (98%)","Size":"767.2MB","TotalCount":"4","Type":"Images"}\n'
    '{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Containers"}\n'
    '{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Local Volumes"}\n'
    '{"Active":"0","Reclaimable":"35.49GB","Size":"36.24GB","TotalCount":"310","Type":"Build Cache"}\n'
)


def test_inner_df_reads_build_cache_and_images(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, REAL_DF, ""))
    cache, images = docker_ops._inner_df("github-runner-1")
    assert cache == "36.24GB"
    assert images == "767.2MB"


def test_inner_df_returns_zeros_when_the_command_fails(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (False, "", "no such container"))
    assert docker_ops._inner_df("github-runner-9") == ("0B", "0B")


def test_inner_df_survives_unparseable_output(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, "not json", ""))
    assert docker_ops._inner_df("github-runner-1") == ("0B", "0B")


def test_inner_df_tolerates_a_missing_type(monkeypatch):
    only_images = '{"Size":"5GB","Reclaimable":"1GB","TotalCount":"2","Type":"Images"}\n'
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, only_images, ""))
    cache, images = docker_ops._inner_df("github-runner-1")
    assert cache == "0B"          # an absent type must not raise
    assert images == "5GB"


def test_inner_df_survives_scalar_json_lines(monkeypatch):
    # Bare scalars in JSON output: valid JSON but not dicts
    mixed = ('42\n'
             '{"Type":"Build Cache","Size":"10GB","Reclaimable":"2GB","TotalCount":"20"}\n'
             '"hello"\n'
             '{"Type":"Images","Size":"5GB","Reclaimable":"1GB","TotalCount":"10"}\n')
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (True, mixed, ""))
    cache, images = docker_ops._inner_df("github-runner-1")
    assert cache == "10GB"  # scalar lines are skipped, real objects are parsed
    assert images == "5GB"


def _df(cache, images):
    return (
        '{"Active":"0","Reclaimable":"0B","Size":"%s","TotalCount":"1","Type":"Images"}\n'
        '{"Active":"0","Reclaimable":"0B","Size":"%s","TotalCount":"1","Type":"Build Cache"}\n'
        % (images, cache)
    )


def test_prune_reports_what_it_freed(monkeypatch):
    """df is measured before and after, so the caller can show a real number."""
    calls, dfs = [], [_df("36.24GB", "24.8GB"), _df("0B", "1.2GB")]
    # Indexed by df-call count alone, not by len(calls): prune() also calls
    # is_idle() (its own _docker("logs", ...) call) before the first
    # measurement, which must not shift which df fixture "before" gets.
    df_calls = []

    def fake(*args, **kwargs):
        joined = " ".join(args)
        if "system" in joined and "df" in joined:
            df_calls.append(1)
            return (True, dfs[min(len(df_calls) - 1, 1)], "")
        calls.append(joined)
        return (True, "Total reclaimed space: 35GB", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    r = docker_ops.prune("github-runner-1")
    assert r["ok"] is True
    assert r["name"] == "github-runner-1"
    assert r["measured"] is True
    assert r["before"]["build_cache"] == "36.24GB"
    assert r["after"]["build_cache"] == "0B"
    assert r["freed_bytes"] > 0
    # both prunes must actually have been issued
    assert any("buildx prune" in c for c in calls)
    assert any("image prune" in c for c in calls)


def test_prune_reports_failure_without_raising(monkeypatch):
    def fake(*args, **kwargs):
        joined = " ".join(args)
        if "system" in joined and "df" in joined:
            return (True, _df("1GB", "1GB"), "")
        return (False, "", "cannot connect to the docker daemon")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    r = docker_ops.prune("github-runner-1")
    assert r["ok"] is False
    assert "cannot connect" in r["error"]


def test_prune_freed_bytes_never_negative(monkeypatch):
    """A concurrent build can grow the cache mid-prune. Report 0, not a negative."""
    dfs = [_df("1GB", "1GB"), _df("5GB", "5GB")]
    seen = []

    def fake(*args, **kwargs):
        joined = " ".join(args)
        if "system" in joined and "df" in joined:
            seen.append(1)
            return (True, dfs[min(len(seen) - 1, 1)], "")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    assert docker_ops.prune("github-runner-1")["freed_bytes"] == 0


def test_prune_stops_after_a_buildx_timeout_and_skips_image_prune(monkeypatch):
    """A killed exec client does not mean the daemon-side sweep stopped. A
    second prune now would race it, so it must not be issued."""
    calls = []

    def fake(*args, **kwargs):
        joined = " ".join(args)
        if "system" in joined and "df" in joined:
            return (True, _df("10GB", "5GB"), "")
        calls.append(joined)
        if "buildx" in joined:
            return (False, "", "timed out after 300s")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: True)
    r = docker_ops.prune("github-runner-1")
    assert r["ok"] is False
    assert r["measured"] is False
    assert r["after"] is None
    assert r["freed_bytes"] is None
    assert "timed out" in r["error"]
    assert any("buildx prune" in c for c in calls)
    assert not any("image prune" in c for c in calls)


def test_prune_failed_post_df_does_not_inflate_freed_bytes(monkeypatch):
    """If the after-measurement fails, "0B" would look like an empty runner
    and overstate everything that was reclaimed. Report unmeasured instead."""
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: True)
    df_calls = []

    def fake(*args, **kwargs):
        joined = " ".join(args)
        if "system" in joined and "df" in joined:
            df_calls.append(1)
            if len(df_calls) == 1:
                return (True, _df("10GB", "5GB"), "")
            return (False, "", "cannot connect to the docker daemon")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    r = docker_ops.prune("github-runner-1")
    assert r["measured"] is False
    assert r["freed_bytes"] is None
    assert r["before"] is not None
    assert r["after"] is None


def test_prune_failed_pre_df_does_not_inflate_freed_bytes(monkeypatch):
    """Same failure, the other side: an unmeasurable before-state must not be
    read as "0B" either."""
    monkeypatch.setattr(docker_ops, "is_idle", lambda n: True)
    df_calls = []

    def fake(*args, **kwargs):
        joined = " ".join(args)
        if "system" in joined and "df" in joined:
            df_calls.append(1)
            if len(df_calls) == 1:
                return (False, "", "cannot connect to the docker daemon")
            return (True, _df("1GB", "1GB"), "")
        return (True, "", "")

    monkeypatch.setattr(docker_ops, "_docker", fake)
    r = docker_ops.prune("github-runner-1")
    assert r["measured"] is False
    assert r["freed_bytes"] is None
    assert r["before"] is None


def test_prune_aborts_if_runner_becomes_busy_just_before_pruning(monkeypatch):
    """The route already checked is_idle once. prune() re-checks immediately
    before touching docker, narrowing (not closing) the race."""
    def explode(*a, **k):
        raise AssertionError("prune must not touch docker once busy")

    monkeypatch.setattr(docker_ops, "is_idle", lambda n: False)
    monkeypatch.setattr(docker_ops, "_docker", explode)
    r = docker_ops.prune("github-runner-1")
    assert r["ok"] is False
    assert "busy" in r["error"].lower()
    assert r["measured"] is False
    assert r["freed_bytes"] is None
