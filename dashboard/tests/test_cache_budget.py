from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from control.reconciler import Reconciler


@pytest.mark.parametrize('state,age,used,pending,queued', [
    ('idle', 1, 41, None, True),
    ('busy', 1, 41, None, False),
    ('idle', 100, 41, None, False),
    ('idle', 1, 40, None, False),
    ('idle', 1, 41, 'another-operation', False),
])
def test_budget_preserves_active_jobs_and_rejects_stale_measurements(state, age, used, pending, queued):
    calls = []
    reconciler = object.__new__(Reconciler)
    reconciler.service = SimpleNamespace(clear_cache=lambda *a, **kw: calls.append((a, kw)))
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).strftime('%Y-%m-%dT%H:%M:%SZ')
    spec = dict(platform='windows', runner_id='runner', actual_state=state,
                desired_state='running', current_operation=pending,
                cache_policy={'max_bytes': 40, 'enabled': True},
                telemetry={'cache_bytes': used, 'cache_at': stamp})
    reconciler._queue_cache_budget(spec, SimpleNamespace(did=lambda *a: None))
    assert bool(calls) is queued
    if calls:
        assert calls[0][1]['requested_by'] == 'cache-budget'
