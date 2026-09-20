"""T-1406: one runner's page, keyed on its runner_id and read from the
controller.

It shows what the controller knows - the card, every capability the runner
declares, the operation in flight and the recent ones, the last error and the
audit tail - and nothing a container engine would have to be asked for. The
v1 page at /runner/<name> stays for today's containers until T-1407.
"""
import os
import re
import shutil
import subprocess
import threading

import pytest

import api_v2
from control import audit
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(HERE, "templates", "runner_v2.html")


@pytest.fixture
def one_runner(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed({"FORGEJO_RUNNER_ARTIFACT_MACOS": "darwin"})
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    import control.service as service_module
    monkeypatch.setattr(service_module, "RUNTIMES", {
        ("forgejo", "macos"): "tests.fake_runtime:UnitRuntime"})
    service = RunnerService(path)
    rid = service.planned_ids(service.plan("forgejo-macos-x64", 1,
                                           env={"FORGEJO_RUNNER_ARTIFACT_MACOS": "darwin"}))[0]
    spec = service.specs.get(rid)
    service.specs.update(rid, spec["spec_version"], actual_state="idle",
                         last_error="registered with other labels",
                         capabilities={"kind": "macos-appliance",
                                       "job_containers": False,
                                       "supports_drain": False,
                                       "clear_cache": True,
                                       "a_capability_added_later": True})
    audit.record(path, "remove", "refused", actor="someone@example",
                 runner_id=rid, parameters={"token": "secret-value-123"})
    return service, rid, path


class TestThePage:
    def test_it_is_keyed_on_a_runner_id(self, client):
        rid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        assert client.get(f"/runners/{rid}").status_code == 200
        assert client.get("/runners/github-runner-3").status_code == 404

    def test_it_names_nothing_an_engine_would_answer(self):
        with open(PAGE, encoding="utf-8") as fh:
            src = fh.read().lower()
        for word in ("docker", "container", "inspect", "cpuset", "volume",
                     "/runner/"):
            assert word not in src, word

    def test_it_reads_from_the_controller_only(self):
        with open(PAGE, encoding="utf-8") as fh:
            src = fh.read()
        fetched = set(re.findall(r"fetch\('([^'+]+)", src))
        assert fetched == {"/api/v2/runners/"}


class TestItsData:
    def test_card_capabilities_error_and_audit(self, client, one_runner):
        service, rid, _ = one_runner
        body = client.get(f"/api/v2/runners/{rid}").get_json()
        assert body["card"]["capabilities"]["a_capability_added_later"]
        assert body["card"]["last_error"] == "registered with other labels"
        assert "no job containers" in body["card"]["annotations"]
        assert body["audit"][0]["verb"] == "remove"
        assert body["audit"][0]["decision"] == "refused"

    def test_no_secret_leaves_in_the_audit_tail(self, client, one_runner):
        service, rid, _ = one_runner
        body = client.get(f"/api/v2/runners/{rid}").get_data(as_text=True)
        assert "secret-value-123" not in body

    def test_the_operation_in_flight_is_shown(self, client, one_runner):
        service, rid, _ = one_runner
        op = service.act(rid, "stop", idempotency_key="k")
        body = client.get(f"/api/v2/runners/{rid}").get_json()
        assert body["card"]["current_operation"] == op
        assert body["operations"][0]["operation_id"] == op


def test_the_page_renders_it_in_a_real_browser(tmp_path, one_runner,
                                               monkeypatch):
    from flask import Flask, render_template
    from werkzeug.serving import make_server

    import browser
    if not browser.works(tmp_path):
        pytest.skip(browser.reason())

    service, rid, _ = one_runner
    stub = Flask("stub", template_folder=os.path.join(HERE, "templates"))
    # init() sets the module's status source; restored after the test.
    monkeypatch.setitem(api_v2._status, "fn", api_v2._status["fn"])
    api_v2.init(stub, lambda: {})
    stub.add_url_rule("/runners/<runner_id>", "page",
                      lambda runner_id: render_template(
                          "runner_v2.html", runner_id=runner_id,
                          role="admin"))
    server = make_server("127.0.0.1", 0, stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dom = browser.dump_dom(
            f"http://127.0.0.1:{server.server_port}/runners/{rid}",
            tmp_path / "edge")
    finally:
        server.shutdown()
    assert "a_capability_added_later" in dom, dom[-1500:]
    assert "registered with other labels" in dom
    assert "no job containers" in dom
    assert "refused" in dom and "someone@example" in dom
