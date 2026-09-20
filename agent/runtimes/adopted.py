"""Which unit on this worker a runner already was.

Nearly every runner is made by the controller, and then its unit's name is
derived from its runner_id and nothing has to be remembered. A runner that
was already serving before the controller knew it (T-0802, MIG-4) keeps the
name it has - `github-runner-1`, not `rnr-<uuid>` - because renaming it
would lose what history keys on, and rebuilding it would interrupt a job.

The macOS appliance keeps that memory inside its guest, where the instance's
own directory is. A container has nowhere comparable: it cannot be told
anything after it is made. So the worker keeps this small map instead, one
file, read on every verb and written only when a runner is adopted or
removed.

It is deliberately dumb. A missing file, a damaged file or an unreadable one
means "nothing was adopted", which is true for every worker but one and is
never worth taking an agent down for: the derived name is then used, and a
verb against a unit that does not exist reports that it does not exist.
"""
import json
import os
import tempfile


class Adopted:
    def __init__(self, path=None):
        self.path = path or os.path.join(
            os.environ.get("AGENT_STATE", "/var/lib/runner-agent"),
            "adopted.json")

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                known = json.load(fh)
            return known if isinstance(known, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, known):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # Atomically, so an agent that stops mid-write leaves the map it had
        # rather than half of it.
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".adopted-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(known, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def name_for(self, runner_id, derived):
        """The unit this runner is: what it was adopted as, or the name its
        runner_id gives."""
        return self._read().get(str(runner_id)) or derived

    def record(self, runner_id, name):
        known = self._read()
        known[str(runner_id)] = str(name)
        self._write(known)

    def forget(self, runner_id):
        known = self._read()
        if known.pop(str(runner_id), None) is not None:
            self._write(known)

    def all(self):
        return dict(self._read())
