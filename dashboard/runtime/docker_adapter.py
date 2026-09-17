"""The Docker runtime: Linux runner containers on a container engine.

This is where the `docker` argv lives. Nothing else in the dashboard builds
one, and a test asserts that, because a second place that shells out to the
engine is a second lifecycle - which is exactly what the uniform platform
design forbids.

**What is deliberately NOT here.** `docker_ops.create()` and `remove()` also do
provider work: they ask a provider for a container environment, mint a Forgejo
registration token, and delete a Forgejo runner record. None of that moved.
A runtime adapter that imported `providers` would break the property T-0001
asserts - that a runtime never learns which forge a runner belongs to - and
that property is the whole reason the seam exists. So this module takes a
fully resolved spec and gives back a handle; deciding what goes into the spec
stays with the caller, and moves to the controller in phase 2.

**How `_docker` is reached, and why it matters.** The existing tests patch
`docker_ops._docker` and expect the patch to take effect. If this module
imported that name at load time it would capture the original function and
every one of those tests would silently stop testing anything. It therefore
resolves the call through the module at call time, with a late import that
also avoids the circular dependency. An injected `run` is the seam the new
tests use directly.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .base import (
    Capabilities,
    ExecUnitKind,
    ExecUnitRef,
    ExecUnitStatus,
    Freed,
    Probe,
    ProbeResult,
    Telemetry,
)


class DockerRuntimeAdapter:
    """One Linux container per runner instance, on one engine."""

    kind = ExecUnitKind.LINUX_CONTAINER

    def __init__(self, run=None, run_logs=None):
        self._run_override = run
        self._run_logs_override = run_logs

    # ---- the engine call, resolved late -----------------------------------

    def _docker(self, *args, **kwargs):
        if self._run_override is not None:
            return self._run_override(*args, **kwargs)
        import docker_ops  # late: breaks the cycle, and honours monkeypatching
        return docker_ops._docker(*args, **kwargs)

    def _docker_logs(self, *args, **kwargs):
        """Reads a CONTAINER's own output, so stdout and stderr are merged.

        The GitHub runner writes its log to stdout and forgejo-runner writes
        its log to stderr. Reading only stdout is correct for one and silently
        empty for the other.
        """
        if self._run_logs_override is not None:
            return self._run_logs_override(*args, **kwargs)
        import docker_ops
        return docker_ops._docker_logs(*args, **kwargs)

    # ---- lifecycle ---------------------------------------------------------

    def create(self, spec: Mapping[str, Any]) -> ExecUnitRef:
        """Create the container from an already-resolved spec.

        `spec` carries only runtime facts: name, image, labels, env, mounts and
        limits. Which forge the runner belongs to, and what its registration
        token is, are not this module's business.
        """
        name = spec["name"]
        args = [
            "run", "-d",
            "--name", name,
            "--privileged",
            "--restart", spec.get("restart", "unless-stopped"),
            # 60s, not the engine default: start.sh deregisters the runner on
            # SIGTERM and needs a few seconds. Docker Engine 29.x creates
            # containers with StopTimeout=1, which kills deregistration
            # mid-flight and orphans the registration.
            "--stop-timeout", str(spec.get("stop_timeout", 60)),
        ]
        for k, v in (spec.get("labels") or {}).items():
            args += ["--label", f"{k}={v}"]
        for m in (spec.get("mounts") or []):
            args += ["-v", m]
        for k, v in (spec.get("env") or {}).items():
            args += ["-e", f"{k}={v}"]

        cpus = (spec.get("cpus") or "0")
        mem = (spec.get("memory") or "0")
        if str(cpus).strip() not in ("", "0"):
            args += ["--cpus", str(cpus)]
        if str(mem).strip() not in ("", "0"):
            # Swap capped at the limit itself: the limit is what makes the
            # kernel reclaim a runner's page cache, and with swap on top it
            # would page the overflow out to disk instead.
            args += ["--memory", str(mem), "--memory-swap", str(mem)]
        if spec.get("cpuset"):
            args += ["--cpuset-cpus", str(spec["cpuset"])]
        args.append(spec["image"])

        ok, out, err = self._docker(*args, timeout=180)
        if not ok:
            raise RuntimeError(err or out or "docker run failed")
        return ExecUnitRef(kind=self.kind, handle=name)

    def start(self, ref: ExecUnitRef) -> None:
        ok, out, err = self._docker("start", ref.handle)
        if not ok:
            raise RuntimeError(err or out)

    def stop(self, ref: ExecUnitRef, timeout: int = 60) -> None:
        """SIGTERM with a grace period long enough to deregister."""
        ok, out, err = self._docker("stop", "-t", str(timeout), ref.handle,
                                    timeout=timeout + 20)
        if not ok:
            raise RuntimeError(err or out)

    def restart(self, ref: ExecUnitRef, timeout: int = 60) -> None:
        ok, out, err = self._docker("restart", "-t", str(timeout), ref.handle,
                                    timeout=timeout + 30)
        if not ok:
            raise RuntimeError(err or out)

    def remove(self, ref: ExecUnitRef, keep_data: bool = False) -> None:
        """Remove the container, and its data volume unless asked to keep it.

        The volume name is read from the container BEFORE removal - afterwards
        there is nothing left to ask. Reconstructing it would be wrong:
        compose names the same thing `<project>_<runner>-docker` while a
        dashboard-created one is `<runner>-docker`, so a guess leaves every
        compose-created runner's volume behind.

        `-v` removes only ANONYMOUS volumes, never named ones, which is why
        the named volume needs this separate step at all.

        180s: these containers hold a nested daemon, and tearing that down on
        removal has been observed to take ~110s. A timeout here reads to the
        caller as "removal failed" even when it eventually succeeds.
        """
        data_volume = ""
        if not keep_data:
            _, vol, _ = self._docker(
                "inspect", "--format",
                '{{range .Mounts}}{{if eq .Destination "/var/lib/docker"}}'
                '{{.Name}}{{end}}{{end}}',
                ref.handle, timeout=30)
            data_volume = (vol or "").strip()

        ok, out, err = self._docker("rm", "-f", "-v", ref.handle, timeout=180)

        if data_volume:
            vol_ok, _, vol_err = self._docker("volume", "rm", data_volume,
                                              timeout=60)
            if not vol_ok and vol_err:
                # Never blocks the removal, for the same reason a failed forge
                # deregistration does not: the operator asked for the container
                # to be gone, and nothing gets to veto that.
                print(f"[volume:{ref.handle}] {data_volume} not removed: "
                      f"{vol_err.strip()[:200]}")
        if not ok:
            raise RuntimeError(err or out)

    # ---- observation -------------------------------------------------------

    def status(self, ref: ExecUnitRef) -> ExecUnitStatus:
        ok, out, _ = self._docker(
            "inspect", "--format",
            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.StartedAt}}"
            "|{{.RestartCount}}",
            ref.handle, timeout=30)
        if not ok or not (out or "").strip():
            return ExecUnitStatus(exists=False, running=False,
                                  message="not found")
        parts = (out.strip().split("|") + ["", "", "", ""])[:4]
        try:
            code = int(parts[1])
        except ValueError:
            code = None
        try:
            restarts = int(parts[3])
        except ValueError:
            restarts = 0
        return ExecUnitStatus(exists=True, running=parts[0] == "running",
                              exit_code=code, started_at=parts[2] or None,
                              restart_count=restarts, message=parts[0])

    def telemetry(self, ref: ExecUnitRef) -> Telemetry:
        """One sample. Every field stays None when it could not be read.

        `docker stats` reports CPU as a percentage of ONE core, so the
        denominator matters: a cpuset narrows what the container may use, and
        the host's core count would imply headroom it cannot reach.
        """
        ok, out, _ = self._docker(
            "stats", "--no-stream", "--format", "{{.CPUPerc}}\t{{.MemUsage}}",
            ref.handle, timeout=25)
        cpu = mem_used = mem_limit = None
        if ok and (out or "").strip():
            parts = out.strip().split("\t")
            if len(parts) >= 1:
                try:
                    cpu = float(parts[0].replace("%", "").strip())
                except ValueError:
                    cpu = None
            if len(parts) >= 2:
                used, _, limit = parts[1].partition("/")
                mem_used = _to_bytes(used)
                mem_limit = _to_bytes(limit)
        return Telemetry(cpu_percent=cpu, cpu_cores=self._cores(ref),
                         mem_used_bytes=mem_used, mem_limit_bytes=mem_limit)

    def _cores(self, ref: ExecUnitRef) -> Optional[float]:
        ok, out, _ = self._docker(
            "inspect", "--format",
            "{{.HostConfig.CpusetCpus}}\t{{.HostConfig.NanoCpus}}",
            ref.handle, timeout=30)
        if not ok or not (out or "").strip():
            return None
        import docker_ops
        cpuset, _, nano = out.strip().partition("\t")
        try:
            nano_i = int(nano)
        except ValueError:
            nano_i = 0
        return docker_ops.cpu_ceiling(cpuset, nano_i)

    def logs(self, ref: ExecUnitRef, since_seconds: int = 45) -> str:
        ok, out, _ = self._docker_logs(
            "logs", "--since", f"{since_seconds}s", ref.handle, timeout=20)
        return out if ok else ""

    def exec_probe(self, ref: ExecUnitRef, probe: Probe) -> ProbeResult:
        """Answer one closed question by asking the nested daemon.

        Not a command channel: the mapping below is the whole of what can be
        asked, and it is fixed at import time.
        """
        if probe in (Probe.DISK_USAGE, Probe.CACHE_SIZE):
            import docker_ops
            rows = docker_ops._df_rows(ref.handle)
            if rows is None:
                return ProbeResult(probe=probe, ok=False,
                                   error="docker system df did not answer")
            key = "Images" if probe is Probe.DISK_USAGE else "Build Cache"
            return ProbeResult(probe=probe, ok=True,
                               value=rows.get(key, {}).get("Size", "0B"))
        if probe is Probe.AGENT_VERSION:
            ok, out, err = self._docker(
                "exec", ref.handle, "cat", "/root/actions-runner/.runner_version",
                timeout=15)
            return ProbeResult(probe=probe, ok=ok,
                               value=(out or "").strip() if ok else None,
                               error="" if ok else (err or "").strip())
        if probe is Probe.JOB_STATE:
            return ProbeResult(
                probe=probe, ok=False,
                error="job state is authoritative at the forge, not here")
        return ProbeResult(probe=probe, ok=False, error="unknown probe")

    # ---- cache -------------------------------------------------------------

    def clear_cache(self, ref: ExecUnitRef,
                    policy: Mapping[str, Any]) -> Freed:
        """Reclaim build cache and unused images inside this runner only.

        Both prunes are attempted even when the first fails - they are
        independent, and reclaiming one of the two is better than neither -
        unless the buildx prune itself timed out, in which case issuing a
        second one while the daemon is still sweeping makes things worse.

        Nothing here names a path outside this container.
        """
        timeout = int(policy.get("timeout", 300))
        per_scope: dict[str, int] = {}
        errors: dict[str, str] = {}

        import docker_ops
        before = docker_ops._df_sizes(docker_ops._df_rows(ref.handle))

        ok1, _, err1 = self._docker("exec", ref.handle, "docker", "buildx",
                                    "prune", "-af", timeout=timeout)
        if not ok1:
            errors["engine-build-cache"] = (err1 or "").strip()[:200]
            if "timed out" in (err1 or ""):
                return Freed(per_scope=per_scope, errors=errors, total_bytes=0)

        ok2, _, err2 = self._docker("exec", ref.handle, "docker", "image",
                                    "prune", "-af", timeout=timeout)
        if not ok2:
            errors["engine-images-unused"] = (err2 or "").strip()[:200]

        after = docker_ops._df_sizes(docker_ops._df_rows(ref.handle))
        total = 0
        if before is not None and after is not None:
            total = max(0, (before or 0) - (after or 0))
            per_scope["engine-build-cache"] = total
        return Freed(per_scope=per_scope, errors=errors, total_bytes=total)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=self.kind,
            job_containers=True,
            nested_builds=True,
            resettable_os=False,
            supports_drain=True,
            notes="one container per instance, own volumes for docker data, "
                  "workspace and cache",
        )


_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
          "TIB": 1024 ** 4, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}


def _to_bytes(s: str) -> Optional[int]:
    """`docker stats` sizes. None when unparsable, never 0."""
    import re
    m = re.match(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*$", s or "")
    if not m:
        return None
    unit = _UNITS.get(m.group(2).upper())
    if unit is None:
        return None
    return int(float(m.group(1)) * unit)
