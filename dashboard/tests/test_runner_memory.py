"""A runner's memory is bounded, and none of it is a disk in disguise.

On 2026-09-15 vmmem stood at 149 GB with ~16 GB of it process memory. The rest
was page cache the runners' builds had left behind (up to 17 GB on a runner
that was idle) and tmpfs /tmp, which is RAM. No runner had a memory limit, so
nothing ever made the kernel give that cache back.

A cgroup memory limit counts page cache, so at the limit the kernel reclaims
the runner's cache instead of growing. Swap must be capped at the same value,
or the limit only moves the overflow onto the swap disk. And /tmp belongs on
the distro's disk, not in memory the limit then has to share with the build.

Every path that creates a runner - compose, the dashboard, the installer - must
agree, or recreating a runner silently undoes the policy.
"""
import os

import docker_ops
import providers

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
COMPOSE = os.path.join(ROOT, "docker-compose.runners.yml")
INSTALLER = os.path.join(ROOT, "install", "nomercy-github-runners-setup.sh")


def _created_args(monkeypatch, env):
    seen = []
    monkeypatch.setattr(docker_ops, "_docker",
                        lambda *a, **k: (seen.append(a), (True, "", ""))[1])
    ok, _, _ = docker_ops.create(1, env, providers.GITHUB)
    assert ok
    return list(seen[0])


def _value(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def test_a_memory_limit_comes_with_the_same_swap_limit(monkeypatch):
    args = _created_args(monkeypatch, {"GH_TOKEN": "t", "RUNNER_MEM_LIMIT": "16g"})
    assert _value(args, "--memory") == "16g"
    assert _value(args, "--memory-swap") == "16g"


def test_no_limit_means_no_swap_flag_either(monkeypatch):
    """--memory-swap without --memory is rejected by the daemon."""
    args = _created_args(monkeypatch, {"GH_TOKEN": "t", "RUNNER_MEM_LIMIT": "0"})
    assert "--memory" not in args
    assert "--memory-swap" not in args


def test_a_dashboard_runner_keeps_tmp_on_disk(monkeypatch):
    args = _created_args(monkeypatch, {"GH_TOKEN": "t"})
    assert "--tmpfs" not in args


def test_every_compose_runner_caps_swap_at_its_memory_limit_and_has_no_tmpfs():
    import yaml
    with open(COMPOSE, encoding="utf-8") as fh:
        services = yaml.safe_load(fh)["services"]
    runners = {n: s for n, s in services.items() if n != "dashboard"}
    assert runners
    for name, svc in runners.items():
        limit = svc["deploy"]["resources"]["limits"]["memory"]
        assert svc.get("memswap_limit") == limit, name
        assert "tmpfs" not in svc, name


def test_the_installer_caps_swap_and_keeps_tmp_on_disk():
    with open(INSTALLER, encoding="utf-8") as fh:
        code = "\n".join(l for l in fh.read().splitlines()
                         if not l.strip().startswith("#"))
    assert "--tmpfs" not in code
    assert "--memory-swap $MEM_LIMIT" in code


class TestNestedEngineGetsARealFilesystem:
    """The nested daemon's data root must not be the container's own layer.

    overlay2 cannot stack on another overlay, so a runner whose /var/lib/docker
    is the container's writable layer is stuck on fuse-overlayfs. That cost
    gigabytes of kernel slab per building runner, inflated the daemon's own
    memory, and - worst - made `docker system df` report 1.3 GB where 59 GB sat
    on disk, so the shared volume filled with nothing on the page warning about
    it. A named volume there is plain ext4 and the constraint disappears.

    Both creation paths must agree, or a dashboard-created runner silently
    behaves differently from a compose-created one.
    """

    def test_a_created_runner_gets_its_own_docker_volume(self, monkeypatch):
        args = _created_args(monkeypatch, {"GH_TOKEN": "t"})
        assert "-v" in args
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        assert any(m.endswith(":/var/lib/docker") for m in mounts), mounts

    def test_the_volume_is_named_after_the_runner(self, monkeypatch):
        """Anonymous would be wiped on every recreate, taking the cache with it
        and leaving an orphan volume behind - the bug docker rm -v exists for."""
        args = _created_args(monkeypatch, {"GH_TOKEN": "t"})
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        docker_mount = next(m for m in mounts if m.endswith(":/var/lib/docker"))
        assert docker_mount.startswith("github-runner-1-docker:"), docker_mount

    def test_every_compose_runner_has_one_too(self):
        import yaml

        with open(COMPOSE, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        for name, svc in spec["services"].items():
            if not name.startswith(("github-runner-", "forgejo-runner-")):
                continue
            mounts = svc.get("volumes") or []
            assert any(str(m).endswith(":/var/lib/docker") for m in mounts), \
                f"{name} has no volume for the nested engine: {mounts}"

    def test_those_volumes_are_declared(self):
        import yaml

        with open(COMPOSE, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        declared = set(spec.get("volumes") or {})
        for name, svc in spec["services"].items():
            for m in (svc.get("volumes") or []):
                m = str(m)
                if m.endswith(":/var/lib/docker"):
                    assert m.split(":")[0] in declared, m
