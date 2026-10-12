"""Real two-dashboard HTTPS/SSE proof with a wire-contract courier fixture.

This exercises local allocation/registration/retirement policy and native timers
through actual server processes. GitHub observations and worker execution are
simulated: it is not a live lending or production NoMercy deployment test.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from tests.federation_http_harness import (
    GROUP, ISSUER, ORG, PROFILE, RECEIVER, SOURCE, TestCourier, Transport, certificates,
)


class Dashboard:
    def __init__(self, directory, origin, actor, peer_actor, access, courier, certificate, private):
        directory.mkdir()
        self.origin = origin
        config = directory / "config.json"
        config.write_text(json.dumps({"origin": origin, "actor": actor, "peer_actor": peer_actor,
            "access": access, "broker_port": courier.server.server_port, "certificate": certificate, "private": private}))
        # Do not inherit host GH/PAT/SSO/agent/runtime configuration into tests.
        # setup-python's shared libpython needs its own loader path; omitting it
        # can resolve the system interpreter prefix and lose installed packages.
        environment = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SystemDrive", "TEMP", "TMP", "WINDIR", "LANG", "LD_LIBRARY_PATH") if key in os.environ}
        environment.update(DASH_DATA=str(directory), ENV_PATH=str(directory / ".env"),
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONUNBUFFERED="1")
        (directory / ".env").write_text("")
        self.log_path = directory / "server.log"
        self.log = self.log_path.open("w")
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("federation_http_harness.py")), str(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, env=environment, cwd=directory)
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self.read_lines, daemon=True)
        self.reader.start()
        try:
            ready = self.read()
            assert Path(ready["python_prefix"]).resolve() == Path(sys.prefix).resolve(), "Child used a different interpreter installation"
        except BaseException:
            self.close()
            raise
        courier.routes[origin.removeprefix("https://")] = ready["port"]
        self.transport = Transport(certificate, courier.routes)
        self.headers = {"Cookie": "session=" + ready["cookie"], "X-CSRF-Token": ready["csrf"]}

    def read_lines(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def read(self):
        try:
            line = self.lines.get(timeout=15)
        except queue.Empty:
            raise AssertionError("Dashboard IPC timed out: " + self.log_path.read_text()) from None
        assert line is not None, "Dashboard exited: " + self.log_path.read_text()
        return json.loads(line)

    def ipc(self, action, **values):
        self.process.stdin.write(json.dumps(dict(action=action, **values)) + "\n")
        self.process.stdin.flush()
        return self.read()

    def call(self, path="", body=None, status=200, headers=None):
        observed, data = self.transport.json(self.origin + "/api/v2/federation" + path,
            "POST" if body is not None else "GET", body, self.headers if headers is None else headers)
        assert observed == status, f"HTTP {observed}: {data}"
        return data

    def events(self):
        return Events(self)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write('{"action":"stop"}\n')
            self.process.stdin.flush()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        self.reader.join(1)
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()


class Events:
    """Wait for actual SSE push; never repeatedly request status endpoints."""
    def __init__(self, dashboard):
        self.connection, self.response = dashboard.transport.open(dashboard.origin + "/api/v2/federation/events",
                                                                  headers=dashboard.headers)
        assert self.response.status == 200

    def wait(self, action, detail=None, request_id=None):
        while True:
            line = self.response.readline().decode()
            assert line, "SSE stream ended before the expected event"
            if not line.startswith("data: "):
                continue
            row = json.loads(line[6:])
            if row["action"] == action and (detail is None or row["detail"] == detail) and (request_id is None or row["request_id"] == request_id):
                return row

    def close(self):
        self.connection.close()


@pytest.fixture
def two_dashboards(tmp_path):
    certificate, private = certificates(tmp_path)
    actors = [str(uuid.uuid4()), str(uuid.uuid4())]
    courier = TestCourier(certificate, private, {"test-source-access": actors[0], "test-receiver-access": actors[1]})
    dashboards, streams = [], []
    try:
        source = Dashboard(tmp_path / "source", SOURCE, actors[0], actors[1], "test-source-access", courier, certificate, private)
        dashboards.append(source)
        receiver = Dashboard(tmp_path / "receiver", RECEIVER, actors[1], actors[0], "test-receiver-access", courier, certificate, private)
        dashboards.append(receiver)
        source_events, receiver_events = source.events(), receiver.events()
        streams.extend((source_events, receiver_events))
        source_id = source.call("/broker/connect", {"issuer": ISSUER, "name": "Source"})["fleet_id"]
        receiver_id = receiver.call("/broker/connect", {"issuer": ISSUER, "name": "Receiver"})["fleet_id"]
        assert source_events.wait("broker-verified")["detail"] == source_id
        assert receiver_events.wait("broker-verified")["detail"] == receiver_id
        yield source, receiver, source_events, receiver_events, courier, actors, receiver_id
    finally:
        for stream in streams:
            stream.close()
        for dashboard in reversed(dashboards):
            dashboard.close()
        courier.close()


def permission(receiver, actor):
    limits = {"max_runners": 1, "max_lease_minutes": 90, "profiles": [PROFILE],
              "organizations": [ORG], "runner_groups": [GROUP]}
    peer = receiver.call("/peers", {"name": "Existing person", "subject": actor, "policy": limits, "transport": "broker"}, 201)
    assert peer["credential"] is None  # UI selects a known local person; no pasted secret.
    receiver.call("/peers/" + peer["id"] + "/connect", {})
    receiver.call("/lending", {"enabled": True, "templates": {PROFILE: "test:guarded"}})
    return peer["id"]


def requested(source, receiver, receiver_events, receiver_id):
    key = source.call("/broker/outgoing", {"target_fleet_id": receiver_id,
        "intent": {"profile": PROFILE, "organization": ORG, "count": 1, "lease_minutes": 60}, "runner_group": GROUP}, 202)["request_key"]
    arrival = receiver_events.wait("requested")
    snapshot = receiver.call()
    row = next(row for row in snapshot["requests"] if row["id"] == arrival["request_id"])
    assert row["state"] == "pending" and row["runner_ids"] == []
    assert receiver.ipc("inspect")["owned"] == []
    return key, row["id"]


def test_two_dashboards_https_owner_approval_registration_results_and_busy_revocation(two_dashboards):
    source, receiver, source_events, receiver_events, courier, actors, receiver_id = two_dashboards
    peer = permission(receiver, actors[0])
    key, request_id = requested(source, receiver, receiver_events, receiver_id)
    source_events.wait("peer-result", "pending", key)
    envelope = next(iter(courier.requests.values()))["envelope"]
    offered = courier.signed(actors[0], receiver_id, envelope, "capacity")
    tampered = json.loads(json.dumps(envelope))
    tampered["intent"]["count"] = 2
    receiver.call("/broker/requests", tampered, 403, {"Authorization": "Bearer " + offered})
    wrong_audience = courier.signed(actors[0], str(uuid.uuid4()), envelope, "capacity")
    receiver.call("/broker/requests", envelope, 403, {"Authorization": "Bearer " + wrong_audience})
    wrong_purpose = courier.signed(actors[0], receiver_id, envelope, "result")
    receiver.call("/broker/requests", envelope, 403, {"Authorization": "Bearer " + wrong_purpose})
    # Retrying the exact signed request is idempotent and creates no extra claim.
    receiver.call("/broker/requests", envelope, 202, {"Authorization": "Bearer " + offered})
    receiver.call("/broker/requests", envelope, 202, {"Authorization": "Bearer " + offered})
    assert len(receiver.call()["requests"]) == 1
    receiver.call("/requests/" + request_id + "/decision", {"approve": True})
    materialised = receiver.ipc("materialise")
    assert "error" not in materialised
    assert materialised["mint_count"] == 0
    source_events.wait("peer-result", "active", key)
    assert next(row for row in source.call()["outgoing"] if row["request_key"] == key)["state"] == "active"
    spec = materialised["owned"][0]
    assert spec["fleet_id"] is None and spec["runner_group"] == GROUP
    assert spec["memory_limit"] == 16 * 1024**3 and spec["disk_limit"] == 100 * 1024**3
    assert spec["cache_policy"]["max_bytes"] == 40 * 1024**3
    low, high = map(int, spec["cpu_limit"].split("-"))
    assert high - low + 1 == 8
    receiver.ipc("busy")
    receiver.call("/peers/" + peer + "/revoke", {})
    source_events.wait("peer-result", "retiring", key)
    refused = receiver.ipc("retire")
    assert refused["error"] == "Refused" and "running or unknown" in refused["message"]
    receiver.ipc("unknown")
    receiver.ipc("idle")
    assert "unknown" in receiver.ipc("retire")["message"]
    receiver.ipc("known")
    receiver.ipc("retire")
    source_events.wait("peer-result", "expired", key)
    final = receiver.ipc("settled")
    assert final["normal_unchanged"] and final["fleets_unchanged"] and final["worker_alive"]
    assert final["owned"][0]["actual_state"] == "absent"
    assert all(count == 0 for count in final["secrets"].values())
    assert source.ipc("inspect")["mint_count"] == 1
    # A broad account bearer cannot reach callback ingress, even with owner cookies.
    receiver.call("/broker/requests", {}, 401, dict(receiver.headers, Authorization="Bearer test-source-access"))
    record = next(iter(courier.requests.values()))
    envelope = record["envelope"]
    offered = courier.signed(actors[0], receiver_id, envelope, "capacity")
    receiver.call("/broker/requests", envelope, 403, {"Authorization": "Bearer " + offered})
    assert receiver.ipc("inspect")["normal_unchanged"]
    assert all(assertion.startswith("nmc_") for _, assertion, _ in courier.deliveries)


def test_two_dashboards_exact_native_lease_expiry_pushes_terminal_result(two_dashboards):
    source, receiver, source_events, receiver_events, courier, actors, receiver_id = two_dashboards
    permission(receiver, actors[0])
    key, request_id = requested(source, receiver, receiver_events, receiver_id)
    receiver.call("/requests/" + request_id + "/decision", {"approve": True})
    assert "error" not in receiver.ipc("materialise")
    source_events.wait("peer-result", "active", key)
    receiver.ipc("expire", request=request_id)
    started = time.monotonic()
    source_events.wait("peer-result", "retiring", key)
    assert time.monotonic() - started < 6
    receiver.ipc("retire")
    source_events.wait("peer-result", "expired", key)
    final = receiver.ipc("settled")
    assert final["normal_unchanged"] and final["fleets_unchanged"]
    assert all(count == 0 for count in final["secrets"].values())
