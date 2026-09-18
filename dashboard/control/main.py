"""The controller as a process: `python -m control <command>`.

    init-pki                 the control plane's authority and its own two
                             certificates, made once and never overwritten
    enrol HOST KIND URL      a worker: its certificate issued, its record
                             pinned to it, its verbs permitted
    run                      the reconciler loop and the receiver for
                             heartbeats and events, until told to stop

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


def _address(text):
    host, _, port = text.rpartition(":")
    return host, int(port)


def unit_images(env):
    """What each cell's units are made from, from RUNNER_UNIT_IMAGE_<PROVIDER>
    _<PLATFORM> - an image for Linux, a template name for Windows and macOS."""
    import providers
    out = {}
    for provider in providers.ALL:
        for platform in providers.PLATFORMS:
            value = (env.get(f"RUNNER_UNIT_IMAGE_{provider.key.upper()}_"
                             f"{platform.upper()}") or "").strip()
            if value:
                out[(provider.key, platform)] = value
    return out


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
        self.service = RunnerService(self.db, runtimes=agent_runtime.TABLE)
        self.service.agents = agent_runtime.AgentWiring(
            client, operations=self.service.operations,
            images=unit_images(env))
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
    args = parser.parse_args(argv)

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
