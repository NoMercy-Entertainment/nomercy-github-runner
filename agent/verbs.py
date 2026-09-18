"""The sixteen things an agent can be asked to do, and nothing else.

`VERBS` maps each name in `protocol.VERB_NAMES` to one handler. It is a frozen
mapping - a `MappingProxyType` over a dict nobody else holds - so a verb cannot
be added at run time, and the server refuses a name that is not in it before it
reads a byte of the request body.

**Every handler accepts a closed set of fields.** `FIELDS` names them per verb,
and anything else in the body is refused rather than ignored: an ignored field
is a field a later change can quietly start honouring. There is no field for a
command, an entrypoint, a mount or a path. Where a runner's data lives is
derived on the worker from its `runner_id`, never taken from a request - so a
request cannot point a runner at a directory that is not its own.

**Every value is validated before the runtime sees it.** The runner id must be
a UUID; an environment variable name must look like one; a probe must be one
of the closed set; a label may not contain a shell metacharacter. None of these
values are ever handed to a shell - the runtimes build argument lists - but
refusing them here means that stays true even if a runtime is written
carelessly one day.

The runtime and registrar are passed in. Phase 4 builds the real ones; here
they are whatever implements the two protocols below.
"""
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from . import protocol


class Refused(Exception):
    """The request is well-formed HTTP but not something this agent will do.

    Carries a reason for the controller to record. Never carries the value
    that was refused: a refused field may be a token sent in the wrong place.
    """

    status = 400

    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = reason
        if status is not None:
            self.status = status


class Runtime(Protocol):
    """What the agent needs from the execution runtime on its worker."""

    def create(self, runner_id: str, spec: Mapping[str, Any]) -> str: ...
    def start(self, runner_id: str) -> None: ...
    def stop(self, runner_id: str) -> None: ...
    def restart(self, runner_id: str) -> None: ...
    def remove(self, runner_id: str, keep_data: bool) -> None: ...
    def status(self, runner_id: str) -> Mapping[str, Any]: ...
    def telemetry(self, runner_id: str) -> Mapping[str, Any]: ...
    def logs(self, runner_id: str, since_seconds: int) -> str: ...
    def probe(self, runner_id: str, probe: str) -> Mapping[str, Any]: ...
    def clear_cache(self, runner_id: str,
                    policy: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def drain(self, runner_id: str) -> None: ...
    def cancel_drain(self, runner_id: str) -> None: ...
    def capabilities(self) -> Mapping[str, Any]: ...
    def instances(self) -> list: ...


class Registrar(Protocol):
    """Registers the runner inside its unit with its forge."""

    def register(self, runner_id: str,
                 plan: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def deregister(self, runner_id: str) -> None: ...


#: Always served: they say who this agent is and what it will serve, and
#: change nothing. The controller has the same two as always-allowed.
DISCOVERY = frozenset({"hello", "capabilities"})


class Agent:
    """The state a handler needs: who this worker is and what it drives.

    `permitted` is this worker's own policy - the verbs it will carry out,
    whatever the controller believes it may ask. The controller checks its
    policy before sending and this one is checked again on arrival, so a
    controller with a wrong or widened policy still cannot get a verb this
    worker was configured not to serve (T-0403).
    """

    def __init__(self, host_id, runtime, registrar, version="0",
                 permitted=None):
        self.host_id = host_id
        self.runtime = runtime
        self.registrar = registrar
        self.version = version
        permitted = (protocol.VERB_NAMES if permitted is None
                     else frozenset(permitted))
        unknown = sorted(permitted - protocol.VERB_NAMES)
        if unknown:
            raise ValueError(f"not protocol verbs: {unknown}")
        self.permitted = frozenset(permitted) | DISCOVERY


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$")
_CPUSET = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")
_SIZE = re.compile(r"^[0-9]+(\.[0-9]+)?[kmgtKMGT]?[bB]?$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: Labels may carry a Forgejo image reference (`docker:docker://node:20`), so
#: colons, slashes and dots are allowed. Nothing a shell treats specially is.
_LABELS = re.compile(r"^[A-Za-z0-9:/._@+,=-]{1,1024}$")
_LABEL_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: The fields each verb reads. Anything else in a body is refused.
FIELDS = MappingProxyType({
    "hello": frozenset(),
    "capabilities": frozenset(),
    "exec_unit.create": frozenset({"runner_id", "spec"}),
    "exec_unit.start": frozenset({"runner_id"}),
    "exec_unit.stop": frozenset({"runner_id"}),
    "exec_unit.restart": frozenset({"runner_id"}),
    "exec_unit.remove": frozenset({"runner_id", "keep_data"}),
    "exec_unit.status": frozenset({"runner_id"}),
    "exec_unit.telemetry": frozenset({"runner_id"}),
    "exec_unit.logs": frozenset({"runner_id", "since_seconds"}),
    "exec_unit.probe": frozenset({"runner_id", "probe"}),
    "exec_unit.clear_cache": frozenset({"runner_id", "policy"}),
    "exec_unit.drain": frozenset({"runner_id"}),
    "exec_unit.cancel_drain": frozenset({"runner_id"}),
    "runner.register": frozenset({"runner_id", "plan"}),
    "runner.deregister": frozenset({"runner_id"}),
})

#: What a unit spec may carry. No command, entrypoint, mount, volume, device,
#: capability or privilege: how a unit is run is the runtime's decision, made
#: on the worker, and where its data lives is derived from its runner_id.
SPEC_FIELDS = frozenset({"image", "env", "labels", "cpus", "memory", "cpuset",
                         "stop_timeout"})
PLAN_FIELDS = frozenset({"url", "token", "name", "labels", "runner_group"})
POLICY_FIELDS = frozenset({"max_bytes", "scopes", "on_clear", "timeout"})


def _closed(body, allowed, what):
    if not isinstance(body, dict):
        raise Refused(f"{what} must be an object")
    extra = sorted(set(body) - set(allowed))
    if extra:
        # Names only. Never the values: a refused field may hold a secret.
        raise Refused(f"{what} carries fields this verb does not take: "
                      f"{extra}")
    return body


def _runner_id(body):
    rid = body.get("runner_id")
    if not isinstance(rid, str) or not _UUID.match(rid):
        raise Refused("runner_id must be a UUID")
    return rid.lower()


def _int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refused(f"{name} must be an integer")
    if not low <= value <= high:
        raise Refused(f"{name} must be between {low} and {high}")
    return value


def _spec(value):
    spec = _closed(value, SPEC_FIELDS, "spec")
    out = {}
    if "image" in spec:
        if not isinstance(spec["image"], str) or not _IMAGE.match(
                spec["image"]):
            raise Refused("spec.image is not an image reference")
        out["image"] = spec["image"]
    if "env" in spec:
        env = spec["env"]
        if not isinstance(env, dict):
            raise Refused("spec.env must be an object")
        for key, val in env.items():
            if not isinstance(key, str) or not _ENV_NAME.match(key):
                raise Refused("spec.env has a name that is not a variable name")
            # One line: the runtime hands the environment to the engine as a
            # file of KEY=value lines, and a line break inside a value would
            # write a second variable of the sender's choosing.
            if not isinstance(val, str) or any(c in val for c in "\x00\r\n"):
                raise Refused(f"spec.env.{key} must be one line of text")
        out["env"] = dict(env)
    if "labels" in spec:
        labels = spec["labels"]
        if not isinstance(labels, dict):
            raise Refused("spec.labels must be an object")
        for key, val in labels.items():
            if not isinstance(key, str) or not _LABEL_KEY.match(key):
                raise Refused("spec.labels has an invalid key")
            if not isinstance(val, str) or len(val) > 256:
                raise Refused(f"spec.labels.{key} must be short text")
        out["labels"] = dict(labels)
    for key in ("cpus", "memory"):
        if key in spec:
            val = str(spec[key])
            if not _SIZE.match(val):
                raise Refused(f"spec.{key} is not a size")
            out[key] = val
    if "cpuset" in spec:
        if not isinstance(spec["cpuset"], str) or not _CPUSET.match(
                spec["cpuset"]):
            raise Refused("spec.cpuset is not a CPU list")
        out["cpuset"] = spec["cpuset"]
    if "stop_timeout" in spec:
        out["stop_timeout"] = _int(spec["stop_timeout"], "spec.stop_timeout",
                                   1, 600)
    return out


def _plan(value):
    plan = _closed(value, PLAN_FIELDS, "plan")
    url = plan.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://[^\s'\"`$;|&<>]+$",
                                                url):
        raise Refused("plan.url is not a URL")
    token = plan.get("token")
    if not isinstance(token, str) or not token or len(token) > 512 or \
            re.search(r"\s", token):
        raise Refused("plan.token is missing or malformed")
    name = plan.get("name", "")
    if name and (not isinstance(name, str) or not _NAME.match(name)):
        raise Refused("plan.name has characters a runner name may not")
    labels = plan.get("labels", "")
    if labels and (not isinstance(labels, str) or not _LABELS.match(labels)):
        raise Refused("plan.labels has characters a label list may not")
    group = plan.get("runner_group", "")
    if group and (not isinstance(group, str) or not _NAME.match(group)):
        raise Refused("plan.runner_group is not a group name")
    return {"url": url, "token": token, "name": name, "labels": labels,
            "runner_group": group}


def _policy(value):
    policy = _closed(value or {}, POLICY_FIELDS, "policy")
    out = {}
    if "scopes" in policy:
        scopes = policy["scopes"]
        if not isinstance(scopes, list) or not all(
                isinstance(s, str) for s in scopes):
            raise Refused("policy.scopes must be a list of names")
        unknown = sorted(set(scopes) - protocol.CACHE_SCOPES)
        if unknown:
            raise Refused(f"policy.scopes names scopes that do not exist: "
                          f"{unknown}")
        out["scopes"] = list(scopes)
    if "on_clear" in policy:
        if policy["on_clear"] not in ("skip-if-busy", "drain-first"):
            raise Refused("policy.on_clear is not a known behaviour")
        out["on_clear"] = policy["on_clear"]
    if "max_bytes" in policy:
        out["max_bytes"] = _int(policy["max_bytes"], "policy.max_bytes", 0,
                                2 ** 50)
    if "timeout" in policy:
        out["timeout"] = _int(policy["timeout"], "policy.timeout", 1, 3600)
    return out


# ---------------------------------------------------------------------------
# the handlers - every one takes (agent, body) and nothing else
# ---------------------------------------------------------------------------
#
# Each handler does two things in a fixed order. It validates everything it
# will use, raising Refused if anything is wrong, and only then returns a
# function that does the work. Nothing is done until that function is called.
#
# The split is what lets a slow verb be answered at once (T-0405): the server
# validates while the caller waits - so a bad request is still a 400, never a
# 202 that fails later - and runs the work after replying. A handler that did
# work before returning would make "validated but not started" impossible to
# express.

def _hello(agent, body):
    return lambda: {"host_id": agent.host_id, "agent_version": agent.version,
                    "protocol_major": protocol.PROTOCOL_MAJOR}


def _capabilities(agent, body):
    """What this worker will serve, and what its runtime can do.

    The verbs reported are the worker's own policy. The controller shows them
    beside its own; it never adopts them as its policy.
    """
    return lambda: {"verbs": sorted(agent.permitted),
                    "runtime": dict(agent.runtime.capabilities() or {})}


def _create(agent, body):
    rid, spec = _runner_id(body), _spec(body.get("spec", {}))
    return lambda: {"handle": agent.runtime.create(rid, spec)}


def _start(agent, body):
    rid = _runner_id(body)
    return lambda: agent.runtime.start(rid) or {}


def _stop(agent, body):
    rid = _runner_id(body)
    return lambda: agent.runtime.stop(rid) or {}


def _restart(agent, body):
    rid = _runner_id(body)
    return lambda: agent.runtime.restart(rid) or {}


def _remove(agent, body):
    keep = body.get("keep_data", False)
    if not isinstance(keep, bool):
        raise Refused("keep_data must be true or false")
    rid = _runner_id(body)
    return lambda: agent.runtime.remove(rid, keep) or {}


def _status(agent, body):
    rid = _runner_id(body)
    return lambda: dict(agent.runtime.status(rid) or {})


def _telemetry(agent, body):
    rid = _runner_id(body)
    return lambda: dict(agent.runtime.telemetry(rid) or {})


def _logs(agent, body):
    since = _int(body.get("since_seconds", 300), "since_seconds", 1, 86400)
    rid = _runner_id(body)
    return lambda: {"text": agent.runtime.logs(rid, since) or ""}


def _probe(agent, body):
    """A probe is a name from a closed set, never a command (NFR-10)."""
    probe = body.get("probe")
    if probe not in protocol.PROBES:
        raise Refused("probe is not one of the named probes")
    rid = _runner_id(body)
    return lambda: dict(agent.runtime.probe(rid, probe) or {})


def _clear_cache(agent, body):
    rid, policy = _runner_id(body), _policy(body.get("policy"))
    return lambda: dict(agent.runtime.clear_cache(rid, policy) or {})


def _drain(agent, body):
    """Ask the unit's process to finish what it is doing, take nothing new,
    and stay down afterwards (OPEN-7). How long it may take is the runner's
    own shutdown timeout; how long the controller waits is its operation's
    deadline. Nothing here ever kills the process."""
    rid = _runner_id(body)
    return lambda: agent.runtime.drain(rid) or {}


def _cancel_drain(agent, body):
    """Undo a drain once it is complete: the unit is started again and
    restarted as before if it stops. The controller only asks from
    `drained`, when the job is done and the unit is down."""
    rid = _runner_id(body)
    return lambda: agent.runtime.cancel_drain(rid) or {}


def _register(agent, body):
    rid, plan = _runner_id(body), _plan(body.get("plan"))

    def work():
        result = agent.registrar.register(rid, plan)
        # Only the forge's identifiers go back. The plan, and its token, stay
        # here.
        return {"registration_id": str((result or {}).get("registration_id")
                                       or ""),
                "registration_uuid": (result or {}).get("registration_uuid")}
    return work


def _deregister(agent, body):
    rid = _runner_id(body)
    return lambda: agent.registrar.deregister(rid) or {}


VERBS = MappingProxyType({
    "hello": _hello,
    "capabilities": _capabilities,
    "exec_unit.create": _create,
    "exec_unit.start": _start,
    "exec_unit.stop": _stop,
    "exec_unit.restart": _restart,
    "exec_unit.remove": _remove,
    "exec_unit.status": _status,
    "exec_unit.telemetry": _telemetry,
    "exec_unit.logs": _logs,
    "exec_unit.probe": _probe,
    "exec_unit.clear_cache": _clear_cache,
    "exec_unit.drain": _drain,
    "exec_unit.cancel_drain": _cancel_drain,
    "runner.register": _register,
    "runner.deregister": _deregister,
})


def prepare(agent, verb, body):
    """Validate one request completely and return the work, not yet done.

    Raises Refused for anything wrong - an unknown verb, one this worker does
    not serve, a field the verb does not take, a value that does not check
    out. A request that gets a function back has passed every check there is.
    """
    handler = VERBS.get(verb)
    if handler is None:
        raise Refused(f"no verb {verb!r}", status=404)
    if verb not in agent.permitted:
        raise Refused("not permitted on this worker", status=403)
    _closed(body, FIELDS[verb], "body")
    return handler(agent, body)


def dispatch(agent, verb, body):
    """Validate and run one verb, synchronously."""
    return prepare(agent, verb, body)()


def secrets_in(body):
    """Values in a request that must never come back out in an error.

    The registration token, and every environment value a unit is created
    with - an environment can carry a token too, and a runtime error that
    echoes the command it ran would echo it. Scrubbed by value, because an
    error message has no field names to match on.
    """
    found = []
    plan = body.get("plan") if isinstance(body, dict) else None
    if isinstance(plan, dict) and isinstance(plan.get("token"), str):
        found.append(plan["token"])
    spec = body.get("spec") if isinstance(body, dict) else None
    if isinstance(spec, dict) and isinstance(spec.get("env"), dict):
        found += [v for v in spec["env"].values() if isinstance(v, str)]
    return [v for v in found if len(v) >= 8]


def scrub(text, secrets):
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text
