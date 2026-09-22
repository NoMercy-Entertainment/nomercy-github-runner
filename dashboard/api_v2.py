"""API v2: runners by runner_id, fleets by fleet_id, and one card for each.

Design 14.2, 14.3 and 14.6. It lands beside v1 rather than replacing it
(MIG-6): v1 keeps serving today's containers by name until T-1407, and v2 is
where everything new goes. The v2 page reads only from here.

**Reads work whether or not the control plane has run.** The page's runners
come from two places during the transition - the controller's RunnerSpecs,
and the v1 collector's snapshot of today's containers - and both become the
same card (`cards.py`). An absent control database is not an error and is
never created from here: the controller is not running, and the page says so.

**Mutations need the control plane and an idempotency key.** Every POST is
recorded as an operation for the reconciler to carry out, never performed in
the request; a caller that did not hear the answer repeats the call with the
same key and gets the same operation back (design 12.3). A request without a
key is refused - the one safe move a caller has only exists if the first call
carried a key.
"""
import os
import json
import sqlite3
import time
from datetime import datetime, timezone

from flask import Blueprint, g, has_request_context, jsonify, request, session

import cards
import providers

bp = Blueprint("api_v2", __name__)

#: How the app hands over its v1 status snapshot. Set by `init`.
_status = {"fn": lambda: {}}


def init(app, status):
    """Register the routes. `status` returns the v1 collector's latest
    snapshot, whatever the app keeps it in."""
    _status["fn"] = status
    app.register_blueprint(bp)


# ---------------------------------------------------------------------------
# the control plane, when it exists
# ---------------------------------------------------------------------------

def _db_path():
    from store import schema
    return schema.DB_PATH


class _LazyAgentClient:
    """Database reads stay available while TLS credentials are being repaired."""

    def __init__(self, inventory, tls_dir, path):
        self.inventory, self.tls_dir, self.path = inventory, tls_dir, path

    def call_and_wait(self, *args, **kwargs):
        from control.agent_client import AgentClient
        from control.certificates import resolve_paths
        client = AgentClient(self.inventory, *resolve_paths(self.tls_dir, "controller"),
                             audit_path=self.path)
        return client.call_and_wait(*args, **kwargs)


def control_plane():
    """(service, note). The service is None when the control plane has not
    run: nothing here creates its database."""
    path = _db_path()
    if not os.path.exists(path):
        return None, "the control plane has not run yet"
    from control.service import RunnerService
    try:
        # With the deployment's settings: what a unit of a cell is made from
        # is one of them, and a service without them reads every cell as
        # unbuildable (2026-09-20).
        from control import agent_runtime
        from control.main import TLS_DIR, unit_images, unit_memory
        from control.secrets import SecretStore
        env = SecretStore(path).overlay(os.environ)
        service = RunnerService(path, runtimes=agent_runtime.TABLE, env=env)
        service.agents = agent_runtime.AgentWiring(
            _LazyAgentClient(service.inventory, TLS_DIR, path), operations=service.operations,
            images=unit_images(env), memory=unit_memory(env))
        return service, None
    except Exception:   # noqa: BLE001 - a half-made database is "not ready"
        return None, "the control database is not ready"


def controller_health(path=None):
    from store import schema
    state = {"running": False, "note": "controller heartbeat unavailable",
             "maintenance": False}
    path = path or _db_path()
    if not os.path.exists(path):
        return state
    try:
        with schema.connect(path) as c:
            row = c.execute("SELECT * FROM controller_status WHERE id=1").fetchone()
            maintenance = c.execute("SELECT value FROM platform_settings WHERE key='maintenance'").fetchone()
        state["maintenance"] = bool(maintenance and maintenance[0] in ("true", "1"))
        if row:
            at = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - at).total_seconds()
            state.update(last_seen_at=row["last_seen_at"], state=row["state"],
                         last_error=row["last_error"])
            state["running"] = -5 <= age <= 45 and row["state"] not in ("stopped", "failed")
            state["note"] = ("maintenance: reconciliation paused" if state["maintenance"]
                             else row["last_error"] or row["state"] if state["running"]
                             else "controller heartbeat expired")
    except (sqlite3.Error, ValueError, TypeError):
        pass
    return state


def _action_policy(items, health=None):
    health = health or controller_health()
    role = getattr(g, "role", None) if has_request_context() else "admin"
    for item in items:
        for action in item.get("actions", []):
            if action.get("method") == "LINK":
                continue
            why = None
            if role == "viewer":
                why = "read-only access"
            elif role != "admin" and action.get("verb") in ("remove", "recreate", "deregister"):
                why = "requires admin"
            elif health.get("maintenance"):
                why = "maintenance is enabled"
            elif not health.get("running"):
                why = health.get("note") or "controller is not running"
            if why and (action.get("url") or "").startswith("/api/v2/"):
                action.update(enabled=False, reason=why)
    return items


def _controller_cards(service):
    if service is None:
        return []
    try:
        specs = service.specs.list()
    except Exception:   # noqa: BLE001
        return []
    healthy = set()
    try:
        healthy = {w["host_id"] for w in service.inventory.healthy()}
    except Exception:   # noqa: BLE001
        pass
    return [_runner_card(service, s, s.get("host_id") in healthy
                         if s.get("host_id") else None)
            for s in specs if s.get("actual_state") != "absent"]


def _runner_card(service, spec, reachable):
    card = cards.from_spec(spec, worker_reachable=reachable)
    from control import states
    if spec.get("actual_state") == "failed":
        card["actions"].append({"verb": "repair", "label": "Repair", "tone": "primary",
            "visible": True, "enabled": True, "method": "POST", "body": {},
            "url": f"/api/v2/runners/{spec['runner_id']}/actions/repair", "idempotent": True})
    for action in card["actions"]:
        verb = action["verb"]
        if verb == "logs":
            continue
        reason = None
        if spec.get("current_operation"):
            reason = "an operation is already in progress"
        elif verb in ("recreate", "repair"):
            buildable, reason = service.buildable(spec["fleet_id"])
            if buildable:
                reason = None
            if verb == "recreate" and not reason:
                from control.service import Refused
                try:
                    service.validate_replacement(spec)
                except Refused as e:
                    reason = str(e)
        elif verb != "remove":
            if service._drains_first(spec, verb, spec["actual_state"]) and action.get("url"):
                action.update(enabled=True, visible=True, reason=None)
            elif not states.allows(verb, spec["actual_state"]):
                reason = f"{verb} is unavailable while {spec['actual_state']}"
        if reason:
            action.update(enabled=False, reason=reason)
    return card


# ---------------------------------------------------------------------------
# the cards
# ---------------------------------------------------------------------------

def all_cards(service=None):
    """Every runner the dashboard can see, as design 14.1's card."""
    snap = _status["fn"]() or {}
    generated = snap.get("generated")
    out = [cards.from_legacy(r, snap.get("host"), generated)
           for r in snap.get("runners") or []]
    out += [cards.from_unmanaged(e, generated)
            for e in snap.get("elsewhere") or []]
    out += _controller_cards(service)
    return _action_policy(out)


def fleet_list(service, note, all_runner_cards):
    """The six fleets - rows, not code (design 14.4). From the controller's
    table when it exists, else from what each provider says it supports, so
    the page shows the same six before the control plane has run."""
    from store.fleets import CELLS, fleet_id
    snap = _status["fn"]() or {}
    env_configured = snap.get("providers_configured") or {}
    rows = {}
    if service is not None:
        try:
            rows = {f["fleet_id"]: f for f in service.fleets.list()}
        except Exception:   # noqa: BLE001
            rows = {}
    # The table's rows when there is a table - so a seventh fleet appears
    # the moment it is a row, with no code change (T-1404) - and the six
    # cells the providers know otherwise, so the page is the same before the
    # control plane has ever run.
    coordinates = [(f["provider"], f["platform"], f["architecture"])
                   for f in rows.values()] or list(CELLS)
    coordinates += [c for c in CELLS if c not in coordinates]
    out = []
    for provider_key, platform, arch in coordinates:
        fid = fleet_id(provider_key, platform, arch)
        provider = providers.by_key(provider_key)
        if provider is None:
            continue
        row = rows.get(fid)
        if row is not None:
            available, reason = bool(row["available"]), \
                row.get("unavailable_reason")
            desired = row["desired_capacity"]
            if available:
                # What the forge supports is not what this deployment can
                # build: a worker that makes units from templates can only
                # make the ones it has, and offering `+ Add runner` for the
                # others is a page that lies (2026-09-20).
                try:
                    can, why = service.buildable(fid)
                except Exception:       # noqa: BLE001
                    can, why = False, "buildability could not be verified"
                if not can:
                    available, reason = False, why
                elif why and not reason:
                    reason = why
        else:
            support = provider.supports(platform, arch, {})
            available, reason, desired = bool(support), support.reason, None
        members = [c["key"] for c in all_runner_cards
                   if c.get("fleet_id") == fid]
        out.append({
            "fleet_id": fid, "provider": provider_key, "platform": platform,
            "architecture": arch,
            "title": f"{FORGE_NAMES.get(provider_key, provider_key)} · "
                     f"{PLATFORM_NAMES.get(platform, platform)} · {arch}",
            "available": available, "reason": reason or None,
            "desired": desired, "runners": members,
            "actions": fleet_actions(fid, provider_key, platform, available,
                                     reason, service is not None, note,
                                     env_configured.get(provider_key),
                                     members),
        })
        if service and fid in rows:
            support = service.fleets.resource_support(fid)
            out[-1]["resource_support"] = support
            out[-1]["resource_notice"] = _resource_notice(rows[fid], support)
    return _action_policy(out)


FORGE_NAMES = {"github": "GitHub", "forgejo": "Forgejo"}
PLATFORM_NAMES = {providers.LINUX: "Linux", providers.WINDOWS: "Windows",
                  providers.MACOS: "macOS"}

#: The fleets today's containers belong to, and so the fleets whose actions
#: still go to v1 until T-1407. Data, looked up - not a branch in the page.
V1_FLEETS = {("github", providers.LINUX), ("forgejo", providers.LINUX)}

#: The controller exposes desired capacity explicitly. The UI validates and
#: submits the entered count; reducing capacity requires an administrator.
FLEET_ACTIONS = ("add", "capacity", "recreate", "clear_cache")
FLEET_LABELS = {"add": "+ Add runner", "recreate": "Recreate fleet",
                "clear_cache": "Clear all cache", "capacity": "Set capacity"}
FLEET_TONES = {"add": "primary", "recreate": "warn", "clear_cache": "warn"}


def _fleet_action(verb, url=None, body=None, reason=None, confirm=None,
                  idempotent=False, visible=True):
    return {"verb": verb, "label": FLEET_LABELS[verb],
            "tone": FLEET_TONES.get(verb, ""), "url": url, "body": body,
            "enabled": bool(url), "reason": None if url else reason,
            "confirm": confirm, "idempotent": idempotent, "visible": visible}


def fleet_actions(fid, provider_key, platform, available, reason,
                  controller_up, note, v1_configured, members):
    title = FORGE_NAMES.get(provider_key, provider_key)
    recreate_confirm = {"title": f"Recreate the {title} "
                                 f"{PLATFORM_NAMES.get(platform, platform)} "
                                 f"fleet?",
                        "body": "Each runner is removed and rebuilt with the "
                                "current settings.",
                        "confirm": "Recreate"}
    clear_confirm = {"title": "Clear cache on every idle runner?",
                     "body": "Busy runners are skipped and left alone.",
                     "confirm": "Clear all"}
    has_runners = bool(members)
    if not available:
        why = reason or "this fleet does not exist on this platform"
        return [_fleet_action(v, reason=why) for v in FLEET_ACTIONS]
    if (provider_key, platform) in V1_FLEETS and v1_configured:
        return [
            _fleet_action("add", "/api/runner/add", {"provider": provider_key}),
            _fleet_action("recreate", "/api/recreate",
                          {"provider": provider_key},
                          confirm=recreate_confirm, visible=has_runners),
            _fleet_action("clear_cache", "/api/prune-all",
                          {"provider": provider_key}, confirm=clear_confirm,
                          visible=has_runners),
        ]
    if not controller_up:
        return [_fleet_action(v, reason=note) for v in FLEET_ACTIONS]
    base = f"/api/v2/fleets/{fid}"
    return [
        _fleet_action("add", f"{base}/runners", {}, idempotent=True),
        _fleet_action("capacity", f"{base}/capacity", {}, idempotent=True),
        _fleet_action("recreate", f"{base}/recreate", {}, idempotent=True,
                      confirm=recreate_confirm, visible=has_runners),
        _fleet_action("clear_cache", f"{base}/clear-cache", {},
                      idempotent=True, confirm=clear_confirm,
                      visible=has_runners),
    ]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

@bp.route("/api/v2/fleet")
def fleet_page_data():
    """Everything the v2 page renders, in one read."""
    return jsonify(fleet_snapshot())


def fleet_snapshot():
    service, note = control_plane()
    runner_cards = all_cards(service)
    snap = _status["fn"]() or {}
    return dict(
        generated=snap.get("generated") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        control_plane=controller_health(),
        workers=service.inventory.summary() if service else [],
        fleets=fleet_list(service, note, runner_cards),
        runners=runner_cards,
        disk=snap.get("disk"))


@bp.route("/api/v2/runners")
def runners_list():
    service, _ = control_plane()
    return jsonify(runners=all_cards(service))


@bp.route("/api/v2/fleets")
def fleets_list():
    service, note = control_plane()
    return jsonify(fleets=fleet_list(service, note, all_cards(service)))


def requested_by():
    return session.get("email") or session.get("sub") or "unknown"


# ---------------------------------------------------------------------------
# the eighteen verbs (T-1301, T-1402)
# ---------------------------------------------------------------------------

RUNNER = "/api/v2/runners/<runner_id>"
FLEET = "/api/v2/fleets/<fleet_id>"

#: Design 12.2's eighteen verbs and where each is served. Mutations are POSTs
#: carrying an Idempotency-Key; the three reads are GETs. A test parses the
#: verb list out of the design and asserts it is exactly these keys.
ROUTES = {
    "create": ("POST", f"{FLEET}/runners"),
    "scale_up": ("POST", f"{FLEET}/capacity"),
    "scale_down": ("POST", f"{FLEET}/capacity"),
    "provision": ("POST", f"{RUNNER}/actions/provision"),
    "register": ("POST", f"{RUNNER}/actions/register"),
    "start": ("POST", f"{RUNNER}/actions/start"),
    "stop": ("POST", f"{RUNNER}/actions/stop"),
    "restart": ("POST", f"{RUNNER}/actions/restart"),
    "drain": ("POST", f"{RUNNER}/actions/drain"),
    "cancel_drain": ("POST", f"{RUNNER}/actions/cancel_drain"),
    "recreate": ("POST", f"{RUNNER}/actions/recreate"),
    "remove": ("POST", f"{RUNNER}/actions/remove"),
    "deregister": ("POST", f"{RUNNER}/actions/deregister"),
    "repair": ("POST", f"{RUNNER}/actions/repair"),
    "clear_cache": ("POST", f"{RUNNER}/actions/clear_cache"),
    "fetch_status": ("GET", f"{RUNNER}/status"),
    "fetch_logs": ("GET", f"{RUNNER}/logs"),
    "inspect_resources": ("GET", f"{RUNNER}/resources"),
}

#: The verbs one runner's action route takes.
RUNNER_ACTIONS = frozenset(v for v, (m, path) in ROUTES.items()
                           if path.startswith(f"{RUNNER}/actions/"))

#: A verb that needs a capability, and the capability. Refused with its
#: reason when the runner declares it false, rather than recorded as an
#: operation that can only fail later (design 14.2).
NEEDS = {"drain": "supports_drain", "cancel_drain": "supports_drain",
         "clear_cache": "clear_cache"}


def _refuse(status, reason, **extra):
    return jsonify(ok=False, error=reason, **extra), status


def _key():
    return (request.headers.get("Idempotency-Key") or "").strip() or None


def _need_plane():
    service, note = control_plane()
    if service is None:
        return None, _refuse(503, note)
    if request.method == "POST" and controller_health().get("maintenance"):
        if not request.path.startswith(("/api/v2/maintenance", "/api/v2/secrets", "/api/v2/settings")):
            return None, _refuse(409, "maintenance is enabled; runner changes are paused")
    return service, None


@bp.route(f"{RUNNER}/actions/<verb>", methods=["POST"])
def runner_action(runner_id, verb):
    """Every runner action of every platform, through one handler.

    Refusals are answers with a reason, never a 500: an unknown verb, a verb
    that is a read or acts on a fleet, a missing idempotency key, a
    capability the runner does not have, or a state the verb cannot start
    from."""
    if verb not in RUNNER_ACTIONS:
        if verb in ROUTES:
            method, path = ROUTES[verb]
            return _refuse(400, f"{verb} is not a runner action; it is "
                                f"{method} {path}")
        return _refuse(404, f"no action {verb!r}; the actions are "
                            f"{sorted(RUNNER_ACTIONS)}")
    key = _key()
    if key is None:
        return _refuse(400, "an Idempotency-Key header is required: it is "
                            "what makes repeating this request safe")
    service, err = _need_plane()
    if err:
        return err
    from control.service import Refused, UnknownRunner
    spec = service.specs.get(runner_id)
    if spec is None:
        return _refuse(404, f"no runner {runner_id}")
    capability = NEEDS.get(verb)
    if capability and (spec.get("capabilities") or {}).get(capability) \
            is False:
        return _refuse(409, f"{verb}: this runner "
                            f"{cards.ANNOTATIONS[capability]}")
    try:
        if verb == "remove":
            # An operator's remove is "this runner may go": desired absent,
            # which the reconciler walks gracefully, and one fewer runner
            # wanted so it does not come back (2026-09-20). The machine's
            # own `remove` edge is the reconciler's, further down that walk.
            op = service.retire(runner_id, requested_by=requested_by(),
                                idempotency_key=key)
        else:
            op = service.act(runner_id, verb, requested_by=requested_by(),
                             idempotency_key=key)
    except UnknownRunner:
        return _refuse(404, f"no runner {runner_id}")
    except Refused as e:
        return _refuse(409, str(e))
    except ValueError as e:
        return _refuse(400, str(e))
    return jsonify(ok=True, operation_id=op), 202


@bp.route(f"{RUNNER}")
def runner_detail(runner_id):
    service, err = _need_plane()
    if err:
        return err
    spec = service.specs.get(runner_id)
    if spec is None:
        return _refuse(404, f"no runner {runner_id}")
    from control.redact import redact_mapping
    operations = service.operations.list(runner_id=runner_id, limit=20)
    reachable = spec.get("host_id") in {w["host_id"] for w in service.inventory.healthy()} if spec.get("host_id") else None
    card = _runner_card(service, spec, reachable)
    _action_policy([card])
    fleet = service.fleets.get(spec.get("fleet_id"))
    resource_notice = None
    if fleet and fleet["platform"] == "macos":
        from control.inventory import current_resource_enforcement
        support = service.fleets.resource_support(fleet["fleet_id"], host_id=spec.get("host_id") or "")
        proof = current_resource_enforcement(spec) if support["appliance_per_runner"] else None
        verified_support = dict(support,
            per_runner_memory_overhead_bytes=proof["memory_overhead_bytes"] if proof else None,
            guest_disk_virtual_bytes=None)  # Worker defaults do not prove this disk's size.
        resource_notice = (_resource_notice(fleet, verified_support, current=True) if proof else
                           "This runner's current CPU/RAM enforcement is unverified. Saved limits alone do not confirm enforcement.")
        if proof:
            resource_notice += (f" Observed limits: {proof['cpu_cores']} CPUs and "
                                f"{proof['memory_limit_bytes'] / 1024**3:g} GiB guest RAM.")
    return jsonify(card=card, spec=redact_mapping(spec),
                   operations=redact_mapping(operations),
                   audit=_audit_tail(runner_id), resource_notice=resource_notice)


def _audit_tail(runner_id, limit=20):
    """The last records about this runner, newest first. Empty when there is
    no audit yet."""
    try:
        from control import audit
        return audit.entries(_db_path(), runner_id=runner_id, limit=limit)
    except Exception:   # noqa: BLE001 - no audit table is no audit
        return []


def _read(runner_id, fn):
    service, err = _need_plane()
    if err:
        return err
    from control.service import UnknownRunner
    try:
        return jsonify(ok=True, result=fn(service))
    except UnknownRunner:
        return _refuse(404, f"no runner {runner_id}")
    except Exception as e:  # noqa: BLE001 - a read that failed says why
        return _refuse(502, f"{type(e).__name__}: {e}"[:300])


@bp.route(f"{RUNNER}/status")
def runner_status(runner_id):
    return _read(runner_id, lambda s: _plain(s.fetch_status(runner_id)))


@bp.route(f"{RUNNER}/logs")
def runner_logs(runner_id):
    try:
        since = max(1, min(86400, int(request.args.get("since", 300))))
    except ValueError:
        return _refuse(400, "since must be a number of seconds")
    return _read(runner_id, lambda s: s.fetch_logs(runner_id,
                                                   since_seconds=since))


@bp.route(f"{RUNNER}/resources")
def runner_resources(runner_id):
    return _read(runner_id,
                 lambda s: _plain(s.inspect_resources(runner_id)))


@bp.route("/api/v2/operations/<operation_id>")
def operation(operation_id):
    """An operation's attempts and trace - runbook 22.1's second step."""
    service, err = _need_plane()
    if err:
        return err
    op = service.operations.get(operation_id)
    if op is None:
        return _refuse(404, f"no operation {operation_id}")
    from control.redact import redact_mapping
    return jsonify(operation=redact_mapping(op))


def _plain(value):
    """A runtime's answer as JSON: dataclasses become dicts."""
    import dataclasses
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return value


# ---------------------------------------------------------------------------
# fleets (T-1403)
# ---------------------------------------------------------------------------

def _fleet_or_404(service, fleet_id):
    fleet = service.fleets.get(fleet_id)
    if fleet is None:
        return None, _refuse(404, f"no fleet {fleet_id}")
    return fleet, None


def _fleet_mutation(fleet_id):
    """What every fleet POST needs first: a key, the control plane, the
    fleet. Returns (service, fleet, key, error)."""
    key = _key()
    if key is None:
        return None, None, None, _refuse(
            400, "an Idempotency-Key header is required: it is what makes "
                 "repeating this request safe")
    service, err = _need_plane()
    if err:
        return None, None, None, err
    fleet, err = _fleet_or_404(service, fleet_id)
    if err:
        return None, None, None, err
    return service, fleet, key, None


@bp.route(f"{FLEET}")
def fleet_detail(fleet_id):
    service, note = control_plane()
    for f in fleet_list(service, note, all_cards(service)):
        if f["fleet_id"] == fleet_id:
            return jsonify(fleet=f)
    return _refuse(404, f"no fleet {fleet_id}")


@bp.route(f"{FLEET}/capacity", methods=["POST"])
def fleet_capacity(fleet_id):
    """Scale up and scale down are this one call with a different number
    (design 14.3)."""
    service, fleet, key, err = _fleet_mutation(fleet_id)
    if err:
        return err
    desired = (request.get_json(silent=True) or {}).get("desired")
    if isinstance(desired, bool) or not isinstance(desired, int) or             desired < 0:
        return _refuse(400, "desired must be a whole number, 0 or more")
    # A decrease removes runners: design 18.2's destroy group (T-1902).
    from flask import g
    if desired < fleet["desired_capacity"] and             getattr(g, "role", None) != "admin":
        from control import audit
        audit.record(_db_path(), "set_capacity", "refused",
                     actor=requested_by(), fleet_id=fleet_id,
                     parameters={"desired": desired},
                     outcome="a decrease requires admin")
        return _refuse(403, "Reducing a fleet's capacity needs the admin "
                            "role.")
    from control.service import Refused
    try:
        op = service.set_capacity(fleet_id, desired,
                                  requested_by=requested_by(),
                                  idempotency_key=key)
    except Refused as e:
        return _refuse(409, str(e))
    return jsonify(ok=True, operation_id=op,
                   note=f"{fleet_id} wants {desired}"), 202


@bp.route(f"{FLEET}/runners", methods=["POST"])
def fleet_add(fleet_id):
    """One more runner: which is to say, a higher capacity."""
    service, fleet, key, err = _fleet_mutation(fleet_id)
    if err:
        return err
    from control.service import Refused
    try:
        op = service.create(fleet_id, 1, requested_by=requested_by(),
                            idempotency_key=key)
    except Refused as e:
        return _refuse(409, str(e))
    return jsonify(ok=True, operation_id=op,
                   note=f"{fleet_id}: one more runner"), 202


def _each_runner(service, fleet_id, verb, key, skip):
    """Ask for `verb` on every runner of a fleet. Each gets a key derived
    from the request's, so repeating the request repeats nothing. A runner
    it cannot apply to is reported with the reason, never silently left
    out."""
    from control.service import Refused
    results = []
    for spec in service.specs.list(fleet_id=fleet_id):
        rid = spec["runner_id"]
        why = skip(spec)
        if why:
            results.append({"runner_id": rid, "ok": False, "skipped": why})
            continue
        try:
            op = service.act(rid, verb, requested_by=requested_by(),
                             idempotency_key=f"{key}:{rid}")
            results.append({"runner_id": rid, "ok": True,
                            "operation_id": op})
        except Refused as e:
            results.append({"runner_id": rid, "ok": False,
                            "error": str(e)})
    return results


@bp.route(f"{FLEET}/recreate", methods=["POST"])
def fleet_recreate(fleet_id):
    service, fleet, key, err = _fleet_mutation(fleet_id)
    if err:
        return err
    results = _each_runner(service, fleet_id, "recreate", key,
                           lambda spec: None)
    return jsonify(ok=all(r["ok"] for r in results), results=results), 202


@bp.route(f"{FLEET}/clear-cache", methods=["POST"])
def fleet_clear_cache(fleet_id):
    """Only idle and drained runners; a busy one is skipped with the reason,
    never cleared from under its job."""
    from control import states
    service, fleet, key, err = _fleet_mutation(fleet_id)
    if err:
        return err
    allowed = states.GUARDED["clear_cache"]

    def skip(spec):
        if spec["actual_state"] not in allowed and not service._drains_first(
                spec, "clear_cache", spec["actual_state"]):
            return f"{spec['actual_state']}: only idle or drained runners "                    f"are cleared"
        if (spec.get("capabilities") or {}).get("clear_cache") is False:
            return cards.ANNOTATIONS["clear_cache"]
        return None
    results = _each_runner(service, fleet_id, "clear_cache", key, skip)
    return jsonify(ok=all(r["ok"] or r.get("skipped") for r in results),
                   results=results), 202


# ---------------------------------------------------------------------------
# audit (T-1802)
# ---------------------------------------------------------------------------

@bp.route("/api/v2/audit")
def audit_log():
    """What was asked, by whom, and what became of it - refusals included.
    Newest first; filter by runner_id, fleet_id, verb or decision."""
    service, err = _need_plane()
    if err:
        return err
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 100))))
    except ValueError:
        return _refuse(400, "limit must be a number")
    from control import audit
    rows = audit.entries(_db_path(), verb=request.args.get("verb"),
                         runner_id=request.args.get("runner_id"),
                         fleet_id=request.args.get("fleet_id"),
                         decision=request.args.get("decision"), limit=limit)
    return jsonify(audit=rows)


# ---------------------------------------------------------------------------
# secrets (T-1901): set, never read back
# ---------------------------------------------------------------------------

@bp.route("/api/v2/secrets")
def secrets_status():
    """Which forge tokens the control plane holds, when they were set and by
    whom - never a value. Admin only (app.guard)."""
    service, err = _need_plane()
    if err:
        return err
    from control.secrets import SecretStore
    return jsonify(secrets=SecretStore(_db_path()).status())


@bp.route("/api/v2/secrets/<name>", methods=["POST"])
def secret_set(name):
    """Set one token. The answer says it is set; it never echoes it."""
    service, err = _need_plane()
    if err:
        return err
    from control import audit
    from control.secrets import SecretRefused, SecretStore
    value = (request.get_json(silent=True) or {}).get("value")
    try:
        SecretStore(_db_path()).set(name, value, requested_by())
    except SecretRefused as e:
        audit.record(_db_path(), "set_secret", "refused",
                     actor=requested_by(), parameters={"name": name},
                     outcome=str(e))
        return _refuse(400, str(e))
    audit.record(_db_path(), "set_secret", "accepted", actor=requested_by(),
                 parameters={"name": name})
    return jsonify(ok=True, note=f"{name} is set")


@bp.route("/api/v2/secrets/<name>/clear", methods=["POST"])
def secret_clear(name):
    service, err = _need_plane()
    if err:
        return err
    from control import audit
    from control.secrets import SecretRefused, SecretStore
    try:
        SecretStore(_db_path()).clear(name)
    except SecretRefused as e:
        return _refuse(400, str(e))
    audit.record(_db_path(), "clear_secret", "accepted",
                 actor=requested_by(), parameters={"name": name})
    return jsonify(ok=True, note=f"{name} is cleared")


@bp.route("/api/v2/settings")
def settings_data():
    service, err = _need_plane()
    if err:
        return err
    fleets = service.fleets.list()
    for fleet in fleets:
        fleet["effective_template"] = service.unit_image(fleet)
        support = service.fleets.resource_support(fleet["fleet_id"])
        fleet.update(support)
        fleet["resource_notice"] = _resource_notice(fleet, support)
    return jsonify(fleets=fleets, control_plane=controller_health())


def _resource_notice(fleet, support, current=False):
    if fleet["platform"] == "linux":
        return ("CPU cores: a whole number pins each runner to its own window of that "
                "many cores, which is what nproc reports inside it; windows are spread "
                "so they overlap as little as possible. A fraction is a CPU quota and "
                "leaves nproc at the host's count. Existing runners keep their window "
                "on recreate unless the width changes.")
    if fleet["platform"] != "macos":
        return None
    if not support["appliance_per_runner"]:
        return ("CPU/RAM settings are locked: no healthy matching worker currently confirms a separate "
                "appliance with enforced CPU and RAM limits per runner. Legacy shared guests provide no such guarantee.")
    text = ("This runner uses a separate appliance with enforced CPU and guest RAM limits." if current else
            "New runners can use a separate appliance with enforced CPU and guest RAM limits. Existing runners require recreation to apply defaults.")
    overhead = support.get("per_runner_memory_overhead_bytes")
    if overhead is not None:
        text += f" Placement also reserves {overhead / 1024**3:g} GiB of appliance overhead per runner."
    else:
        text += " Appliance memory overhead depends on the selected worker; no common value is currently reported."
    disk = support.get("guest_disk_virtual_bytes")
    if disk and not support["disk_quota_supported"]:
        text += f" Guest disk size is fixed at {disk / 1024**3:g} GiB; configurable disk limits are unavailable."
    return text


@bp.route("/api/v2/settings/<fleet_id>", methods=["POST"])
def settings_save(fleet_id):
    service, err = _need_plane()
    if err:
        return err
    from store.fleets import UnknownFleet
    from control import audit
    try:
        fleet = service.fleets.set_defaults(fleet_id, request.get_json(silent=True))
    except UnknownFleet as e:
        return _refuse(404, str(e))
    except ValueError as e:
        return _refuse(400, str(e))
    audit.record(_db_path(), "set_defaults", "accepted", actor=requested_by(),
                 fleet_id=fleet_id, parameters=request.get_json())
    return jsonify(ok=True, fleet=fleet,
                   note="Saved. Defaults apply to new runners and recreations.")


@bp.route("/api/v2/maintenance", methods=["POST"])
def maintenance_set():
    if getattr(g, "role", None) != "admin":
        return _refuse(403, "Maintenance requires admin")
    service, err = _need_plane()
    if err:
        return err
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return _refuse(400, "enabled must be boolean")
    from store import schema
    from control import audit
    with schema.connect(_db_path()) as c:
        c.execute("INSERT INTO platform_settings(key,value) VALUES('maintenance',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("true" if enabled else "false",))
    audit.record(_db_path(), "maintenance", "accepted", actor=requested_by(),
                 parameters={"enabled": enabled})
    return jsonify(ok=True, enabled=enabled,
                   note="Reconciliation paused; already running jobs are unaffected."
                   if enabled else "Reconciliation resumed.")


@bp.route(f"{RUNNER}/history")
def runner_history(runner_id):
    service, err = _need_plane()
    if err:
        return err
    if service.specs.get(runner_id) is None:
        return _refuse(404, f"no runner {runner_id}")
    import history
    return jsonify(runs=history.list_runs(runner_id=runner_id, limit=30))


@bp.route(f"{RUNNER}/history/<int:run_id>")
def runner_history_run(runner_id, run_id):
    import history
    run = history.get_run(run_id)
    if not run or run.get("runner_id") != runner_id:
        return _refuse(404, "run not found for this runner")
    return jsonify(run)
