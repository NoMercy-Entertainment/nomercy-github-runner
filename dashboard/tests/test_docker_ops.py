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
