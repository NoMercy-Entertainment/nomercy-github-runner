"""The controller as a process, against a real agent over real mutual TLS.

Everything between the reconciler and a worker is the real thing here: the
authority `init-pki` makes, a worker `enrol` pins, the agent's own entry point
(`agent.__main__.Running`) serving and heartbeating, the receiver taking the
beats, `AgentClient` calling the verbs, and `AgentRuntime` and `FlowAgent`
translating the flow into them. Only the two ends are stand-ins: the worker's
units live in a dict instead of an engine, and GitHub is a class that mints
tokens and lists what was registered. A runner is taken from nothing to
serving, drained, and removed, and every step crosses the wire.
"""
import os
import socket
import sys
import time

import pytest

import providers as P
from control import agent_runtime, main
from control.inventory import Inventory
from store import schema
from store.fleets import fleet_id

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import __main__ as agent_main          # noqa: E402
from agent.config import Config                    # noqa: E402

GH = fleet_id("github", "linux", "x64")
HOST = "linux-worker-1"
TOKEN = "tok-sentinel-5f1e0c9a7b3d"
ENV = {"GH_TOKEN": "gh-deployment-token-0000", "GITHUB_ORG": "NoMercy",
       "RUNNER_UNIT_IMAGE_GITHUB_LINUX": "ghcr.io/nomercy/runner-unit:1",
       "RUNNER_LABELS": "self-hosted,Linux,X64,beast-unit"}


class WorkerUnits:
    """The worker's side: units in a dict, as the agent's runtime sees them."""

    def __init__(self):
        self.units = {}
        self.calls = []

    def create(self, runner_id, spec):
        self.calls.append(("create", runner_id, dict(spec)))
        self.units[runner_id] = {"running": True, "spec": dict(spec)}
        return f"rnr-{runner_id}"

    def start(self, runner_id):
        self.calls.append(("start", runner_id))
        self.units[runner_id]["running"] = True

    def stop(self, runner_id):
        self.calls.append(("stop", runner_id))
        self.units[runner_id]["running"] = False

    def restart(self, runner_id):
        self.calls.append(("restart", runner_id))

    def remove(self, runner_id, keep_data):
        self.calls.append(("remove", runner_id, keep_data))
        self.units.pop(runner_id, None)

    def status(self, runner_id):
        unit = self.units.get(runner_id)
        if unit is None:
            return {"exists": False, "running": False, "state": "absent"}
        return {"exists": True, "running": unit["running"],
                "state": "running" if unit["running"] else "exited"}

    def telemetry(self, runner_id):
        return {"cpu_percent": 2.0}

    def logs(self, runner_id, since_seconds):
        return "listening for jobs"

    def probe(self, runner_id, probe):
        return {"ok": True, "value": 1}

    def clear_cache(self, runner_id, policy):
        return {"total_bytes": 0, "measured": True}

    def drain(self, runner_id):
        self.calls.append(("drain", runner_id))

    def cancel_drain(self, runner_id):
        self.calls.append(("cancel_drain", runner_id))

    def capabilities(self):
        return {"kind": "linux-container", "supports_drain": True,
                "job_containers": True}

    def instances(self):
        return [{"runner_id": rid,
                 "state": "running" if u["running"] else "stopped"}
                for rid, u in self.units.items()]


class WorkerRegistrar:
    def __init__(self, github):
        self.github = github
        self.plans = []

    def register(self, runner_id, plan):
        self.plans.append(dict(plan))
        rid = self.github.add(plan)
        self.github.by_unit[runner_id] = rid
        return {"registration_id": rid, "registration_uuid": None}

    def deregister(self, runner_id):
        self.github.runners.pop(self.github.by_unit.pop(runner_id, None),
                                None)


class GitHub:
    """What the controller asks of GitHub, for one org."""

    org = "NoMercy"

    def __init__(self):
        self.runners = {}
        self.by_unit = {}
        self.next_id = 100

    def add(self, plan):
        self.next_id += 1
        rid = str(self.next_id)
        self.runners[rid] = {
            "id": int(rid), "name": plan["name"], "status": "online",
            "busy": False, "os": "linux",
            "labels": [(x, "read-only" if x in ("self-hosted", "Linux", "X64")
                        else "custom") for x in plan["labels"].split(",")],
            "runner_group": "Default"}
        return rid

    def registration_token(self):
        return TOKEN

    def runners_list(self):
        return [dict(r, labels=[n for n, _ in r["labels"]])
                for r in self.runners.values()]

    def runner_labels(self, rid):
        return list(self.runners[str(rid)]["labels"])

    def remove_custom_labels(self, rid):
        r = self.runners[str(rid)]
        r["labels"] = [x for x in r["labels"] if x[1] == "read-only"]
        return list(r["labels"])

    def set_custom_labels(self, rid, labels):
        r = self.runners[str(rid)]
        r["labels"] = [x for x in r["labels"] if x[1] == "read-only"] + [
            (n, "custom") for n in labels]
        return list(r["labels"])

    def delete_runner(self, rid):
        self.runners.pop(str(rid), None)
        return True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def plant(tmp_path, monkeypatch):
    github = GitHub()

    class Client:
        org = github.org
        registration_token = staticmethod(github.registration_token)
        runners = staticmethod(github.runners_list)
        runner_labels = staticmethod(github.runner_labels)
        remove_custom_labels = staticmethod(github.remove_custom_labels)
        set_custom_labels = staticmethod(github.set_custom_labels)
        delete_runner = staticmethod(github.delete_runner)

    monkeypatch.setattr(P.GITHUB, "forge_client", lambda env: Client())
    tls_dir, db = str(tmp_path / "tls"), str(tmp_path / "control.db")
    main.init_pki(tls_dir)
    schema.init(db)

    agent_port = free_port()
    bundle = main.enrol(HOST, "hyperv-linux",
                        f"https://127.0.0.1:{agent_port}", db=db,
                        tls_dir=tls_dir)
    controller = main.Controller(dict(ENV), db=db, tls_dir=tls_dir,
                                 receiver="127.0.0.1:0", interval=0.1)
    controller.receiver.start()
    units, registrar = WorkerUnits(), WorkerRegistrar(github)
    receiver_port = controller.receiver.server_address[1]
    agent = agent_main.Running(Config(
        host_id=HOST, runtime="linux-container",
        listen=("127.0.0.1", agent_port),
        controller=f"https://127.0.0.1:{receiver_port}",
        cert=os.path.join(bundle, "agent.crt"),
        key=os.path.join(bundle, "agent.key"),
        ca=os.path.join(bundle, "ca.pem"),
        capacity={"max_runners": 2}),
        runtime=units, registrar=registrar).start()
    try:
        until(lambda: Inventory(db).health(HOST) == "healthy",
              "the worker's first heartbeat")
        yield controller, units, registrar, github, db
    finally:
        agent.stop()
        controller.receiver.stop()


def until(check, what, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def converge(controller, runner=None, state=None, passes=12):
    for _ in range(passes):
        controller.pass_once()
        if runner and controller.service.specs.get(runner)[
                "actual_state"] == state:
            return
    if runner:
        got = controller.service.specs.get(runner)
        raise AssertionError(f"{runner} is {got['actual_state']}, not "
                             f"{state}: {got['last_error']}")


def the_runner(controller):
    specs = controller.service.specs.list(fleet_id=GH)
    assert len(specs) == 1
    return specs[0]


class TestFromNothingToServing:
    def test_a_runner_is_built_on_the_worker_and_registered(self, plant):
        controller, units, registrar, github, db = plant
        controller.service.scale_up(GH)
        controller.pass_once()
        runner = the_runner(controller)["runner_id"]
        converge(controller, runner, "idle")

        spec = controller.service.specs.get(runner)
        assert spec["host_id"] == HOST
        assert spec["exec_unit_ref"] == f"rnr-{runner}"
        created = [c for c in units.calls if c[0] == "create"]
        assert created and created[0][1] == runner
        assert created[0][2]["image"] == "ghcr.io/nomercy/runner-unit:1"
        assert registrar.plans[0]["token"] == TOKEN
        assert spec["registration_id"] in github.runners

    def test_the_token_went_to_the_worker_and_nowhere_else(self, plant):
        controller, units, registrar, github, db = plant
        controller.service.scale_up(GH)
        for _ in range(12):
            controller.pass_once()
        with open(db, "rb") as fh:
            assert TOKEN.encode() not in fh.read()
        assert TOKEN not in repr(units.calls)

    def test_heartbeats_carry_the_unit_back(self, plant):
        controller, units, registrar, github, db = plant
        controller.service.scale_up(GH)
        controller.pass_once()
        runner = the_runner(controller)["runner_id"]
        converge(controller, runner, "idle")
        until(lambda: controller.service.specs.get(runner)["unit_state"]
              == "running", "a heartbeat naming the unit", timeout=15)

    def test_the_worker_declares_what_it_can_hold(self, plant):
        controller, units, registrar, github, db = plant
        worker = Inventory(db).get(HOST)
        assert '"max_runners": 2' in (worker.get("capabilities") or "") \
            or (worker.get("capabilities") or {}).get("max_runners") == 2


class TestDrainedAndGone:
    def test_drained_at_github_then_removed_from_the_worker(self, plant):
        controller, units, registrar, github, db = plant
        controller.service.scale_up(GH)
        controller.pass_once()
        runner = the_runner(controller)["runner_id"]
        converge(controller, runner, "idle")
        rid = controller.service.specs.get(runner)["registration_id"]

        controller.service.drain(runner)
        converge(controller, runner, "drained")
        assert [n for n, kind in github.runners[rid]["labels"]
                if kind == "custom"] == []
        assert not [c for c in units.calls if c[0] == "drain"], \
            "a GitHub runner is never signalled on its worker"

        controller.service.scale_down(GH)
        converge(controller, runner, "absent", passes=20)
        assert rid not in github.runners
        assert runner not in units.units


@pytest.mark.parametrize("env,expected", [
    ({}, 600), ({"CONTROL_VERIFY_TIMEOUT": "30"}, 30),
    ({"CONTROL_VERIFY_TIMEOUT": ""}, 600),
    ({"CONTROL_VERIFY_TIMEOUT": "not a number"}, 600),
    ({"CONTROL_VERIFY_TIMEOUT": "0"}, 600)])
def test_how_long_a_new_runner_is_given_to_come_up(env, expected):
    """A fresh unit starts a nested engine before its runner answers, which
    on a busy worker takes minutes. A verification that gives up first
    undoes a registration that had just succeeded (2026-09-20)."""
    assert main.verify_timeout(env) == expected
