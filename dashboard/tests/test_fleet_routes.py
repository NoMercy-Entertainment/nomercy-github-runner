"""T-1403 and T-1404: the fleets, operable, and rows rather than code.

Capacity, add, recreate and clear-cache per fleet, each an operation with an
idempotency key; scale up and scale down are the same capacity call. The
fleets themselves come from the table - six by seeding, and a seventh the
moment it is a row - and a fleet that cannot exist says why.
"""
import pytest

import api_v2
import providers as P
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore

BUILT = {"FORGEJO_RUNNER_ARTIFACT_WINDOWS": "forgejo-runner.exe",
         "FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin-amd64"}
GH = "github-linux-x64"


@pytest.fixture
def plane(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed(BUILT)
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    monkeypatch.setitem(api_v2._status, "fn", lambda: {})
    return RunnerService(path), path


def post(client, url, key="k-1", body=None):
    return client.post(url, json=body or {},
                       headers={"Idempotency-Key": key} if key else {})


def desired(service, fid=GH):
    return service.fleets.get(fid)["desired_capacity"]


class TestCapacity:
    def test_up_and_down_are_one_call(self, client, plane):
        service, _ = plane
        assert post(client, f"/api/v2/fleets/{GH}/capacity", "a",
                    {"desired": 3}).status_code == 202
        assert desired(service) == 3
        assert post(client, f"/api/v2/fleets/{GH}/capacity", "b",
                    {"desired": 1}).status_code == 202
        assert desired(service) == 1

    def test_a_repeat_does_not_undo_a_change_made_since(self, client, plane):
        service, _ = plane
        post(client, f"/api/v2/fleets/{GH}/capacity", "first", {"desired": 3})
        post(client, f"/api/v2/fleets/{GH}/capacity", "second",
             {"desired": 5})
        again = post(client, f"/api/v2/fleets/{GH}/capacity", "first",
                     {"desired": 3})
        assert again.status_code == 202
        assert desired(service) == 5

    @pytest.mark.parametrize("bad", [-1, "3", 1.5, True, None])
    def test_a_number_that_is_not_one_is_refused(self, client, plane, bad):
        r = post(client, f"/api/v2/fleets/{GH}/capacity", body={"desired": bad})
        assert r.status_code == 400

    def test_a_fleet_that_cannot_exist_is_refused_with_its_reason(
            self, client, tmp_path, monkeypatch):
        path = str(tmp_path / "bare.db")
        schema.init(path)
        FleetStore(path).seed({})          # no self-built artefacts
        monkeypatch.setattr(api_v2, "_db_path", lambda: path)
        r = post(client, "/api/v2/fleets/forgejo-windows-x64/capacity",
                 body={"desired": 1})
        assert r.status_code == 409
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in r.get_json()["error"]

    def test_an_unknown_fleet(self, client, plane):
        r = post(client, "/api/v2/fleets/nope/capacity", body={"desired": 1})
        assert r.status_code == 404


class TestAddARunner:
    def test_it_is_one_more_capacity(self, client, plane):
        service, _ = plane
        before = desired(service)
        assert post(client, f"/api/v2/fleets/{GH}/runners",
                    "add-1").status_code == 202
        assert desired(service) == before + 1

    def test_sent_twice_it_adds_one(self, client, plane):
        """The caller who did not hear the answer and asked again."""
        service, _ = plane
        before = desired(service)
        post(client, f"/api/v2/fleets/{GH}/runners", "add-once")
        post(client, f"/api/v2/fleets/{GH}/runners", "add-once")
        assert desired(service) == before + 1


def with_runners(service, states_):
    rids = service.planned_ids(service.plan(GH, len(states_), env=BUILT))
    for rid, state in zip(rids, states_):
        spec = service.specs.get(rid)
        service.specs.update(rid, spec["spec_version"], actual_state=state,
                             capabilities={"clear_cache": True})
    return rids


class TestRecreateAndClear:
    def test_recreate_asks_every_runner_and_says_what_each_answered(
            self, client, plane):
        service, _ = plane
        drained, idle = with_runners(service, ["drained", "idle"])
        body = post(client, f"/api/v2/fleets/{GH}/recreate", "r").get_json()
        by_id = {r["runner_id"]: r for r in body["results"]}
        assert by_id[drained]["ok"] is True
        assert by_id[idle]["ok"] is False and by_id[idle]["error"]

    def test_clear_cache_skips_a_busy_runner_with_the_reason(self, client,
                                                             plane):
        service, _ = plane
        idle, busy = with_runners(service, ["idle", "busy"])
        body = post(client, f"/api/v2/fleets/{GH}/clear-cache",
                    "c").get_json()
        by_id = {r["runner_id"]: r for r in body["results"]}
        assert by_id[idle]["ok"] is True
        assert "only idle or drained" in by_id[busy]["skipped"]
        assert service.specs.get(busy)["current_operation"] is None

    def test_repeating_either_repeats_nothing(self, client, plane):
        service, _ = plane
        (idle,) = with_runners(service, ["idle"])
        first = post(client, f"/api/v2/fleets/{GH}/clear-cache", "same")
        again = post(client, f"/api/v2/fleets/{GH}/clear-cache", "same")
        assert first.get_json()["results"][0]["operation_id"] == \
            again.get_json()["results"][0]["operation_id"]


class TestTheFleetsAreRows:
    def test_without_a_control_plane_the_six_come_from_the_providers(
            self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: str(tmp_path / "none.db"))
        monkeypatch.setitem(api_v2._status, "fn", lambda: {})
        fleets = client.get("/api/v2/fleets").get_json()["fleets"]
        assert len(fleets) == 6
        win = next(f for f in fleets if f["fleet_id"] == "forgejo-windows-x64")
        assert win["available"] is False
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in win["reason"]
        assert all(not a["enabled"] and a["reason"] for a in win["actions"])

    def test_a_seventh_fleet_needs_no_code_only_a_row(self, client, plane):
        service, path = plane
        with schema.connect(path) as c:
            c.execute("INSERT INTO fleets (fleet_id, provider, platform,"
                      " architecture, desired_capacity, labels, available)"
                      " VALUES ('github-linux-arm64', 'github', 'linux',"
                      " 'arm64', 0, '[]', 1)")
        fleets = client.get("/api/v2/fleets").get_json()["fleets"]
        ids = [f["fleet_id"] for f in fleets]
        assert "github-linux-arm64" in ids and len(ids) == 7
        arm = next(f for f in fleets if f["fleet_id"] == "github-linux-arm64")
        assert arm["title"] == "GitHub · Linux · arm64"
        assert any(a["enabled"] for a in arm["actions"])

    def test_a_fleet_reads_by_its_id(self, client, plane):
        r = client.get(f"/api/v2/fleets/{GH}")
        assert r.status_code == 200
        assert r.get_json()["fleet"]["fleet_id"] == GH

    def test_the_v1_fleets_keep_their_v1_actions_while_v1_serves_them(
            self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: str(tmp_path / "none.db"))
        monkeypatch.setitem(api_v2._status, "fn", lambda: {
            "providers_configured": {"github": True, "forgejo": False}})
        fleets = {f["fleet_id"]: f for f in
                  client.get("/api/v2/fleets").get_json()["fleets"]}
        add = next(a for a in fleets[GH]["actions"] if a["verb"] == "add")
        assert (add["url"], add["body"]) == ("/api/runner/add",
                                             {"provider": "github"})
        fj = next(a for a in fleets["forgejo-linux-x64"]["actions"]
                  if a["verb"] == "add")
        assert fj["enabled"] is False, "Forgejo is not configured in v1"
