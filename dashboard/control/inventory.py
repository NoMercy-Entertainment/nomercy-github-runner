"""The workers runners are placed on, and whether they can be trusted today.

A worker is a machine that hosts execution units: a Hyper-V Linux VM, a Windows
Server guest, and - in this design - the same Linux VM again when it hosts the
macOS appliance. There is deliberately no `apple-host` kind. The appliance is an
execution unit of the Linux worker, not a worker of its own, because inventing a
second worker kind for it would be the first place "uniform" quietly stopped
being true.

**Health is measured, not declared.** A worker is healthy while its heartbeats
keep arriving and degraded once three are missed. Degraded is not a label for a
dashboard; it is a gate. No destructive verb is dispatched to a degraded worker,
because "I cannot reach it" and "it is not there" look identical from here, and
removing a runner on that evidence deletes capacity that was only unreachable.

**A worker going away does not remove its runners.** They are marked stale and
left alone. They come back.
"""
import json
from datetime import datetime, timedelta, timezone

from store import schema

#: What a worker is. Named for what they are; a guest is never called a
#: container (CON-7).
HYPERV_LINUX = "hyperv-linux"
HYPERV_WINDOWS = "hyperv-windows"
KINDS = (HYPERV_LINUX, HYPERV_WINDOWS)

HEALTHY, DEGRADED, UNKNOWN = "healthy", "degraded", "unknown"

#: Spec 17.2: a heartbeat every 5s, no retry. Three missed beats is the point
#: at which a pause becomes an absence - long enough not to trip on one slow
#: sample, short enough that an operator is not acting on stale information.
HEARTBEAT_SECONDS = 5
MISSED_BEATS_BEFORE_DEGRADED = 3


#: The key in `workers.capabilities` that holds which verbs the controller may
#: send this worker. Policy, not a fact about the worker - so only `permit()`
#: writes it. Everything else in that column is what the agent declares about
#: itself, and a heartbeat replaces those keys freely; if a heartbeat could
#: write this one too, an agent could grant itself any verb it liked.
PERMITTED = "verbs"

#: Always allowed, because they are how a worker is learned about: who it is,
#: and what it will serve. Neither changes anything.
DISCOVERY = frozenset({"hello", "capabilities"})


class UnknownWorker(Exception):
    pass


def _declared(stored, declared):
    """The capabilities column after the agent declares `declared`.

    The agent's own keys are replaced wholesale - it is the authority on what
    it is - and the controller's policy key is carried over untouched. A
    `verbs` key in what the agent sent is dropped, not merged.
    """
    current = json.loads(stored) if stored else {}
    merged = {k: v for k, v in (declared or {}).items() if k != PERMITTED}
    if PERMITTED in current:
        merged[PERMITTED] = current[PERMITTED]
    return json.dumps(merged)


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _decode(row):
    if row is None:
        return None
    worker = dict(row)
    if worker.get("capabilities"):
        try:
            worker["capabilities"] = json.loads(worker["capabilities"])
        except (ValueError, TypeError):
            pass
    return worker


class Inventory:
    def __init__(self, path=None):
        self.path = path or schema.DB_PATH

    def _conn(self):
        return schema.connect(self.path)

    def register_worker(self, host_id, kind, display_name=None, endpoint=None,
                        agent_version=None, capabilities=None,
                        certificate_fingerprint=None):
        """Add a worker, or update one that is already here.

        Idempotent on `host_id`, because an agent re-announces itself after
        every restart and a second row for the same machine would let the
        scheduler place two runners where there is room for one.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown worker kind {kind!r}; expected {KINDS}")

        with self._conn() as c:
            existing = c.execute(
                "SELECT capabilities FROM workers WHERE host_id = ?",
                (host_id,)).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO workers (host_id, display_name, kind,"
                    " endpoint, agent_version, capabilities,"
                    " certificate_fingerprint, state, last_seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (host_id, display_name or host_id, kind, endpoint,
                     agent_version, _declared(None, capabilities),
                     certificate_fingerprint, UNKNOWN, None))
            else:
                # Re-announcing never widens what the worker may be asked to
                # do, and omitting capabilities leaves them as they were
                # rather than wiping them.
                caps = (existing["capabilities"] if capabilities is None
                        else _declared(existing["capabilities"], capabilities))
                c.execute(
                    "UPDATE workers SET display_name = ?, kind = ?,"
                    " endpoint = ?, agent_version = ?, capabilities = ?,"
                    " certificate_fingerprint = ? WHERE host_id = ?",
                    (display_name or host_id, kind, endpoint, agent_version,
                     caps, certificate_fingerprint, host_id))
        return host_id

    def permit(self, host_id, verbs):
        """Set which verbs the controller may send this worker. Policy.

        The only writer of the `verbs` key. Replaces the list rather than
        adding to it, so what a worker may be asked for is always one
        statement an operator made, not the sum of every grant ever issued.
        """
        from .agent_client import VERB_NAMES
        verbs = set(verbs)
        unknown = sorted(verbs - VERB_NAMES)
        if unknown:
            raise ValueError(f"not protocol verbs: {unknown}")
        with self._conn() as c:
            row = c.execute(
                "SELECT capabilities FROM workers WHERE host_id = ?",
                (host_id,)).fetchone()
            if row is None:
                raise UnknownWorker(host_id)
            caps = json.loads(row["capabilities"]) if row["capabilities"] \
                else {}
            caps[PERMITTED] = sorted(verbs)
            c.execute("UPDATE workers SET capabilities = ? WHERE host_id = ?",
                      (json.dumps(caps), host_id))

    def permitted_verbs(self, host_id):
        """What the controller may send this worker: its policy, plus the two
        discovery verbs. A worker nobody has permitted anything can still be
        asked who it is."""
        worker = self.get(host_id)
        if worker is None:
            raise UnknownWorker(host_id)
        caps = worker.get("capabilities") or {}
        policy = caps.get(PERMITTED) if isinstance(caps, dict) else None
        return frozenset(policy or ()) | DISCOVERY

    def heartbeat(self, host_id, agent_version=None, capabilities=None,
                  at=None):
        """Record that the worker answered. The only thing that makes it
        healthy."""
        sets = ["last_seen_at = ?", "state = ?"]
        params = [_iso(at or _now()), HEALTHY]
        if agent_version is not None:
            sets.append("agent_version = ?")
            params.append(agent_version)

        with self._conn() as c:
            row = c.execute(
                "SELECT capabilities FROM workers WHERE host_id = ?",
                (host_id,)).fetchone()
            if row is None:
                raise UnknownWorker(host_id)
            if capabilities is not None:
                # What the agent says about itself. Never the verbs it may be
                # sent - see PERMITTED.
                sets.append("capabilities = ?")
                params.append(_declared(row["capabilities"], capabilities))
            c.execute(
                f"UPDATE workers SET {', '.join(sets)} WHERE host_id = ?",
                params + [host_id])

    def get(self, host_id):
        with self._conn() as c:
            return _decode(c.execute(
                "SELECT * FROM workers WHERE host_id = ?",
                (host_id,)).fetchone())

    def list(self, kind=None):
        where, params = ([], [])
        if kind:
            where.append("kind = ?")
            params.append(kind)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM workers {clause} ORDER BY host_id",
                params).fetchall()
        return [_decode(r) for r in rows]

    def health(self, host_id, now=None):
        """`healthy`, `degraded` or `unknown`, computed from the last beat.

        Computed rather than read back, so a worker that stopped answering
        becomes degraded by the passage of time alone. A stored flag would stay
        `healthy` for ever if whatever was meant to clear it also died - which
        is exactly the failure that makes a health field worth nothing.
        """
        worker = self.get(host_id)
        if worker is None:
            raise UnknownWorker(host_id)
        return self._health_of(worker, now)

    def _health_of(self, worker, now=None):
        last = _parse(worker.get("last_seen_at"))
        if last is None:
            return UNKNOWN
        limit = timedelta(
            seconds=HEARTBEAT_SECONDS * MISSED_BEATS_BEFORE_DEGRADED)
        return HEALTHY if (now or _now()) - last <= limit else DEGRADED

    def healthy(self, kind=None, now=None):
        """Workers that may be given work right now."""
        return [w for w in self.list(kind)
                if self._health_of(w, now) == HEALTHY]

    def accepts_destructive_verbs(self, host_id, now=None):
        """Whether a removal may be dispatched to this worker.

        The gate, stated as a question rather than left to each call site to
        remember. Unreachable and absent look the same from here, and removing
        a runner on that evidence deletes capacity that was only out of touch.
        """
        return self.health(host_id, now) == HEALTHY
