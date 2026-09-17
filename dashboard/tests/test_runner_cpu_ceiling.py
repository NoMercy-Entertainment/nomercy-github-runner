"""What the dashboard calls a runner's CPU limit must be what the kernel enforces.

Two knobs cap a container's CPU and they are not interchangeable:

  --cpus (NanoCpus) is a CFS quota. It caps CPU TIME. It leaves the affinity
  mask alone, so `nproc` inside still reports every host core and a build
  running -j$(nproc) still spawns one job per host core, holding the memory
  to match. Measured on this fleet: `--cpus=8` still reported nproc=56.

  --cpuset-cpus pins the affinity mask, so `nproc` reports the width of the
  set. Measured: `--cpuset-cpus=0-7` reported nproc=8, and a nested container
  (how buildx builds actually run) reported the same, because a child cgroup's
  cpuset.cpus.effective can never exceed its parent's.

On 2026-09-17 the runners were pinned to 16-CPU sets while the page still read
NanoCpus alone, so every pinned runner was labelled "unlimited (all 56 cores)".
The comment in runner_detail.py says the point of reporting from HostConfig is
to notice a limit that was set but never applied; reading only one of the two
knobs defeated exactly that.
"""
import docker_ops


def test_a_cpuset_is_a_limit_even_with_no_quota():
    """The regression: pinned runners were reported as unlimited."""
    assert docker_ops.cpu_ceiling("0-15", 0) == 16


def test_a_wrapped_cpuset_counts_both_of_its_ranges():
    """forgejo-runner-1 is pinned to 43-55,0-2 - one set, two ranges."""
    assert docker_ops.cpu_ceiling("43-55,0-2", 0) == 16


def test_single_cpus_and_ranges_mix():
    """0, 2, and 4 through 7 - six CPUs, not seven."""
    assert docker_ops.cpu_ceiling("0,2,4-7", 0) == 6


def test_a_quota_alone_still_counts():
    assert docker_ops.cpu_ceiling("", 8_000_000_000) == 8


def test_the_lower_of_the_two_wins():
    """Both set: the container cannot exceed either one."""
    assert docker_ops.cpu_ceiling("0-15", 4_000_000_000) == 4
    assert docker_ops.cpu_ceiling("0-3", 40_000_000_000) == 4


def test_neither_set_is_unlimited_not_zero():
    """None means "no ceiling"; 0 would read as "no cores at all"."""
    assert docker_ops.cpu_ceiling("", 0) is None


def test_an_unparsable_cpuset_is_not_reported_as_zero_cores():
    """Unknown answers unknown, the discipline the rest of this module keeps.

    A spec this cannot read must not become a ceiling of nothing, which would
    render as a runner with no CPU at all.
    """
    assert docker_ops.cpu_ceiling("not-a-set", 0) is None
    assert docker_ops.cpu_ceiling("9-2", 0) is None


def test_caps_map_strips_the_leading_slash_docker_inspect_adds(monkeypatch):
    """Every other map in docker_ops is keyed on the bare container name."""
    monkeypatch.setattr(docker_ops, "_docker", lambda *a, **k: (
        True, "/github-runner-1\t0-15\t0\n/github-runner-2\t\t8000000000", ""))
    m = docker_ops._cpu_caps_map(["github-runner-1", "github-runner-2"])
    assert m["github-runner-1"] == ("0-15", 16)
    assert m["github-runner-2"] == ("", 8)


def test_caps_map_asks_once_for_the_whole_fleet(monkeypatch):
    """Per-container inspects turn a refresh into seconds of dead page."""
    calls = []
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (calls.append(a), (True, "", ""))[1])
    docker_ops._cpu_caps_map([f"github-runner-{i}" for i in range(1, 11)])
    assert len(calls) == 1


def test_caps_map_survives_a_failed_inspect(monkeypatch):
    monkeypatch.setattr(docker_ops, "_docker", lambda *a, **k: (False, "", "boom"))
    assert docker_ops._cpu_caps_map(["github-runner-1"]) == {}
