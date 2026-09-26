"""T-1402 and T-1301: one action route for every runner, and the eighteen verbs
of design 12.2 each on a route.

`POST /api/v2/runners/<runner_id>/actions/<verb>` is the one door for every
runner of every platform. What a runner cannot do is refused with a reason -
never a 500 - and every mutation carries an idempotency key, so a caller that
did not hear the answer can ask again safely. The route table is checked
against the verb list the design itself gives in 12.2.
"""
import os
import re

import pytest

import api_v2
import providers as P
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore, fleet_id

SPEC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "docs", "superpowers", "specs",
    "2026-09-17-uniform-hyperv-runner-platform-design.md")

BUILT = {"FORGEJO_RUNNER_ARTIFACT_WINDOWS": "forgejo-runner.exe",
         "FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin-amd64"}
ALL_CELLS = {(p.key, platform): "tests.fake_runtime:UnitRuntime"
             for p in P.ALL for platform in P.PLATFORMS}


@pytest.fixture
def plane(tmp_path, monkeypatch):
    """A control plane with one runner in each of the six cells, all idle."""
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed(BUILT)
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    import control.service as service_module
    monkeypatch.setattr(service_module, "RUNTIMES", dict(ALL_CELLS))
    service = RunnerService(path)
    runners = {}
    for p in P.ALL:
        for platform in P.PLATFORMS:
            fid = fleet_id(p.key, platform, P.X64)
            rid = service.planned_ids(service.plan(fid, 1, env=BUILT))[0]
            spec = service.specs.get(rid)
            service.specs.update(rid, spec["spec_version"],
                                 actual_state="idle",
                                 capabilities={"supports_drain": True,
                                               "clear_cache": True})
            runners[(p.key, platform)] = rid
    return service, runners


def post(client, url, key="k-1", body=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(url, json=body or {}, headers=headers)


class TestOneDoor:
    def test_every_verb_of_every_platform_reaches_the_same_handler(self):
        import app as dash
        adapter = dash.app.url_map.bind("localhost")
        rid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        endpoints = {adapter.match(f"/api/v2/runners/{rid}/actions/{verb}",
                                   method="POST")[0]
                     for verb in api_v2.RUNNER_ACTIONS}
        assert endpoints == {"api_v2.runner_action"}

    @pytest.mark.parametrize("cell", [(p.key, pl) for p in P.ALL
                                      for pl in P.PLATFORMS])
    def test_a_stop_is_accepted_in_every_cell(self, client, plane, cell):
        service, runners = plane
        rid = runners[cell]
        r = post(client, f"/api/v2/runners/{rid}/actions/stop")
        assert r.status_code == 202, r.get_json()
        op = service.operations.get(r.get_json()["operation_id"])
        assert (op["verb"], op["runner_id"]) == ("stop", rid)

    def test_a_repeat_with_the_same_key_is_the_same_operation(self, client,
                                                              plane):
        service, runners = plane
        rid = runners[("github", "linux")]
        first = post(client, f"/api/v2/runners/{rid}/actions/drain", "same")
        again = post(client, f"/api/v2/runners/{rid}/actions/drain", "same")
        assert first.get_json()["operation_id"] == \
            again.get_json()["operation_id"]


class TestRefusalsAreAnswers:
    """Never a 500: each says why."""

    def test_an_unknown_action(self, client, plane):
        rid = plane[1][("github", "linux")]
        r = post(client, f"/api/v2/runners/{rid}/actions/fly")
        assert r.status_code == 404 and "the actions are" in r.get_json()[
            "error"]

    def test_a_read_is_not_an_action(self, client, plane):
        rid = plane[1][("github", "linux")]
        r = post(client, f"/api/v2/runners/{rid}/actions/fetch_logs")
        assert r.status_code == 400
        assert "GET" in r.get_json()["error"]

    def test_removed_scale_verb_is_not_an_action(self, client, plane):
        rid = plane[1][("github", "linux")]
        r = post(client, f"/api/v2/runners/{rid}/actions/scale_up")
        assert r.status_code == 404
        assert "no action" in r.get_json()["error"]

    def test_no_key_no_mutation(self, client, plane):
        service, runners = plane
        rid = runners[("github", "linux")]
        r = post(client, f"/api/v2/runners/{rid}/actions/stop", key=None)
        assert r.status_code == 400
        assert "Idempotency-Key" in r.get_json()["error"]
        assert service.specs.get(rid)["current_operation"] is None

    def test_a_capability_the_runner_does_not_have(self, client, plane):
        service, runners = plane
        rid = runners[("forgejo", "macos")]
        spec = service.specs.get(rid)
        service.specs.update(rid, spec["spec_version"],
                             capabilities={"supports_drain": False})
        r = post(client, f"/api/v2/runners/{rid}/actions/drain")
        assert r.status_code == 409
        assert "cannot be drained" in r.get_json()["error"]

    def test_a_state_the_verb_cannot_start_from(self, client, plane):
        rid = plane[1][("github", "linux")]
        r = post(client, f"/api/v2/runners/{rid}/actions/start")
        assert r.status_code == 409
        assert "starts from" in r.get_json()["error"]

    def test_an_unknown_runner(self, client, plane):
        r = post(client, "/api/v2/runners/"
                         "00000000-0000-4000-8000-000000000000/actions/stop")
        assert r.status_code == 404

    def test_no_control_plane(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: str(tmp_path / "none.db"))
        r = post(client, "/api/v2/runners/"
                         "00000000-0000-4000-8000-000000000000/actions/stop")
        assert r.status_code == 503
        assert "has not run" in r.get_json()["error"]
        assert not (tmp_path / "none.db").exists()

    def test_a_viewer_cannot_act(self, client, plane, monkeypatch):
        import users
        users.approve("sub-test-admin", "viewer")
        rid = plane[1][("github", "linux")]
        r = post(client, f"/api/v2/runners/{rid}/actions/stop")
        assert r.status_code == 403


class TestTheEighteenVerbs:
    """T-1301: the verb list in design 12.2 and the route table agree."""

    def design_verbs(self):
        with open(SPEC, encoding="utf-8") as fh:
            text = fh.read()
        para = re.search(r"Mapping to the eighteen verbs(.*?)edge\.", text,
                         re.S).group(1)
        names = {n.replace(" ", "_") for n in re.findall(r"`([a-z ]+)`",
                                                        para)}
        # "`repair`/`reconcile` is the failed -> provisioning edge": one verb
        # under two names. And the paragraph names the states `clear cache`
        # needs - states, not verbs.
        from control import states
        names.discard("reconcile")
        return names - states.STATES

    def test_the_design_lists_eighteen(self):
        assert len(self.design_verbs()) == 18

    def test_operator_routes_exclude_numeric_scaling(self):
        assert set(api_v2.ROUTES) == self.design_verbs() - {"scale_up", "scale_down"}

    def test_and_the_controller_knows_the_other_verbs(self):
        from control import states
        assert set(api_v2.ROUTES) == set(states.VERBS) - {"scale_up", "scale_down"}

    def test_reads_are_get_and_mutations_post(self):
        from control import states
        for verb, (method, _) in api_v2.ROUTES.items():
            assert method == ("GET" if verb in states.READS else "POST"), verb

    def test_every_route_is_served(self):
        import app as dash
        served = {(m, r.rule) for r in dash.app.url_map.iter_rules()
                  for m in r.methods}
        for verb, (method, path) in api_v2.ROUTES.items():
            rule = path.replace("<runner_id>", "<runner_id>")
            if path.endswith("/actions/" + verb):
                rule = path.rsplit("/", 1)[0] + "/<verb>"
            assert (method, rule) in served, (verb, method, rule)

    @pytest.mark.parametrize("verb", sorted(
        v for v, (m, _) in api_v2.ROUTES.items() if m == "POST"))
    def test_every_mutation_requires_a_key(self, client, plane, verb):
        service, runners = plane
        rid = runners[("github", "linux")]
        path = api_v2.ROUTES[verb][1].replace(
            "<runner_id>", rid).replace("<fleet_id>", "github-linux-x64")
        r = client.post(path, json={"desired": 1})
        assert r.status_code == 400
        assert "Idempotency-Key" in r.get_json()["error"]

    def test_the_reads_answer(self, client, plane):
        rid = plane[1][("github", "linux")]
        for read in ("status", "logs", "resources"):
            r = client.get(f"/api/v2/runners/{rid}/{read}")
            assert r.status_code == 200, (read, r.get_json())

    def test_a_runner_reads_as_its_card_and_redacted_spec(self, client,
                                                          plane):
        rid = plane[1][("forgejo", "windows")]
        body = client.get(f"/api/v2/runners/{rid}").get_json()
        assert body["card"]["runner_id"] == rid
        assert body["card"]["platform"] == "windows"
        assert body["spec"]["runner_id"] == rid
