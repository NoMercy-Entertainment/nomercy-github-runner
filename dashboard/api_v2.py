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
import time

from flask import Blueprint, jsonify, request, session

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


def control_plane():
    """(service, note). The service is None when the control plane has not
    run: nothing here creates its database."""
    path = _db_path()
    if not os.path.exists(path):
        return None, "the control plane has not run yet"
    from control.service import RunnerService
    try:
        return RunnerService(path), None
    except Exception:   # noqa: BLE001 - a half-made database is "not ready"
        return None, "the control database is not ready"


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
    return [cards.from_spec(s, worker_reachable=(s.get("host_id") in healthy)
                            if s.get("host_id") else None)
            for s in specs if s.get("actual_state") != "absent"]


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
    return out


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
    out = []
    for provider_key, platform, arch in CELLS:
        fid = fleet_id(provider_key, platform, arch)
        provider = providers.by_key(provider_key)
        row = rows.get(fid)
        if row is not None:
            available, reason = bool(row["available"]), \
                row.get("unavailable_reason")
            desired = row["desired_capacity"]
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
    return out


FORGE_NAMES = {"github": "GitHub", "forgejo": "Forgejo"}
PLATFORM_NAMES = {providers.LINUX: "Linux", providers.WINDOWS: "Windows",
                  providers.MACOS: "macOS"}

#: The fleets today's containers belong to, and so the fleets whose actions
#: still go to v1 until T-1407. Data, looked up - not a branch in the page.
V1_FLEETS = {("github", providers.LINUX), ("forgejo", providers.LINUX)}

FLEET_ACTIONS = ("add", "capacity", "recreate", "clear_cache")
FLEET_LABELS = {"add": "+ Add runner", "capacity": "Capacity",
                "recreate": "Recreate fleet", "clear_cache": "Clear all cache"}
FLEET_TONES = {"add": "primary", "recreate": "warn", "clear_cache": "warn"}


def _fleet_action(verb, url=None, body=None, reason=None, confirm=None,
                  idempotent=False, visible=True, prompt=None):
    return {"verb": verb, "label": FLEET_LABELS[verb],
            "tone": FLEET_TONES.get(verb, ""), "url": url, "body": body,
            "enabled": bool(url), "reason": None if url else reason,
            "confirm": confirm, "idempotent": idempotent, "visible": visible,
            "prompt": prompt}


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
            _fleet_action("capacity", reason="capacity comes with the "
                                             "control plane; add or remove "
                                             "runners one at a time"),
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
        _fleet_action("capacity", f"{base}/capacity", {}, idempotent=True,
                      prompt="How many runners should this fleet have?"),
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
    service, note = control_plane()
    runner_cards = all_cards(service)
    snap = _status["fn"]() or {}
    return jsonify(
        generated=snap.get("generated") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        control_plane={"running": service is not None, "note": note},
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
