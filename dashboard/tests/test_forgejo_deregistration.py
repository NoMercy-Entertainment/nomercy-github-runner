"""T-1002: a Forgejo record is only ever deleted through the API, and a
removal that cannot reach the API is refused, not forced.

MEASURED when the design was written: `forgejo-runner` has no `unregister`
subcommand. A Forgejo runner whose unit goes away any other way leaves an
offline record in Forgejo for ever. So there is exactly one door - the API
delete, through `control/forges.py` - and when it does not open, the unit
stays, because the unit is the last thing that says which record is whose.
"""
import io
import urllib.error

import pytest

import forgejo_api
import providers as P
from control.forges import ForgeUnreachable, LiveForges
from store import storage
from tests.fake_runtime import UnitRuntime
from tests.test_partial_failure import FJ, passes, the_runner  # noqa: F401
from tests.test_partial_failure import world  # noqa: F401

ENV = {"FORGEJO_INSTANCE_URL": "https://git.nomercy.tv",
       "FORGEJO_API_TOKEN": "fj-token-0000000000"}


class TestTheOneDoor:
    def test_records_come_from_the_provider(self, monkeypatch):
        monkeypatch.setattr(P.FORGEJO, "forge_records",
                            lambda env: [{"uuid": "u"}])
        assert LiveForges(ENV).records(P.FORGEJO) == [{"uuid": "u"}]

    def test_a_forge_that_cannot_be_read_is_unknown_not_empty(self,
                                                              monkeypatch):
        def boom(env):
            raise OSError("down")
        monkeypatch.setattr(P.FORGEJO, "forge_records", boom)
        assert LiveForges(ENV).records(P.FORGEJO) is None

    def test_a_confirmed_delete_is_true(self, monkeypatch):
        monkeypatch.setattr(P.FORGEJO, "delete_record", lambda env, rid: True)
        assert LiveForges(ENV).delete(P.FORGEJO, "12") is True

    def test_an_unconfirmed_delete_is_refused_with_its_reason(self,
                                                              monkeypatch):
        monkeypatch.setattr(P.FORGEJO, "delete_record",
                            lambda env, rid: False)
        with pytest.raises(ForgeUnreachable, match="did not confirm"):
            LiveForges(ENV).delete(P.FORGEJO, "12")

    def test_a_delete_with_no_id_is_refused(self):
        with pytest.raises(ValueError):
            LiveForges(ENV).delete(P.FORGEJO, "")

    def test_it_goes_all_the_way_to_forgejos_api(self, monkeypatch):
        seen = []

        class Gone:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            seen.append((req.get_method(), req.full_url))
            return Gone()
        monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", urlopen)
        assert LiveForges(ENV).delete(P.FORGEJO, "12") is True
        assert seen == [("DELETE", "https://git.nomercy.tv/api/v1/user/"
                                   "actions/runners/12")]

    def test_a_network_that_is_down_is_a_refusal(self, monkeypatch):
        def urlopen(req, timeout=None):
            raise urllib.error.URLError("Name or service not known")
        monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", urlopen)
        with pytest.raises(ForgeUnreachable):
            LiveForges(ENV).delete(P.FORGEJO, "12")

    def test_a_500_is_a_refusal(self, monkeypatch):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "x", {},
                                         io.BytesIO(b""))
        monkeypatch.setattr(forgejo_api.urllib.request, "urlopen", urlopen)
        with pytest.raises(ForgeUnreachable):
            LiveForges(ENV).delete(P.FORGEJO, "12")


class TestTheRemovalWaitsForIt:
    def stopped(self, world):
        service, flow, agent, forges, reconciler = world
        service.scale_up(FJ)
        passes(service, reconciler)
        spec = the_runner(service, FJ)
        service.stop(spec["runner_id"])
        passes(service, reconciler)
        return the_runner(service, FJ)

    def test_the_runner_is_never_asked_to_deregister_itself(self, world):
        """It cannot. Only the API can."""
        service, flow, agent, forges, reconciler = world
        spec = self.stopped(world)
        service.remove(spec["runner_id"])
        passes(service, reconciler)
        assert not any(c[0] == "deregister" for c in agent.calls)
        assert [k for k, _ in forges.deleted] == ["forgejo"]

    def test_a_forge_that_cannot_be_reached_refuses_the_removal(self, world):
        service, flow, agent, forges, reconciler = world
        spec = self.stopped(world)
        real = forges.delete

        def unreachable(provider, rid):
            raise ForgeUnreachable("forgejo: the forge could not be asked")
        forges.delete = unreachable
        service.remove(spec["runner_id"])
        passes(service, reconciler)
        spec = service.specs.get(spec["runner_id"])
        assert spec["actual_state"] != "absent"
        assert storage.unit_name(spec["runner_id"]) in UnitRuntime.units, \
            "the unit is kept: it is the last thing that says whose record"
        assert "could not be asked" in spec["last_error"]

        # It waits in `deregistering`, which the reconciler keeps driving, so
        # when the forge answers again the removal finishes by itself.
        assert spec["actual_state"] == "deregistering"
        forges.delete = real
        passes(service, reconciler)
        assert storage.unit_name(spec["runner_id"]) not in UnitRuntime.units
        assert ("forgejo", spec["registration_id"]) in forges.deleted
