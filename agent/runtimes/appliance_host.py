"""Power control of one named Docker/QEMU appliance on its Linux worker."""
import json
import re
import time

from .macos_appliance import _exec


BOOT_CLEANUP_LABEL = "nomercy.appliance_boot_cleanup"
BOOT_ENTRYPOINT = "/usr/local/bin/nomercy-appliance-start"


class DockerApplianceHost:
    def __init__(self, name, guest, docker="docker", boot_timeout=600, run=None,
                 sleep=time.sleep, clock=time.monotonic):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise ValueError("appliance name must identify one Docker unit")
        self.name = name
        self._guest = guest
        self._docker = docker
        self._timeout = boot_timeout
        self._run = run or _exec
        self._sleep, self._clock = sleep, clock

    def _inspect(self):
        ok, out, _ = self._run([self._docker, "inspect", "--type", "container",
                               "--format", "{{json .}}", self.name], timeout=15)
        try:
            data = json.loads(out) if ok else None
            return data if isinstance(data, dict) and data.get("Name", "").lstrip("/") == self.name else None
        except ValueError:
            return None

    def state(self):
        data = self._inspect()
        state = (data or {}).get("State") or {}
        if state.get("Running") and not state.get("Restarting"):
            return "running"
        if state.get("Status") in ("exited", "created"):
            return "stopped"
        return "unknown"

    def clear_boot_leftovers(self):
        """Verify the fixed pre-boot cleanup; it executes in the unit at boot.

        A stopped writable layer cannot be reached with Docker exec. The
        dedicated image entrypoint clears only the known OpenCore leftovers
        before delegating to the original QEMU startup command.
        """
        data = self._inspect()
        config = (data or {}).get("Config") or {}
        labels = config.get("Labels") or {}
        if labels.get(BOOT_CLEANUP_LABEL) != "true" or config.get("Entrypoint") != [BOOT_ENTRYPOINT]:
            raise RuntimeError("appliance lacks the verified pre-boot cleanup entrypoint; rebuild its host image")
        if not config.get("Cmd"):
            raise RuntimeError("appliance has no QEMU startup command")
        return []

    def wait_ready(self):
        deadline = self._clock() + self._timeout
        while self._clock() < deadline:
            ok, out, _ = self._guest(["/usr/bin/uname", "-s"],
                                    timeout=min(10, max(1, int(deadline - self._clock()))))
            if ok and out.strip() == "Darwin":
                return
            self._sleep(min(2, max(0, deadline - self._clock())))
        raise RuntimeError(f"macOS guest was not ready within {self._timeout}s")

    def boot(self):
        self.clear_boot_leftovers()
        state = self.state()
        if state == "unknown":
            raise RuntimeError("appliance power state is unknown; not booting blind")
        if state == "stopped":
            ok, out, err = self._run([self._docker, "start", self.name], timeout=60)
            if not ok:
                raise RuntimeError(err or out or "appliance start failed")
        self.wait_ready()
