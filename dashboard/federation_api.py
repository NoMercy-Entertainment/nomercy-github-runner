"""Federation's restricted ingress and owner screens, alongside API v2."""
import hmac
import json
import os
import re
import secrets
import time

from flask import Blueprint, Response, g, jsonify, render_template, request, session, stream_with_context

import api_v2
from control.federation import Federation, Refused

bp = Blueprint("federation", __name__)
# Installed by the deployment's local controller integration, never a request.
adapter_factory = None
public_origin_provider = None
INGRESS = re.compile(r"^/api/v2/federation/peer/(?:capacity|requests(?:/[0-9a-f-]{36}(?:/extend)?)?)$")


def _body():
    if hasattr(g, "federation_body"):
        return g.federation_body
    raw = request.stream.read(32769)
    if len(raw) > 32768:
        raise Refused("Federation messages must be smaller than 32 KiB")
    try:
        value = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise Refused("A JSON object is required") from None
    if not isinstance(value, dict):
        raise Refused("A JSON object is required")
    g.federation_body = value
    return value


def plane():
    path = api_v2._db_path()
    if not os.path.exists(path):
        raise Refused("The control database is unavailable")
    adapter = adapter_factory(path) if adapter_factory else None
    if adapter_factory is None:
        from control.federation_provision import LocalProvisioner
        service, _ = api_v2.control_plane()
        if service:
            adapter = LocalProvisioner(service)
    return Federation(path, adapter)


def guard():
    """Return None for normal auth; peer bearers can reach exactly this ingress."""
    scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
    courier_routes = {"/api/v2/federation/broker/requests": "capacity", "/api/v2/federation/broker/challenge": "registration",
                      "/api/v2/federation/broker/results": "result"}
    if request.path in courier_routes or credential.startswith("nmc_"):
        if request.path not in courier_routes or request.method != "POST":
            return jsonify(error="Courier assertions authorise only their exact capacity envelope"), 403
        if scheme.casefold() != "bearer" or not credential.startswith("nmc_"):
            return jsonify(error="A dedicated courier assertion is required"), 401
        try:
            from control.federation_broker import Broker
            peer = Broker(plane().path).verify(credential, _body(), courier_routes[request.path])
        except Refused as error:
            return jsonify(error=str(error)), 403
        g.federation_peer = peer
        g.role = "federation-peer"
        return True
    if not credential.startswith("nrf_"):
        return None
    if scheme.casefold() != "bearer":
        return jsonify(error="A bearer peer credential is required"), 401
    if not INGRESS.fullmatch(request.path):
        return jsonify(error="Peer credentials cannot operate or inspect local runners"), 403
    try:
        peer = plane().authenticate(credential)
    except Refused:
        peer = None
    if not peer:
        return jsonify(error="Peer credential is not valid"), 401
    g.federation_peer = peer
    g.role = "federation-peer"
    return True


def init(app, public_origin=None):
    global public_origin_provider
    public_origin_provider = public_origin
    app.register_blueprint(bp)


def _csrf():
    csrf = request.headers.get("X-CSRF-Token", "")
    expected = session.get("federation_csrf", "")
    return bool(csrf and expected and hmac.compare_digest(csrf, expected))


@bp.before_request
def policy_guard():
    if request.content_length and request.content_length > 32768:
        return jsonify(error="Federation messages must be smaller than 32 KiB"), 413
    if request.method == "POST":
        # Read a bounded amount even if Content-Length was omitted or lied.
        raw = request.stream.read(32769) if not hasattr(g, "federation_body") else b""
        if len(raw) > 32768:
            return jsonify(error="Federation messages must be smaller than 32 KiB"), 413
        if not hasattr(g, "federation_body"):
            try:
                g.federation_body = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                return jsonify(error="A JSON object is required"), 400
        if not isinstance(g.federation_body, dict):
            return jsonify(error="A JSON object is required"), 400
    if getattr(g, "federation_peer", None):
        return None
    if getattr(g, "role", None) != "admin":
        return jsonify(error="Only a local admin can manage federation"), 403
    if request.method == "POST" and not _csrf():
        return jsonify(error="Refresh the federation page before changing access"), 403


@bp.errorhandler(Refused)
def refused(error):
    return jsonify(error=str(error)), 409


@bp.route("/federation")
def screen():
    session.setdefault("federation_csrf", secrets.token_urlsafe(32))
    try:
        ready = any(value["provisioning_available"] for value in (plane().capacity(peer["id"]) for peer in plane().peers() if peer["enabled"]))
        note = None
    except Refused as error:
        ready, note = False, str(error)
    import users
    return render_template("federation.html", csrf=session["federation_csrf"], people=users.list_users(),
                           provisioning_ready=ready, unavailable=note)


@bp.route("/api/v2/federation")
def snapshot():
    service = plane()
    capacity = [service.capacity(p["id"]) for p in service.peers() if p["enabled"]]
    from control.federation_broker import Broker
    from control.federation_outbound import Outbound
    response = jsonify(peers=service.peers(), requests=service.requests(),
                   activity=service.activity(), capacity=capacity,
                   provisioning_available=any(row["provisioning_available"] for row in capacity),
                   broker_available=Broker(service.path).settings().get("enabled", False),
                   outgoing=Outbound(service.path).results(session.get("sub")))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/v2/federation/peers", methods=["POST"])
def add_peer():
    value = g.federation_body
    if set(value) - {"name", "subject", "policy", "transport"} or not {"name", "subject", "policy"} <= set(value):
        raise Refused("A peer contains name, subject, local policy and optional transport")
    transport = value.get("transport", "direct")
    if transport == "broker":
        import users
        if not any(person["sub"] == value["subject"] for person in users.list_users()):
            raise Refused("Choose an existing authenticated account")
    identity, credential = plane().add_peer(value["name"], value["subject"], value["policy"],
                                           api_v2.requested_by(), transport)
    response = jsonify(id=identity, credential=credential, transport=transport)
    response.headers["Cache-Control"] = "no-store"
    return response, 201


@bp.route("/api/v2/federation/peers/<identity>/connect", methods=["POST"])
def connect_permission(identity):
    if g.federation_body:
        raise Refused("Connect an existing local permission without additional parameters")
    peer = next((row for row in plane().peers() if row["id"] == identity and row["enabled"] and row["transport"] == "broker"), None)
    if not peer:
        raise Refused("Select an enabled existing NoMercy person permission")
    return jsonify(outbound().grant(session["sub"], peer["subject"], True))


@bp.route("/api/v2/federation/peers/<identity>/limits", methods=["POST"])
def set_limits(identity):
    plane().set_policy(identity, g.federation_body, api_v2.requested_by())
    return jsonify(saved=True)


@bp.route("/api/v2/federation/peers/<identity>/revoke", methods=["POST"])
def revoke(identity):
    if g.federation_body:
        raise Refused("Revocation accepts no additional parameters")
    plane().revoke(identity, api_v2.requested_by())
    return jsonify(revoked=True)


@bp.route("/api/v2/federation/requests/<identity>/decision", methods=["POST"])
def decide(identity):
    value = g.federation_body
    if set(value) - {"approve", "reason"} or "approve" not in value:
        raise Refused("A decision contains approve and an optional reason")
    return jsonify(request=plane().decide(identity, value["approve"], api_v2.requested_by(), value.get("reason", "")))


@bp.route("/api/v2/federation/peer/capacity")
def peer_capacity():
    peer = getattr(g, "federation_peer", None)
    if not peer:
        return jsonify(error="A peer credential is required"), 403
    return jsonify(capacity=plane().capacity(peer["id"]))


@bp.route("/api/v2/federation/peer/requests", methods=["GET", "POST"])
def peer_requests():
    peer = getattr(g, "federation_peer", None)
    if not peer:
        return jsonify(error="A peer credential is required"), 403
    service = plane()
    if request.method == "GET":
        return jsonify(requests=service.requests(peer["id"]))
    from store import schema
    with schema.connect(service.path) as c:
        worker = c.execute("SELECT value FROM platform_settings WHERE key='federation_runtime'").fetchone()
    if not worker or worker[0] != "running" or not api_v2.controller_health(service.path)["running"]:
        raise Refused("The local capacity-sharing expiry worker is unavailable")
    value = g.federation_body
    if set(value) != {"request_key", "intent", "credential"}:
        raise Refused("A request contains request_key, bounded intent and registration credential")
    return jsonify(request=service.submit(peer["id"], value["request_key"], value["intent"], value["credential"])), 202


@bp.route("/api/v2/federation/peer/requests/<identity>")
def peer_request(identity):
    peer = getattr(g, "federation_peer", None)
    row = plane().get(identity)
    if not peer or row["peer_id"] != peer["id"]:
        return jsonify(error="Request is not owned by this peer"), 404
    return jsonify(request=row)


@bp.route("/api/v2/federation/peer/requests/<identity>/extend", methods=["POST"])
def extend(identity):
    peer = getattr(g, "federation_peer", None)
    if not peer:
        return jsonify(error="A peer credential is required"), 403
    value = g.federation_body
    if set(value) != {"minutes"}:
        raise Refused("An extension contains only minutes")
    return jsonify(request=plane().extend(identity, peer["id"], value["minutes"]))


@bp.route("/api/v2/federation/broker/settings", methods=["POST"])
def configure_broker():
    from control.federation_broker import Broker
    Broker(plane().path).configure(g.federation_body)
    return jsonify(saved=True)


@bp.route("/api/v2/federation/broker/challenge", methods=["POST"])
def broker_challenge():
    from control.federation_broker import Broker
    return jsonify(proof=Broker(plane().path).challenge(g.federation_body, g.federation_peer))


@bp.route("/api/v2/federation/broker/results", methods=["POST"])
def broker_result():
    from control.federation_outbound import Outbound
    Outbound(plane().path).receive(g.federation_peer, g.federation_body)
    return jsonify(received=True)


def outbound():
    from control.federation_outbound import Outbound
    service, note = api_v2.control_plane()
    if not service:
        raise Refused(note or "The local controller is unavailable")
    return Outbound(plane().path, service)


@bp.route("/api/v2/federation/broker/sign-in", methods=["POST"])
def sharing_sign_in():
    if g.federation_body:
        raise Refused("Sharing sign-in accepts no account secrets")
    session["federation_login"] = True
    return jsonify(redirect="/auth/start")


@bp.route("/api/v2/federation/broker/connect", methods=["POST"])
def connect_broker():
    if set(g.federation_body) != {"issuer", "name"}:
        raise Refused("Choose a known broker and fleet name")
    public = public_origin_provider() if public_origin_provider else ""
    return jsonify(outbound().register(session["sub"], g.federation_body["issuer"], g.federation_body["name"], public))


@bp.route("/api/v2/federation/broker/directory")
def directory():
    response = jsonify(fleets=outbound().directory(session["sub"]))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/v2/federation/broker/outgoing", methods=["POST"])
def send_request():
    value = g.federation_body
    if set(value) != {"target_fleet_id", "intent", "runner_group"}:
        raise Refused("Choose a known peer, intent and restricted group")
    return jsonify(request_key=outbound().submit(session["sub"], value["target_fleet_id"], value["intent"], value["runner_group"])), 202


@bp.route("/api/v2/federation/broker/retry-results", methods=["POST"])
def retry_results():
    if g.federation_body:
        raise Refused("Retry only the current owner's undelivered result events")
    return jsonify(outbound().call(session["sub"], "/retry-results", {}))


@bp.route("/api/v2/federation/lending", methods=["GET", "POST"])
def lending():
    from store import schema
    from control import placement
    local, note = api_v2.control_plane()
    if not local:
        raise Refused(note or "The local controller is unavailable")
    choices = {}
    for fleet in local.fleets.list():
        if fleet["provider"] != "github" or not fleet["available"]:
            continue
        choices[fleet["fleet_id"]] = sorted({template for worker in local.inventory.healthy()
            if worker["kind"] == placement.WORKER_KIND[fleet["platform"]] and
            placement.declared(worker, "kind") == placement.RUNTIME_KIND[fleet["platform"]] and
            placement.declared(worker, "federation_fork_guard") is True
            for template in (placement.declared(worker, "federation_templates") or [])})
    if request.method == "POST":
        value = g.federation_body
        if set(value) != {"enabled", "templates"} or type(value["enabled"]) is not bool or not isinstance(value["templates"], dict):
            raise Refused("Choose an enabled state and locally advertised guarded templates")
        if not value["templates"] or any(key not in choices or template not in choices[key] for key, template in value["templates"].items()):
            raise Refused("Only healthy workers' guarded local templates may be selected")
        with schema.connect(local.specs.path) as c:
            c.execute("BEGIN IMMEDIATE")
            for key, saved in (("federation_lending", json.dumps(value["enabled"])), ("federation_templates", json.dumps(value["templates"]))):
                c.execute("INSERT INTO platform_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, saved))
            if not value["enabled"]:
                c.execute("UPDATE federation_requests SET state='retiring',reason='Owner disabled lending',sealed_credential=NULL "
                          "WHERE state IN ('approved','activating','active')")
            c.execute("INSERT INTO federation_activity(at,actor,action,detail) VALUES(?,?,'lending-settings',?)",
                      (int(time.time()), api_v2.requested_by(), 'Enabled' if value["enabled"] else 'Disabled; existing leases retiring'))
        return jsonify(saved=True)
    with schema.connect(local.specs.path) as c:
        settings = {row[0]: row[1] for row in c.execute("SELECT key,value FROM platform_settings WHERE key IN ('federation_lending','federation_templates')")}
    return jsonify(enabled=settings.get("federation_lending") == "true", templates=json.loads(settings.get("federation_templates", "{}")), choices=choices)


@bp.route("/api/v2/federation/broker/requests", methods=["POST"])
def broker_request():
    service = plane()
    peer = getattr(g, "federation_peer", None)
    if not peer or peer["transport"] != "broker":
        raise Refused("An enabled local NoMercy person permission is required")
    from store import schema
    with schema.connect(service.path) as c:
        worker = c.execute("SELECT value FROM platform_settings WHERE key='federation_runtime'").fetchone()
    if not worker or worker[0] != "running" or not api_v2.controller_health(service.path)["running"]:
        raise Refused("The local capacity-sharing expiry worker is unavailable")
    value = g.federation_body
    if set(value) != {"request_key", "intent", "credential", "receipt"} or not isinstance(value["receipt"], dict) or value["receipt"].get("request_id") != value["request_key"]:
        raise Refused("A courier envelope contains only request identity, bounded intent, credential and matching receipt")
    from control.federation_broker import Broker
    broker = Broker(service.path)
    # Validate receipt before accepting capacity or persisting a registration credential.
    receipt = value["receipt"]
    if set(receipt) != {"request_id", "credential", "expires_at"} or not isinstance(receipt.get("credential"), str) or not re.fullmatch(r"[A-Za-z0-9]{64}", receipt["credential"]):
        raise Refused("A bounded matching broker receipt credential is required")
    if type(receipt["expires_at"]) is not int or not int(time.time()) < receipt["expires_at"] <= int(time.time()) + 2 * 7200 * 60 + 3600:
        raise Refused("Receipt expiry is invalid")
    row = service.submit(peer["id"], value["request_key"], value["intent"], value["credential"])
    broker.receipt(row, receipt, peer["verified_issuer"])
    return jsonify(request=row), 202


@bp.route("/api/v2/federation/events")
def events():
    raw = request.headers.get("Last-Event-ID") or request.args.get("after", "0")
    if not re.fullmatch(r"[0-9]{1,18}", raw):
        raise Refused("A nonnegative activity sequence is required")
    service = plane()
    from dbwatch import DatabaseChanges
    import users
    subject = session.get("sub")
    watch = DatabaseChanges(service.path)

    def records():
        sequence = int(raw)
        try:
            yield "retry: 5000\n\n"
            while users.role_of(subject) == "admin":
                for row in service.activity(sequence):
                    sequence = row["sequence"]
                    yield f"id: {sequence}\nevent: change\ndata: {json.dumps(row)}\n\n"
                if not watch.wait(30):
                    yield ": keepalive\n\n"
        finally:
            watch.close()
    return Response(stream_with_context(records()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
