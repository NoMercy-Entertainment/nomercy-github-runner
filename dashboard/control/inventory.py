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
import math
from datetime import datetime, timedelta, timezone

from store import schema

#: What a worker is. Named for what they are; a guest is never called a
#: container (CON-7).
HYPERV_LINUX = "hyperv-linux"
HYPERV_WINDOWS = "hyperv-windows"
KINDS = (HYPERV_LINUX, HYPERV_WINDOWS)

HEALTHY, DEGRADED, UNKNOWN = "healthy", "degraded", "unknown"

#: Design 13.4: a heartbeat every 10s. Three missed beats is the point at
#: which a pause becomes an absence - long enough not to trip on one slow
#: beat, short enough that an operator is not acting on stale information.
#:
#: This was 5 until T-0404, taken from the "Heartbeat" row of design 17.2 -
#: but that row gives the heartbeat call's timeout, not how often it is sent.
#: At 5 a worker beating on schedule would have read as degraded after 15s of
#: a 30s window.
HEARTBEAT_SECONDS = 10
MISSED_BEATS_BEFORE_DEGRADED = 3

#: What a worker may say about one of its execution units. Anything else it
#: sends is recorded as unknown rather than guessed at: "unknown" is a real
#: answer and is never folded into running or stopped (design 13.4).
UNIT_STATES = frozenset({"running", "stopped", "absent", "unknown"})


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
    except (ValueError, TypeError):
        return None


def _resource_enforcement(value):
    """Closed observation of one inspected appliance; never fleet intent."""
    if not isinstance(value, dict) or value.get("kind") != "macos-appliance":
        return None
    flags = ("appliance_per_runner", "cpu_enforcement", "memory_enforcement")
    numbers = ("cpu_cores", "memory_limit_bytes", "memory_overhead_bytes")
    if any(value.get(key) is not True for key in flags):
        return None
    if any(type(value.get(key)) is not int or not 0 < value[key] <= 2 ** 63 - 1 for key in numbers):
        return None
    return {key: value[key] for key in ("kind", *flags, *numbers)}


def current_resource_enforcement(spec, at=None):
    """Evidence expires independently of worker liveness/minimal beats."""
    telemetry = spec.get("telemetry") or {}
    if not isinstance(telemetry, dict):
        return None
    measured = _parse(telemetry.get("resource_enforcement_at"))
    age = ((at or _now()) - measured).total_seconds() if measured else None
    if age is None or not 0 <= age < HEARTBEAT_SECONDS * MISSED_BEATS_BEFORE_DEGRADED:
        return None
    return _resource_enforcement(telemetry.get("resource_enforcement"))


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


#: What a heartbeat may say a unit uses, and nothing else: numbers, or null.
TELEMETRY_KEYS = ("cpu_percent", "cpu_cores", "host_cores", "mem_used_bytes", "mem_limit_bytes",
                  "mem_swap_limit_bytes",
                  "root_disk_used_bytes", "root_disk_total_bytes",
                  "storage_bytes", "cache_bytes", "cache_cap_bytes")


def _telemetry(value):
    """A unit's reported usage, kept to the known keys and to numbers - a
    beat is data from a worker, and only what the card can show is taken."""
    if not isinstance(value, dict):
        return {}
    result = {k: value[k] for k in TELEMETRY_KEYS
            if k in value and (value[k] is None or (
                isinstance(value[k], (int, float))
                and not isinstance(value[k], bool)
                and 0 <= value[k] <= 2 ** 63 - 1
                and math.isfinite(value[k])))}
    job = value.get("job")
    if isinstance(job, str):
        result["job"] = " ".join(job.split())[:200] or None
    for key in ("at", "storage_at", "cache_at"):
        if isinstance(value.get(key), str) and _parse(value[key]):
            result[key] = value[key]
    return result


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
        healthy - and so the thing that clears a reason it was marked
        degraded for: a beat only reaches here once it has passed every
        check, the protocol version included."""
        sets = ["last_seen_at = ?", "state = ?", "state_reason = NULL"]
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

    def accept_heartbeat(self, host_id, payload, at=None):
        """Take one heartbeat from a worker whose identity is already proved.

        `host_id` comes from the certificate the beat arrived with, not from
        the payload: a payload naming another worker is refused, so one worker
        cannot beat on another's behalf. For each unit it reports, the runner's
        `last_seen_at` and `unit_state` are written - but only for runners
        placed on this worker. A unit it does not mention is left alone rather
        than taken to be gone: silence about a unit is not evidence about it.

        Returns what it recorded, for the caller to log.
        """
        if not isinstance(payload, dict):
            raise ValueError("a heartbeat must be an object")
        claimed = payload.get("host_id")
        if claimed != host_id:
            raise ValueError(
                f"heartbeat names {claimed!r} but arrived from {host_id!r}")

        moment = at or _now()
        measured = _parse(payload.get("measured_at")) or moment
        observed_at = _iso(min(measured, moment))
        declared = payload.get("capabilities")
        self.heartbeat(host_id,
                       agent_version=payload.get("agent_version"),
                       capabilities=declared if isinstance(declared, dict)
                       else None, at=moment)

        seen, used, enforced = {}, {}, {}
        for unit in payload.get("instances") or []:
            if not isinstance(unit, dict):
                continue
            runner_id = unit.get("runner_id")
            state = unit.get("state")
            if not isinstance(runner_id, str):
                continue
            seen[runner_id] = state if state in UNIT_STATES else "unknown"
            enforced[runner_id] = (_resource_enforcement(unit.get("resource_enforcement"))
                                   if state != "absent" else None)
            t = _telemetry(unit.get("telemetry"))
            if t:
                used[runner_id] = t

        recorded = {}
        with self._conn() as c:
            for runner_id, state in seen.items():
                # Direct, and outside spec_version: an observation, not a
                # change of intent. The host_id condition is the fence - a
                # worker cannot report on a runner placed somewhere else.
                cur = c.execute(
                    "UPDATE runner_specs SET last_seen_at = ?,"
                    " unit_state = ? WHERE runner_id = ? AND host_id = ?",
                    (observed_at, state, runner_id, host_id))
                if not cur.rowcount:
                    continue
                recorded[runner_id] = state
                self._merge_telemetry(c, runner_id, used.get(runner_id, {}),
                                      _iso(moment), enforced.get(runner_id), observed_at)
        return recorded

    def _merge_telemetry(self, c, runner_id, fresh, at, enforcement=None, observed_at=None):
        """What a unit uses, as last reported (T-1803). CPU and memory are
        replaced every beat; storage and cache only arrive on a deep beat and
        are kept, with their own time, until the next one."""
        row = c.execute("SELECT telemetry FROM runner_specs"
                        " WHERE runner_id = ?", (runner_id,)).fetchone()
        try:
            current = json.loads(row["telemetry"]) if row and                 row["telemetry"] else {}
        except ValueError:
            current = {}
        current["resource_enforcement"] = enforcement
        current["resource_enforcement_at"] = observed_at if enforcement else None
        for key in ("cpu_percent", "cpu_cores", "host_cores", "job",
                    "mem_used_bytes", "mem_limit_bytes",
                    "mem_swap_limit_bytes",
                    "root_disk_used_bytes", "root_disk_total_bytes"):
            current[key] = fresh.get(key)
        current["at"] = min(fresh.get("at", at), at)
        for key, stamp in (("storage_bytes", "storage_at"),
                           ("cache_bytes", "cache_at")):
            if key in fresh:
                current[key] = fresh[key]
                current[stamp] = min(fresh.get(stamp, at), at)
                current["deep_at"] = current[stamp]
        if "cache_cap_bytes" in fresh:
            current["cache_cap_bytes"] = fresh["cache_cap_bytes"]
        c.execute("UPDATE runner_specs SET telemetry = ? WHERE runner_id = ?",
                  (json.dumps(current), runner_id))

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

    def mark_degraded(self, host_id, reason):
        """Mark a worker degraded for a reason silence would not explain.

        Sticky: the worker reads as degraded however recently it last beat,
        until a beat that passes every check clears it. Used for a protocol
        major the controller does not speak (T-0406) - a worker that keeps
        beating in a language nobody here understands is not healthy, and
        should not look it.
        """
        with self._conn() as c:
            cur = c.execute(
                "UPDATE workers SET state = ?, state_reason = ?"
                " WHERE host_id = ?", (DEGRADED, str(reason)[:500], host_id))
            if not cur.rowcount:
                raise UnknownWorker(host_id)

    def health_reason(self, host_id, now=None):
        """Why a worker is in the health it is in, in words.

        What the dashboard shows beside the state. A marked reason wins over
        the heartbeat arithmetic, because it says something the arithmetic
        cannot.
        """
        worker = self.get(host_id)
        if worker is None:
            raise UnknownWorker(host_id)
        if worker.get("state") == DEGRADED and worker.get("state_reason"):
            return worker["state_reason"]
        health = self._health_of(worker, now)
        if health == UNKNOWN:
            return "never heard from"
        if health == DEGRADED:
            last = _parse(worker.get("last_seen_at"))
            gap = int(((now or _now()) - last).total_seconds())
            return f"no heartbeat for {gap}s"
        return ""

    def summary(self, now=None):
        """Every worker with its health and the reason for it. Read-only, and
        nothing in it is secret."""
        out = []
        for worker in self.list():
            out.append({
                "host_id": worker["host_id"],
                "kind": worker["kind"],
                "agent_version": worker.get("agent_version"),
                "last_seen_at": worker.get("last_seen_at"),
                "health": self._health_of(worker, now),
                "reason": self.health_reason(worker["host_id"], now),
                "resources": {key: (worker.get("capabilities") or {}).get(key)
                              for key in ("memory_bytes", "memory_total_bytes", "swap_bytes",
                                          "swap_total_bytes", "memory_commit_bytes",
                                          "memory_admission", "capacity_valid", "max_runners")},
            })
        return out

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
        if worker.get("state") == DEGRADED and worker.get("state_reason"):
            return DEGRADED
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
