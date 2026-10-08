"""One runner card: design 14.1's payload, the only shape the v2 page renders.

Every runner the dashboard can see becomes the same eighteen fields, whatever
it is and wherever it runs, and the page renders those fields and nothing else
(FR-12, CON-4, CON-8). Where platforms differ, the difference arrives as data:
`capabilities` says what the runner can do, `annotations` says in words what
it cannot, and `actions` says for each of design 14.2's actions whether it is
offered, whether it is enabled, and why not. The template never tests a
provider or a platform; tests/test_generic_card.py reads it and fails if it
does.

Three sources feed it during the transition (design 14.6, MIG-6):

* **controller runners**, from a RunnerSpec - the destination. Their actions
  go to `/api/v2/runners/<runner_id>/actions/<verb>`.
* **today's containers**, from the v1 status snapshot. They have no
  runner_id yet, so their actions go to the v1 routes they always used, and
  each card says where - as data, in `actions`, not as a branch in the page.
* **runners the forge knows that are not managed here** - the Windows service
  and the macOS appliance, until they are adopted (T-0802). Their cards carry
  every action, each disabled with the reason.

Nothing here talks to Docker or a forge. It translates what the collectors
already measured.
"""
import re

import providers

#: Design 14.1, in its order. Every card carries every one of these keys.
FIELDS = ("runner_id", "display_name", "provider", "platform",
          "architecture", "labels", "worker", "runtime", "state", "job", "cpu",
          "memory", "storage", "cache", "reachable", "last_seen_at",
          "current_operation", "last_error", "last_note",
          "capabilities")

#: Design 14.2's actions, plus cancelling a drain, in the order a card shows
#: them.
ACTIONS = ("drain", "cancel_drain", "start", "stop", "restart", "recreate",
           "remove", "clear_cache", "logs")

LABELS = {"drain": "Drain", "cancel_drain": "Cancel drain", "start": "Start",
          "stop": "Stop", "restart": "Restart", "recreate": "Recreate",
          "remove": "Remove", "clear_cache": "Clear cache", "logs": "Logs"}

#: How each action looks, so danger is data too.
TONES = {"drain": "warn", "cancel_drain": "warn", "stop": "danger",
         "recreate": "warn", "remove": "danger", "clear_cache": "warn"}

#: What each capability's absence means, in words. A capability that is false
#: renders its annotation on the card: the page shows the difference instead
#: of hiding it (design 14.1).
ANNOTATIONS = {
    "job_containers": "no job containers",
    "nested_builds": "no nested builds",
    "supports_drain": "cannot be drained",
    "clear_cache": "cache cannot be cleared",
}

#: The build-cache ceiling a Linux runner's builder enforces.
CACHE_CAP_BYTES = 40 * 10 ** 9

#: Today's containers: one runner each on the WSL engine, with the v1
#: dashboard's own drain.
LEGACY_CAPABILITIES = {"kind": "linux-container", "job_containers": True,
                       "nested_builds": True, "resettable_os": False,
                       "supports_drain": True, "clear_cache": True}

#: A runner the forge knows that is not managed here. What it cannot do is
#: known from its platform; everything it could do is not reachable yet.
UNMANAGED_CAPABILITIES = {
    providers.WINDOWS: {"kind": "windows-process", "job_containers": False,
                        "nested_builds": False, "resettable_os": False,
                        "supports_drain": False, "clear_cache": False},
    providers.MACOS: {"kind": "macos-appliance", "job_containers": False,
                      "nested_builds": False, "resettable_os": False,
                      "supports_drain": False, "clear_cache": False},
    None: {"kind": "unknown", "supports_drain": False, "clear_cache": False},
}

#: A forge label that names a platform outright. Forge records carry no
#: platform field, so this is the only evidence; a runner whose labels name
#: none reads as platform unknown rather than being guessed at.
PLATFORM_LABELS = {"windows": providers.WINDOWS, "macos": providers.MACOS,
                   "osx": providers.MACOS, "darwin": providers.MACOS,
                   "linux": providers.LINUX}

#: Forgejo's words for a runner's state, in this page's.
FORGE_STATES = {"active": "busy", "idle": "idle", "offline": "offline"}

UNMANAGED_REASON = ("not managed from here yet: this runner is outside the "
                    "control plane until it is adopted (T-0802)")

_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
          "TB": 1000 ** 4, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
          "TIB": 1024 ** 4}


def to_bytes(text):
    """A docker size string in bytes, or None when it cannot be read."""
    m = re.match(r"\s*([0-9.]+)\s*([KMGT]?I?B)\s*$", str(text or ""), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def annotations(capabilities):
    caps = capabilities or {}
    return [text for key, text in ANNOTATIONS.items()
            if caps.get(key) is False]


def _drift(note):
    """The registration-drift sentence of a note, without its timestamp, or
    None. A runner registered with other labels than its fleet asks for
    still works - it takes the wrong jobs - so it is a warning, not an
    error."""
    text = str(note or "")
    at = text.find("registered with other labels")
    return text[at:] if at >= 0 else None


def fleet_of(provider, platform, architecture):
    """The fleet a card belongs to, in store/fleets.py's own id form."""
    if not (provider and platform and architecture):
        return None
    return f"{provider}-{platform}-{architecture}"


def _card(**fields):
    card = {key: fields.pop(key, None) for key in FIELDS}
    card["annotations"] = annotations(card["capabilities"])
    card["fleet_id"] = fields.pop("fleet_id", None) or fleet_of(
        card["provider"], card["platform"], card["architecture"])
    card.update(fields)
    return card


def _action(verb, visible=True, enabled=True, reason=None, method="POST",
            url=None, body=None, confirm=None, idempotent=False):
    return {"verb": verb, "label": LABELS[verb], "tone": TONES.get(verb, ""),
            "visible": bool(visible), "enabled": bool(enabled and url),
            "reason": None if (enabled and url) else
            (reason or "not available for this runner"),
            "method": method, "url": url, "body": body, "confirm": confirm,
            "idempotent": idempotent}


def _visible(state):
    """Which actions make sense in which state - the v1 card's rule."""
    stopped, draining = state == "stopped", state == "draining"
    return {"drain": not stopped and not draining, "cancel_drain": draining,
            "start": stopped, "stop": not stopped, "restart": not stopped,
            "recreate": True, "remove": True, "clear_cache": True,
            "logs": True}


def _confirm(verb, who, forge):
    return {
        "remove": {"title": f"Remove {who}?",
                   "body": f"It is deregistered from {forge} and deleted.",
                   "confirm": "Remove"},
        "stop": {"title": f"Stop {who}?",
                 "body": "A running job would be killed.", "confirm": "Stop"},
        "recreate": {"title": f"Recreate {who}?",
                     "body": "It is removed and rebuilt with a fresh "
                             "workspace and registration.",
                     "confirm": "Recreate"},
        "clear_cache": {"title": f"Clear {who}'s cache?",
                        "body": "The cache is deleted. This cannot be "
                                "undone.", "confirm": "Clear cache"},
    }.get(verb)


# ---------------------------------------------------------------------------
# today's containers
# ---------------------------------------------------------------------------

def display_name(name, provider_key):
    """"github-runner-3" -> "Runner 3", as the v1 page has always shown it."""
    return re.sub(rf"^{re.escape(provider_key or 'github')}-runner-",
                  "Runner ", name or "")


def from_legacy(runner, host=None, generated=None):
    """A card for one of today's containers, from the v1 status snapshot."""
    name = runner.get("name")
    provider = runner.get("provider") or "github"
    state = runner.get("state") or "unknown"
    forge = {"github": "GitHub", "forgejo": "Forgejo"}.get(provider, provider)
    who = name
    host = host or {}
    caps = dict(LEGACY_CAPABILITIES)
    visible = _visible(state)

    urls = {
        "drain": ("/api/runner/drain", {"name": name}),
        "cancel_drain": ("/api/runner/canceldrain", {"name": name}),
        "start": ("/api/runner/start", {"name": name}),
        "stop": ("/api/runner/stop", {"name": name}),
        "restart": ("/api/runner/restart", {"name": name}),
        "remove": ("/api/runner/remove", {"name": name}),
        "clear_cache": (f"/api/runner/{name}/prune", None),
    }
    actions = []
    for verb in ACTIONS:
        if verb == "logs":
            actions.append(_action("logs", method="LINK",
                                   url=f"/runner/{name}"))
            continue
        if verb == "recreate":
            actions.append(_action(
                "recreate", enabled=False,
                reason="one runner at a time comes with the control plane; "
                       "until then, recreate the whole fleet"))
            continue
        url, body = urls[verb]
        actions.append(_action(verb, visible=visible[verb], url=url,
                               body=body, confirm=_confirm(verb, who, forge)))

    stopped = state == "stopped"
    # The same four meters a controller runner's card has, from the same
    # function: the snapshot read as the telemetry it is.
    cpu, memory, storage, cache = _measured({}, {
        "cpu_percent": None if stopped else runner.get("cpu_percent"),
        "cpu_cores": runner.get("cpu_cores"),
        "mem_used_bytes": None if stopped else to_bytes(runner.get("mem_used")),
        "mem_limit_bytes": to_bytes(runner.get("mem_limit")),
        "cache_bytes": None if stopped else to_bytes(runner.get("build_cache")),
        "cache_cap_bytes": CACHE_CAP_BYTES,
    }, None, {"cpus": host.get("ncpu"),
              "memory_bytes": host.get("mem_total_bytes")})
    return _card(
        runner_id=None,
        display_name=display_name(name, provider),
        provider=provider,
        platform=providers.LINUX,
        architecture=providers.X64,
        labels=None,
        label_drift=None,
        worker="wsl:github-runners",
        runtime="linux-container",
        state=state,
        job=runner.get("job") or None,
        cpu=cpu,
        memory=memory,
        storage=storage,
        cache=cache,
        reachable=not stopped,
        last_seen_at=generated,
        current_operation=None,
        last_error=None,
        last_note=None,
        capabilities=caps,
        key=f"v1:{name}",
        source="v1",
        registration=runner.get("registration"),
        uptime=runner.get("uptime"),
        href=f"/runner/{name}",
        actions=actions,
    )


# ---------------------------------------------------------------------------
# runners the forge knows that are not managed here
# ---------------------------------------------------------------------------

def platform_from_labels(labels):
    words = labels if isinstance(labels, (list, tuple)) else \
        str(labels or "").split(",")
    for word in words:
        name = str(word).strip().split(":", 1)[0].lower()
        if name in PLATFORM_LABELS:
            return PLATFORM_LABELS[name]
    return None


def from_unmanaged(entry, generated=None):
    """A card for a runner registered with Forgejo that no worker here runs:
    the Windows service and the macOS appliance, until T-0802 adopts them."""
    platform = platform_from_labels(entry.get("labels"))
    caps = dict(UNMANAGED_CAPABILITIES.get(platform,
                                           UNMANAGED_CAPABILITIES[None]))
    t = entry.get("telemetry") or None
    disk = (t or {}).get("disk") or {}
    state = FORGE_STATES.get(entry.get("status") or "", "unknown")
    # The same four meters as every other card. The exporter reads the
    # machine's disk, not the runner's own share of it: that is the volume
    # the runner shares.
    cpu, memory, storage, cache = _measured({}, {
        "cpu_percent": (t or {}).get("cpu_percent"),
        "cpu_cores": (t or {}).get("cpu_cores"),
        "mem_used_bytes": (t or {}).get("mem_used_bytes"),
        "mem_limit_bytes": (t or {}).get("mem_limit_bytes"),
        "storage_volume_used_bytes": disk.get("used_bytes"),
        "storage_volume_total_bytes": disk.get("total_bytes"),
        "cache_cap_bytes": None,
    }, None)
    return _card(
        runner_id=None,
        display_name=entry.get("name") or "-",
        provider="forgejo",
        platform=platform,
        architecture=providers.X64 if platform else None,
        worker=None,
        runtime=caps.get("kind"),
        state=state,
        job=(t or {}).get("job") if state == "busy" else None,
        cpu=cpu,
        memory=memory,
        storage=storage,
        cache=cache,
        reachable=t is not None,
        last_seen_at=generated if t is not None else None,
        current_operation=None,
        last_error=None if t is not None else
        "no telemetry: the exporter did not answer for this runner",
        last_note=None,
        capabilities=caps,
        key=f"forge:{entry.get('uuid')}",
        source="forge",
        registration=entry.get("version") and f"v{entry['version']}",
        labels=None,
        label_drift=None,
        href=None,
        actions=[_action(verb, enabled=False, reason=UNMANAGED_REASON,
                         visible=_visible(state)[verb])
                 for verb in ACTIONS],
    )


# ---------------------------------------------------------------------------
# controller runners
# ---------------------------------------------------------------------------

#: The controller's lifecycle states, in the page's fewer words where the
#: card's styling has one. Everything else is shown as it is.
_SPEC_STATES = {"drained": "draining", "stopped": "stopped", "failed": "failed"}

#: How recent an observation must be to count (T-1803). Heartbeats come
#: every 10 s and three missed make a worker degraded; the reconciler asks
#: the forge on every pass.
HEARTBEAT_FRESH = 30
FORGE_FRESH = 120
STORAGE_FRESH = 600

#: Lifecycle states in which the runner should be taking or doing work, and
#: so in which "ready" is the question.
SERVING = frozenset({"idle", "busy", "draining", "drained"})


def _age(stamp, now):
    from datetime import datetime, timezone
    if not stamp:
        return None
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - then).total_seconds()


def readiness(spec, now=None):
    """Whether the runner is ready, from both halves (design 18.5).

    Ready is the unit running, as its worker last reported within a
    heartbeat's reach, AND the forge showing it online, as it last said
    within a few passes. Either alone has been wrong on this fleet: a
    process healthy for hours while the forge had it offline. A healthy
    process whose forge cannot be asked is `unknown`, never ready.
    """
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    unit, seen = spec.get("unit_state"), _age(spec.get("last_seen_at"), now)
    fresh = seen is not None and 0 <= seen <= HEARTBEAT_FRESH
    process = ("up" if unit == "running" and fresh else
               "down" if unit in ("stopped", "absent") and fresh else
               "unknown")
    word = spec.get("forge_state")
    heard = _age(spec.get("forge_seen_at"), now)
    recent = heard is not None and 0 <= heard <= FORGE_FRESH
    forge = ("online" if word in ("idle", "busy") and recent else
             "offline" if word == "offline" and recent else "unknown")
    return {"process": process, "forge": forge,
            "ready": process == "up" and forge == "online"}


def _shown_state(lifecycle, ready):
    """What the card's badge says: the lifecycle state when the runner is
    confirmed ready, and otherwise the honest word for why it is not."""
    if lifecycle == "draining":
        return "draining"
    if lifecycle not in SERVING or ready["ready"]:
        return _SPEC_STATES.get(lifecycle, lifecycle)
    if ready["forge"] == "offline":
        return "offline"
    if ready["process"] == "down":
        return "stopped"
    return "unknown"


def _amount(value):
    """A positive number, or None: zero, a boolean or text is no limit."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value > 0 else None


def _first(*values):
    return next((v for v in map(_amount, values) if v is not None), None)


def _measured(spec, override, now, host=None):
    """The card's four meters - CPU, memory, storage and cache - always all
    four, each with what the unit uses and what that is a share of.

    The total is the runner's own limit where it has one: its cores, its
    memory limit, its own disk, its cache cap. Without one it is the real
    boundary the runner shares, and the meter says `shared`: the machine's
    cores and memory (from the beat, else from what the worker declared -
    `host`, from control/hardware.py), and the volume its directory or its
    cache is on. A shared storage or cache meter also carries that volume's
    used figure, because the volume filling is what stops the runner.

    Usage comes from heartbeats and is None - unknown, never zero - where
    it was not reported, or not recently. Nothing here asks which platform
    the runner is on: every runtime reports the same keys."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    host = host or {}
    t = dict(spec.get("telemetry") or {})
    t.update(override or {})
    age = _age(t.get("at"), now)
    live = override is not None or (age is not None and
                                    0 <= age <= HEARTBEAT_FRESH)

    def recent(*stamps):
        if override is not None:
            return True
        seen = _age(next((t[s] for s in stamps if t.get(s)), None), now)
        return seen is not None and 0 <= seen <= STORAGE_FRESH

    own_cores = _amount(t.get("cpu_cores"))
    host_cores = _first(t.get("host_cores"), host.get("cpus"))
    cpu = {"percent": t.get("cpu_percent") if live else None,
           "cores": own_cores, "host_cores": host_cores,
           "total_cores": own_cores or host_cores,
           "shared": own_cores is None}

    own_memory = _amount(t.get("mem_limit_bytes"))
    host_memory = None if own_memory else _first(t.get("host_mem_bytes"),
                                                 host.get("memory_bytes"))
    memory = {"used_bytes": t.get("mem_used_bytes") if live else None,
              "limit_bytes": own_memory, "host_bytes": host_memory,
              "total_bytes": own_memory or host_memory,
              "shared": own_memory is None}

    def bounded(used, own, volume, stamp, filled=True):
        """A storage-like meter. Its total is the unit's own boundary where
        it has one, else the volume it shares. The volume's figures - what
        the bar fills with - come when they were read recently and the
        boundary is a disk, its own or a shared one; a cap is filled by the
        unit's own use alone."""
        fresh = filled and recent(stamp)
        volume_total = _amount(t.get(f"{volume}_total_bytes")) if fresh else None
        volume_used = t.get(f"{volume}_used_bytes") if volume_total else None
        return {"used_bytes": used, "total_bytes": own or volume_total,
                "shared": not own, "volume_used_bytes": volume_used,
                "volume_total_bytes": volume_total}

    # A runner's own disk (T-27): its size as the agent measured it. Once a
    # deep beat has said - null when there is none - that is the answer;
    # before, what the controller told the disk to be, the same number,
    # known since before create.
    if "storage_total_bytes" in t:
        own_disk = _amount(t["storage_total_bytes"])
    else:
        own_disk = _first(t.get("disk_limit_bytes"), spec.get("disk_limit"))
    storage = bounded(
        t.get("storage_bytes") if recent("storage_at", "deep_at") else None,
        own_disk, "storage_volume", "storage_volume_at")
    # The cache's own boundary: its cap, or a disk of the unit's own it
    # lives on. What the agent said outranks the fleet's policy, which is
    # only the answer until it has.
    if "cache_cap_bytes" in t or "cache_total_bytes" in t:
        cap = _amount(t.get("cache_cap_bytes"))
        cache_disk = _amount(t.get("cache_total_bytes"))
    else:
        cap = _first((spec.get("cache_policy") or {}).get("max_bytes"))
        cache_disk = None
    measured = bounded(
        t.get("cache_bytes") if recent("cache_at", "deep_at") else None,
        cap or cache_disk, "cache_volume", "cache_volume_at",
        filled=cap is None)
    cache = {"used_bytes": measured.pop("used_bytes"), "cap_bytes": cap,
             **measured}
    return cpu, memory, storage, cache


def _job_telemetry(spec, override, now):
    from datetime import datetime, timezone
    if override is not None:
        return override
    t = spec.get("telemetry") or {}
    age = _age(t.get("at"), now or datetime.now(timezone.utc))
    return t if age is not None and 0 <= age <= HEARTBEAT_FRESH else None


def _job(lifecycle, telemetry):
    """The running job's name when something reported it. A busy runner
    whose job nobody named says it is running one - "no active job" on a
    busy card would be false (T-1803). Naming the job needs the forge's job
    API, which is still to be added."""
    if lifecycle not in ("busy", "draining"):
        return None
    named = (telemetry or {}).get("job")
    if isinstance(named, str) and named:
        return named
    if lifecycle == "busy":
        return "running a job - the forge does not say which"
    return None


def from_spec(spec, telemetry=None, worker_reachable=None, now=None,
              host=None):
    """A card for a runner the controller manages, from its RunnerSpec.
    `host` is the hardware its worker declared ({"cpus", "memory_bytes"},
    control/hardware.py): what a runner with no limit of its own shares,
    when its heartbeat does not say."""
    rid = spec["runner_id"]
    caps = dict(spec.get("capabilities") or {})
    state = spec.get("actual_state") or "unknown"
    ready = readiness(spec, now)
    forge = {"github": "GitHub", "forgejo": "Forgejo"}.get(
        spec.get("provider"), spec.get("provider"))
    who = spec.get("display_name") or f"rnr-{rid[:8]}"
    visible = _visible("stopped" if state in ("stopped", "failed")
                       else "draining" if state in ("draining", "drained")
                       else state)
    needs = {"drain": "supports_drain", "cancel_drain": "supports_drain",
             "clear_cache": "clear_cache"}
    actions = []
    for verb in ACTIONS:
        if verb == "logs":
            actions.append(_action("logs", method="LINK",
                                   url=f"/runners/{rid}"))
            continue
        capability = needs.get(verb)
        if capability and caps.get(capability) is False:
            actions.append(_action(verb, enabled=False,
                                   visible=visible[verb],
                                   reason=ANNOTATIONS[capability]))
            continue
        actions.append(_action(
            verb, visible=visible[verb],
            url=f"/api/v2/runners/{rid}/actions/{verb}", body={},
            confirm=_confirm(verb, who, forge), idempotent=True))
    cpu, memory, storage, cache = _measured(spec, telemetry, now, host)
    reachable = None if worker_reachable is None else         bool(worker_reachable) and ready["process"] == "up"
    return _card(
        runner_id=rid,
        display_name=who,
        provider=spec.get("provider"),
        platform=spec.get("platform"),
        architecture=spec.get("architecture"),
        labels=spec.get("forge_labels"),
        label_drift=_drift(spec.get("last_note")),
        worker=spec.get("host_id"),
        runtime=caps.get("kind"),
        state=_shown_state(state, ready),
        job=_job(state, _job_telemetry(spec, telemetry, now)),
        cpu=cpu,
        memory=memory,
        storage=storage,
        cache=cache,
        reachable=reachable,
        readiness=ready,
        last_seen_at=spec.get("last_seen_at"),
        current_operation=spec.get("current_operation"),
        last_error=spec.get("last_error"),
        last_note=spec.get("last_note"),
        capabilities=caps,
        key=f"rnr:{rid}",
        source="controller",
        lifecycle_state=state,
        href=f"/runners/{rid}",
        fleet_id=spec.get("fleet_id"),
        actions=actions,
    )
