"""What one fleet subscriber is sent, and when it is sent nothing at all.

The socket is event-driven: one snapshot on connect to establish a baseline,
then only what actually changed. A collector tick that produces an identical
picture puts nothing on the wire - the connection stays open on ping/pong
alone. Pushing the whole snapshot on a timer would be polling with a socket
around it.

The loop is a generator over injected callables so all of this can be asserted
without a real connection. The property that matters most is the last one: the
access model rests on the role being re-read continuously, and a socket is a
single request that lives for hours. Without it the model quietly degrades to
"revoked when they close the tab", and every existing test still passes,
because they all speak HTTP.
"""
import app as dash


def snap(runners=None, disk=None, generated="t0"):
    return {"generated": generated, "disk": disk or {"used": 1},
            "runners": runners or []}


def runner(name, **fields):
    base = {"name": name, "state": "idle", "job": "", "cpu_percent": 0.0,
            "mem_used": "100MiB", "build_cache": "0B", "uptime": "1 hour"}
    base.update(fields)
    return base


# ------------------------------------------------------------------ the diff

def test_two_identical_snapshots_produce_nothing():
    """The whole point: an idle fleet is silent."""
    a = snap([runner("github-runner-2")])
    assert dash.diff_status(a, snap([runner("github-runner-2")])) is None


def test_only_the_changed_field_of_the_changed_runner_is_reported():
    before = snap([runner("github-runner-2"), runner("github-runner-3")])
    after = snap([runner("github-runner-2", cpu_percent=42.0),
                  runner("github-runner-3")])

    delta = dash.diff_status(before, after)

    assert delta["runners"] == {"github-runner-2": {"cpu_percent": 42.0}}


def test_a_state_change_is_reported_with_its_job():
    before = snap([runner("github-runner-7")])
    after = snap([runner("github-runner-7", state="busy", job="build-base")])

    delta = dash.diff_status(before, after)

    assert delta["runners"]["github-runner-7"] == {"state": "busy",
                                                   "job": "build-base"}


def test_a_new_runner_arrives_whole():
    """A diff of a runner the client has never seen is useless to it."""
    delta = dash.diff_status(snap([]), snap([runner("github-runner-9")]))
    assert delta["runners"]["github-runner-9"]["name"] == "github-runner-9"
    assert delta["runners"]["github-runner-9"]["state"] == "idle"


def test_a_removed_runner_is_named_so_the_client_can_drop_it():
    delta = dash.diff_status(snap([runner("github-runner-9")]), snap([]))
    assert delta["gone"] == ["github-runner-9"]


def test_a_disk_change_is_reported():
    delta = dash.diff_status(snap(disk={"used": 1}), snap(disk={"used": 2}))
    assert delta["disk"] == {"used": 2}


def test_the_timestamp_alone_is_not_a_change():
    """generated moves every tick; on its own it is not worth a frame."""
    a = snap([runner("github-runner-2")], generated="t0")
    b = snap([runner("github-runner-2")], generated="t1")
    assert dash.diff_status(a, b) is None


# ------------------------------------------------------------------ the loop

class Clock:
    """Stands in for the collector: hands out wakes, then ends the test."""

    def __init__(self, wakes):
        self.remaining = list(wakes)

    def __call__(self):
        if not self.remaining:
            raise StopIteration()
        return self.remaining.pop(0)


def frames(authorised, wakes, snapshots):
    seen = []
    snaps = iter(snapshots)
    gen = dash.fleet_frames(authorised, Clock(wakes), lambda: next(snaps))
    try:
        for f in gen:
            seen.append(f)
    except (StopIteration, RuntimeError):
        pass
    return seen


def test_a_subscriber_gets_one_full_snapshot_on_connect():
    base = snap([runner("github-runner-2")])
    seen = frames(lambda: True, [], [base])
    assert seen == [{"type": "snapshot", "data": base}]


def test_an_unchanged_tick_puts_nothing_on_the_wire():
    base = snap([runner("github-runner-2")])
    seen = frames(lambda: True, [True, True],
                  [base, snap([runner("github-runner-2")]),
                   snap([runner("github-runner-2")])])
    assert seen == [{"type": "snapshot", "data": base}]


def test_a_change_is_sent_as_an_update_carrying_only_the_change():
    base = snap([runner("github-runner-2")])
    seen = frames(lambda: True, [True],
                  [base, snap([runner("github-runner-2", state="busy",
                                       job="deploy")])])
    assert seen[1] == {"type": "update",
                       "data": {"runners": {"github-runner-2":
                                            {"state": "busy", "job": "deploy"}}}}


def test_nothing_is_sent_to_an_identity_with_no_role():
    assert frames(lambda: False, [True], [snap()]) == []


def test_the_stream_stops_when_access_is_revoked_mid_connection():
    """The property the whole access model rests on."""
    calls = {"n": 0}

    def authorised():
        calls["n"] += 1
        return calls["n"] <= 2

    seen = frames(authorised, [True, True],
                  [snap([runner("r1")]),
                   snap([runner("r1", state="busy")]),
                   snap([runner("r1", state="idle")])])
    assert [f["type"] for f in seen] == ["snapshot", "update"]


def test_revocation_is_noticed_even_when_nothing_is_changing():
    """A quiet fleet must not keep a revoked socket alive."""
    calls = {"n": 0}

    def authorised():
        calls["n"] += 1
        return calls["n"] <= 1

    seen = frames(authorised, [True, True],
                  [snap([runner("r1")]), snap([runner("r1")]),
                   snap([runner("r1")])])
    assert [f["type"] for f in seen] == ["snapshot"]


# ------------------------------------------------------------- the handshake

def test_an_unauthenticated_handshake_is_refused_as_json(anon_client):
    """A WebSocket client cannot read a redirect to a login page."""
    r = anon_client.get("/ws/fleet")
    assert r.status_code == 401
    assert r.is_json
