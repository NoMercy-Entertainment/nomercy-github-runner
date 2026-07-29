import app as dash


def _status(names, cpu=10.0, mem="1GiB", cache="2GB"):
    return {
        "generated": "2026-07-28T10:00:00Z",
        "runners": [{"name": n, "cpu_percent": cpu, "mem_used": mem,
                     "build_cache": cache} for n in names],
    }


def test_series_records_one_entry_per_tick():
    dash._series.clear()
    dash._record_series(_status(["github-runner-1"]))
    dash._record_series(_status(["github-runner-1"]))
    out = dash._series_for("github-runner-1")
    assert len(out) == 2
    assert out[0]["cpu"] == 10.0
    assert out[0]["mem"] > 0          # parsed to bytes
    assert out[0]["cache"] > 0


def test_series_is_bounded_at_120():
    dash._series.clear()
    for _ in range(200):
        dash._record_series(_status(["github-runner-1"]))
    assert len(dash._series_for("github-runner-1")) == 120


def test_series_drops_runners_that_disappear_from_a_live_list():
    """A runner genuinely missing from a non-empty, live status still loses
    its ring - distinct from the empty-status case below, which must not."""
    dash._series.clear()
    dash._record_series(_status(["github-runner-1", "github-runner-2"]))
    assert dash._series_for("github-runner-2")
    dash._record_series(_status(["github-runner-1"]))
    assert dash._series_for("github-runner-2") == []
    assert "github-runner-2" not in dash._series


def test_series_survives_an_empty_status_without_wiping_history():
    """An empty runner list (a transient `docker ps` failure, or "Recreate
    fleet" briefly emptying every runner at once) must not be read as every
    runner disappearing - that would discard up to ten minutes of history for
    all six of them over what is usually a blip."""
    dash._series.clear()
    dash._record_series(_status(["github-runner-1", "github-runner-2"]))
    dash._record_series({"generated": "t", "runners": []})
    assert dash._series_for("github-runner-1")
    assert dash._series_for("github-runner-2")
    assert "github-runner-1" in dash._series
    assert "github-runner-2" in dash._series


def test_series_for_unknown_runner_is_empty():
    dash._series.clear()
    assert dash._series_for("github-runner-99") == []
