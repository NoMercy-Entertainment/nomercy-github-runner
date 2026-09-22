"""Swap admission is explicit and bounded; physical-only remains the default."""
from control.placement import choose

G = 1024**3


def worker(**overrides):
    caps = dict(kind="linux-container", memory_bytes=88*G, swap_bytes=640*G,
                memory_commit_bytes=728*G, memory_admission="bounded-overcommit",
                max_runners=13, capacity_valid=True)
    caps.update(overrides)
    return {"host_id": "linux", "capabilities": caps}


def runner(number, memory=32*G, combined=64*G):
    return dict(runner_id=str(number), platform="linux", host_id="linux",
                memory_limit=memory, memory_swap_limit=combined)


def test_ten_github_and_three_forgejo_fit_explicit_backed_budget():
    placed = [runner(n) for n in range(10)]
    placed += [runner(n, 6*G, 12*G) for n in range(10,12)]
    assert choose(runner(12,6*G,12*G), [worker()], placed)[0] == "linux"


def test_combined_ceiling_is_counted_not_just_physical_ceiling():
    placed = [runner(n) for n in range(2)]
    host, why = choose(runner(3), [worker(memory_commit_bytes=160*G)], placed)
    assert host is None and "not enough memory" in why


def test_swap_absence_does_not_allow_physical_overcommit():
    placed = [runner(n, combined=None) for n in range(2)]
    assert choose(runner(3,combined=None), [worker()], placed)[0] is None


def test_original_strict_physical_budget_remains_default():
    placed = [runner(n) for n in range(2)]
    assert choose(runner(3), [worker(memory_commit_bytes=None)], placed)[0] is None


def test_unbacked_commit_or_lost_swap_is_refused():
    for w in (worker(memory_commit_bytes=1000*G), worker(capacity_valid=False),
              worker(swap_bytes=0), worker(memory_bytes=16*G)):
        assert choose(runner(1), [w], [])[0] is None


def test_disk_quota_is_never_silently_ignored():
    assert choose(dict(runner(1),disk_limit=50*G), [worker()], [])[0] is None


def test_a_linux_worker_that_enforces_disk_limits_places_them():
    """The Linux runtime says `disk_limit_enforced`; Windows says both it and
    `disk_quota`. Either is the worker's promise, and without accepting the
    Linux one no Linux runner with a disk limit could be recreated
    (2026-09-22: all thirteen had one)."""
    assert choose(dict(runner(1), disk_limit=100*G),
                  [worker(disk_limit_enforced=True)], [])[0] == "linux"


def test_an_explicit_no_disk_quota_wins_over_enforcement():
    assert choose(dict(runner(1), disk_limit=100*G),
                  [worker(disk_limit_enforced=True, disk_quota=False)], [])[0] is None
