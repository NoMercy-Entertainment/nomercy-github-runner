"""The controller as a process: `python -m control <command>`.

    init-pki                 the control plane's authority and its own two
                             certificates, made once and never overwritten
    enrol HOST KIND URL      a worker: its certificate issued, its record
                             pinned to it, its verbs permitted
    run                      the reconciler loop and the receiver for
                             heartbeats and events, until told to stop
    status                   workers, fleets and runners, as the store has
                             them - read-only
    capacity FLEET N         set a fleet's desired capacity; the running
                             controller does the rest, never aborting a job

Everything a pass does is the reconciler's (control/reconciler.py); this file
only builds the parts and keeps them running. The parts are the ones the tests
drive, put together: `AgentClient` over mutual TLS, `AgentRuntime` for every
cell, `FlowAgent` and `LiveForges` under the provisioning flow, and the
receiver writing heartbeats into the inventory and events into operations.

**It changes nothing on its own.** Fleets are seeded at capacity 0, and a
runner this controller did not plan is not in its store: the WSL fleet the
dashboard already shows is unmanaged here and stays untouched. The first thing
the controller builds is the first runner an operator asks it for.

**The authority's key never leaves this machine by this code.** `enrol`
writes a worker's own key and certificate into a bundle for the operator to
carry to the worker; the authority's key stays where `init-pki` put it,
readable by its owner only.
"""
import argparse
import os
import signal
import sys
import threading
import time

TLS_DIR = os.environ.get("CONTROL_TLS_DIR",
                         os.path.join(os.environ.get("DASH_DATA", "/data"),
                                      "control-tls"))
#: Where agents send heartbeats and events. The control plane's management
#: address, never every address (13.2).
RECEIVER = os.environ.get("CONTROL_RECEIVER", "127.0.0.1:8444")
INTERVAL = float(os.environ.get("CONTROL_INTERVAL", "15"))

FILES = {"ca": "ca.pem", "ca_key": "ca.key",
         "controller": "controller.crt", "controller_key": "controller.key",
         "receiver": "receiver.crt", "receiver_key": "receiver.key"}


def _path(name, tls_dir=None):
    return os.path.join(tls_dir or TLS_DIR, FILES[name])


def _write(path, data, private=False):
    """Written once: an existing file is never replaced, because a replaced
    authority would orphan every worker enrolled under the old one."""
    if os.path.exists(path):
        raise FileExistsError(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def init_pki(tls_dir=None):
    """The authority, the controller's calling certificate and its receiving
    one. Idempotent: what exists is kept, and only what is missing is made -
    but a missing authority with certificates present is refused, since new
    certificates from a new authority would match nothing already enrolled."""
    from . import ca
    tls_dir = tls_dir or TLS_DIR
    os.makedirs(tls_dir, mode=0o700, exist_ok=True)
    have_ca = os.path.exists(_path("ca", tls_dir))
    if not have_ca:
        if any(os.path.exists(_path(n, tls_dir))
               for n in ("controller", "receiver")):
            raise RuntimeError("certificates exist without their authority; "
                               "refusing to make a second one")
        cert, key = ca.create_ca()
        _write(_path("ca_key", tls_dir), key, private=True)
        _write(_path("ca", tls_dir), cert)
    authority = (_read(_path("ca", tls_dir)), _read(_path("ca_key", tls_dir)))
    made = []
    for role in ("controller", "receiver"):
        if os.path.exists(_path(role, tls_dir)):
            continue
        cert, key = ca.issue(*authority, ca.CONTROLLER_SUBJECT, role)
        _write(_path(f"{role}_key", tls_dir), key, private=True)
        _write(_path(role, tls_dir), cert)
        made.append(role)
    return {"authority": "made" if not have_ca else "kept", "made": made}


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def enrol(host_id, kind, endpoint, db=None, tls_dir=None, out_dir=None):
    """Issue a worker's certificate, pin its record to it, permit it every
    verb. Returns the bundle directory - the worker's key, certificate and
    the authority's certificate - for the operator to carry to the worker."""
    from . import ca
    from .agent_client import VERB_NAMES
    from .inventory import Inventory
    tls_dir = tls_dir or TLS_DIR
    if not endpoint.startswith("https://"):
        raise ValueError("a worker is reached over https only")
    authority = (_read(_path("ca", tls_dir)), _read(_path("ca_key", tls_dir)))
    _store(db)
    bundle = out_dir or os.path.join(tls_dir, "workers", host_id)
    os.makedirs(bundle, mode=0o700, exist_ok=True)
    cert, key = ca.issue(*authority, host_id, "agent")
    for name, data, private in (("agent.key", key, True),
                                ("agent.crt", cert, False),
                                ("ca.pem", authority[0], False)):
        path = os.path.join(bundle, name)
        if os.path.exists(path):
            os.unlink(path)             # a re-enrolment replaces the bundle
        _write(path, data, private=private)
    inventory = Inventory(db)
    inventory.register_worker(host_id, kind, endpoint=endpoint,
                              certificate_fingerprint=ca.fingerprint(cert))
    inventory.permit(host_id, VERB_NAMES)
    return bundle


def _store(db=None):
    """The store, made if it is not there yet. Idempotent. Every command
    that reads or writes it calls this first: one run before the controller
    ever has - an enrolment while it starts, say - would otherwise fail on a
    table that does not exist yet."""
    from store import schema
    from store.fleets import FleetStore
    path = db or schema.DB_PATH
    schema.init(path)
    FleetStore(path).seed(_env())
    return path


def _address(text):
    host, _, port = text.rpartition(":")
    return host, int(port)


def _per_cell(env, prefix):
    import providers
    out = {}
    for provider in providers.ALL:
        for platform in providers.PLATFORMS:
            value = (env.get(f"{prefix}_{provider.key.upper()}_"
                             f"{platform.upper()}") or "").strip()
            if value:
                out[(provider.key, platform)] = value
    return out


def unit_images(env):
    """What each cell's units are made from, from RUNNER_UNIT_IMAGE_<PROVIDER>
    _<PLATFORM> - an image for Linux, a template name for Windows and macOS."""
    return _per_cell(env, "RUNNER_UNIT_IMAGE")


def unit_memory(env):
    """Each cell's default unit memory limit, from RUNNER_UNIT_MEMORY_
    <PROVIDER>_<PLATFORM>, in the engine's own syntax ("6g")."""
    return _per_cell(env, "RUNNER_UNIT_MEMORY")


class Controller:
    """The parts of a running controller, built and stopped together."""

    def __init__(self, env, db=None, tls_dir=None, receiver=RECEIVER,
                 interval=INTERVAL):
        import socket

        from store import schema
        from store.fleets import FleetStore

        from . import agent_runtime, receiver as rcv
        from .agent_client import AgentClient
        from .forges import LiveForges
        from .inventory import Inventory
        from .provision import ProvisioningFlow
        from .reconciler import Reconciler
        from .service import RunnerService

        tls_dir = tls_dir or TLS_DIR
        self.db = db or schema.DB_PATH
        self.interval = interval
        schema.init(self.db)
        FleetStore(self.db).seed(env)
        inventory = Inventory(self.db)
        client = AgentClient(inventory, _path("controller", tls_dir),
                             _path("controller_key", tls_dir),
                             _path("ca", tls_dir), audit_path=self.db)
        self.service = RunnerService(self.db, runtimes=agent_runtime.TABLE,
                                     env=env)
        self.service.agents = agent_runtime.AgentWiring(
            client, operations=self.service.operations,
            images=unit_images(env), memory=unit_memory(env))
        self.flow = ProvisioningFlow(self.service,
                                     agent_runtime.FlowAgent(
                                         self.service.agents),
                                     LiveForges(env), env=env)
        self.reconciler = Reconciler(
            self.service, self.flow,
            holder=f"controller-{socket.gethostname()}-{os.getpid()}")
        self.receiver = rcv.Receiver(
            inventory,
            rcv.server_context(_path("receiver", tls_dir),
                               _path("receiver_key", tls_dir),
                               _path("ca", tls_dir)),
            address=_address(receiver),
            on_event=self.service.operations.apply_event,
            audit_path=self.db)
        self._stop = threading.Event()

    def pass_once(self):
        return self.reconciler.pass_once()

    def run(self, log=print):
        self.receiver.start()
        host, port = self.receiver.server_address[:2]
        log(f"controller: receiving on {host}:{port}, a pass every "
            f"{self.interval:g}s")
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    report = self.pass_once()
                except Exception as e:          # noqa: BLE001
                    log(f"controller: pass failed: {type(e).__name__}: {e}")
                else:
                    if report.actions or report.errors:
                        log(f"controller: did {report.actions}; errors "
                            f"{report.errors}; held {report.held}")
                self._stop.wait(max(0.0, self.interval
                                    - (time.monotonic() - started)))
        finally:
            self.receiver.stop()
            log("controller: stopped")

    def stop(self):
        self._stop.set()


class Refused(Exception):
    """An adoption that must not happen, and why. Raised before anything is
    written, so a refusal leaves no half-adopted runner behind."""


def adopt(fleet, name, host_id, label, root=None, template=None, db=None,
          env=None, forge=None, requested_by="cli"):
    """Take over a runner that is already serving, without making it again.

    MIG-4: the macOS runner has been taking jobs from its appliance for
    months, with its own launchd job and its own registration. A managed
    runner is not a new runner, so the record the forge already holds is
    read here and written into the spec: nothing is minted, and no record is
    stranded. The spec also carries what the runner already is on its
    worker - the launchd job and the directory it runs from - and the
    reconciler provisions it from there, which for a unit that exists means
    adopting it rather than building one.

    Idempotent: adopting the same runner twice returns the first spec.
    """
    import providers
    from . import retry
    from .inventory import Inventory
    from .service import RunnerService

    db = _store(db)
    env = _env() if env is None else env
    service = RunnerService(db, env=env)

    cell = service.fleets.get(fleet)
    if cell is None:
        raise Refused(f"no fleet {fleet}")
    if Inventory(db).get(host_id) is None:
        raise Refused(f"no worker {host_id}: enrol it before adopting onto "
                      f"it")

    provider = forge or providers.by_key(cell["provider"])
    if provider is None:
        raise Refused(f"{fleet} names an unknown provider "
                      f"{cell['provider']!r}")

    def ask():
        return provider.forge_records(env)

    # The same deadline the reconciler reads the forge under.
    records = retry.call(retry.FORGE_STATUS, ask)
    if records is None:
        raise Refused("the forge could not be asked which runners it has; "
                      "adopting blind would strand the record it holds")
    record = next((r for r in records
                   if str(r.get("name") or "") == str(name)), None)
    if record is None:
        raise Refused(f"no runner named {name} at this forge; it is the "
                      f"forge's own record that is being adopted")

    return service.adopt(
        fleet, name, host_id,
        registration={"id": str(record.get("id") or "") or None,
                      "uuid": record.get("uuid")},
        unit={"label": label, "root": root, "template": template},
        requested_by=requested_by)


def status(db=None):
    """Workers, fleets and runners as lines of text. Reads the store only."""
    from .inventory import Inventory
    from .service import RunnerService
    db = _store(db)
    service = RunnerService(db)
    lines = ["workers:"]
    for w in Inventory(db).summary():
        lines.append(f"  {w['host_id']:<16} {w['kind']:<15} {w['health']:<9}"
                     f" seen {w['last_seen_at'] or 'never'}"
                     + (f" - {w['reason']}" if w.get("reason") else ""))
    lines.append("fleets:")
    for f in service.fleets.list():
        if f["desired_capacity"] or not f["available"]:
            lines.append(f"  {f['fleet_id']:<22} capacity "
                         f"{f['desired_capacity']}"
                         + ("" if f["available"] else
                            f" - unavailable: {f['unavailable_reason']}"))
    lines.append("runners:")
    for r in service.specs.list():
        lines.append(f"  {r['runner_id']} {r['fleet_id']:<22} "
                     f"{r['actual_state']:<13} want {r['desired_state']:<8}"
                     f" on {r['host_id'] or '-'}"
                     + (f" - {r['last_error']}" if r.get("last_error") else
                        ""))
    return "\n".join(lines)


def capacity(fleet, count, db=None, who="cli"):
    from .service import RunnerService
    return RunnerService(_store(db)).set_capacity(fleet, int(count),
                                                  requested_by=who)


def _env():
    """The deployment's settings: the process environment, which compose
    fills from the same `.env` the dashboard reads."""
    return dict(os.environ)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m control")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-pki")
    e = sub.add_parser("enrol")
    e.add_argument("host_id")
    e.add_argument("kind", choices=("hyperv-linux", "hyperv-windows"))
    e.add_argument("endpoint", help="https://address:port of its agent")
    sub.add_parser("run")
    sub.add_parser("status")
    c = sub.add_parser("capacity")
    c.add_argument("fleet", help="e.g. forgejo-linux-x64")
    c.add_argument("count", type=int)
    a = sub.add_parser("adopt", help="take over a runner that already serves")
    a.add_argument("fleet", help="e.g. forgejo-macos-x64")
    a.add_argument("name", help="the name the forge knows it by")
    a.add_argument("host_id", help="the worker whose agent can reach it")
    a.add_argument("label", help="what the unit is called on that worker")
    a.add_argument("--root", help="the directory it runs from")
    a.add_argument("--template", help="what it was built from, for the "
                                      "record")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(status())
        return 0
    if args.command == "capacity":
        print(f"operation {capacity(args.fleet, args.count)}")
        return 0
    if args.command == "adopt":
        try:
            runner_id = adopt(args.fleet, args.name, args.host_id, args.label,
                              root=args.root, template=args.template)
        except Refused as e:
            print(f"refused: {e}")
            return 2
        print(f"adopted {args.name} as {runner_id}; the next pass provisions "
              f"it from what is already there")
        return 0

    if args.command == "init-pki":
        print(init_pki())
        return 0
    if args.command == "enrol":
        print(f"bundle for {args.host_id}: "
              f"{enrol(args.host_id, args.kind, args.endpoint)}")
        return 0

    from . import redact
    env = _env()
    redact.install_log_redaction(redact.secret_values(env))
    controller = Controller(env)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: controller.stop())
    controller.run(log=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
