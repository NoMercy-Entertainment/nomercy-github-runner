"""Retired empty fleets stay absent from every dashboard entry point."""
import json

import pytest

from store import schema
from store.fleets import FleetStore, UnknownFleet
from tests.test_fleet_routes import plane

DISABLED = ['github-windows-arm64', 'forgejo-windows-arm64']


@pytest.fixture
def retired(plane):
    service, path = plane
    with schema.connect(path) as connection:
        connection.execute("INSERT INTO platform_settings(key,value) VALUES('disabled_fleets',?)",
                           (json.dumps(DISABLED),))
    return service, path


def test_restart_seeding_keeps_retired_fleets_disabled(retired):
    service, path = retired
    service.fleets.seed({'FORGEJO_RUNNER_ARTIFACT_WINDOWS_ARM64': 'arm.exe'})
    assert len(service.fleets.list()) == 6
    for fid in DISABLED:
        assert service.fleets.get(fid) is None
        with schema.connect(path) as connection:
            row = connection.execute('SELECT desired_capacity,available FROM fleets WHERE fleet_id=?',
                                     (fid,)).fetchone()
        assert tuple(row) == (0, 0)
        with pytest.raises(UnknownFleet):
            service.fleets.set_capacity(fid, 1)


def test_fleet_and_settings_payloads_omit_retired_fleets(client, retired):
    for endpoint in ('/api/v2/fleets', '/api/v2/settings'):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert len(response.json['fleets']) == 6
        assert 'windows-arm64' not in response.get_data(as_text=True)


@pytest.mark.parametrize('fid', DISABLED)
def test_retired_fleet_has_no_detail_or_add_endpoint(client, retired, fid):
    assert client.get('/api/v2/fleets/' + fid).status_code == 404
    response = client.post('/api/v2/fleets/' + fid + '/runners', json={},
                           headers={'Idempotency-Key': 'retired:' + fid})
    assert response.status_code == 404


def test_fallback_cannot_reintroduce_retired_cells(retired, monkeypatch):
    import api_v2
    service, path = retired
    monkeypatch.setattr(schema, 'DB_PATH', path)
    assert len(api_v2.fleet_list(None, None, [])) == 6


def test_sharing_profile_choices_omit_retired_windows_arm(client, retired):
    response = client.get('/federation')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'windows-arm64' not in text
    assert 'github-windows-x64' in text and 'github-macos-x64' in text
