"""T-1401: one runner card, fed by design 14.1's payload, for all six cells.

Three levels. The payload, in Python: every source of a runner - today's
containers, runners only the forge knows, controller runners in all six cells
- becomes a card with every field of 14.1, and a capability that is false
becomes its annotation. The renderer, under node: the one `cardHTML` in
templates/_card.js draws every field for every cell, and nothing in it or in
the page tests a provider or a platform. And the page, in a real browser when
one is installed: headless Edge loads the v2 page against a stub serving
those cards and the DOM is read back.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

import api_v2
import cards
import providers as P


def test_forgejo_busy_card_uses_the_workflow_job_name(monkeypatch):
    calls = []

    class Client:
        def task_info(self, repo, task_id):
            calls.append((repo, task_id))
            return {"workflow": "verify.yml", "name": "macos-latest"}

    monkeypatch.setattr(api_v2.providers, "by_key", lambda key: SimpleNamespace(
        forge_client=lambda env: Client()))
    api_v2._task_name_cache.clear()
    service = SimpleNamespace(env={"FORGEJO_INSTANCE_URL": "https://forgejo.example"})
    job = "task 1272 - FiLL/HA-Companion-App"
    expected = "FiLL/HA-Companion-App · verify.yml / macos-latest"
    assert api_v2._named_forgejo_job(service, job) == expected
    assert api_v2._named_forgejo_job(service, job) == expected
    assert calls == [("FiLL/HA-Companion-App", 1272)]

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARD_JS = os.path.join(HERE, "templates", "_card.js")
PAGE = os.path.join(HERE, "templates", "fleet_v2.html")

#: A v1 status snapshot, in the collector's own shape.
SNAPSHOT = {
    "generated": "2026-09-18T05:00:00Z",
    "host": {"ncpu": 56, "mem_total_bytes": 160 * 10 ** 9},
    "disk": {"used_bytes": 1, "total_bytes": 2, "percent": 50},
    "providers_configured": {"github": True, "forgejo": True},
    "runners": [
        {"name": "github-runner-3", "provider": "github",
         "registration": "nomercy-kvzz9", "state": "busy",
         "job": "build (NoMercy/app)", "uptime": "2 hours",
         "cpu_percent": 812.5, "mem_used": "11.2GiB", "mem_limit": "32GiB",
         "build_cache": "12.5GB", "images": "3GB", "cpuset": "0-15",
         "cpu_cores": 16},
        {"name": "forgejo-runner-1", "provider": "forgejo",
         "registration": "forgejo-runner-1", "state": "stopped", "job": "",
         "uptime": "Exited (0) 3 hours ago", "cpu_percent": 0,
         "mem_used": "0B", "mem_limit": "-", "build_cache": "0B",
         "images": "0B", "cpuset": "", "cpu_cores": None},
    ],
    "elsewhere": [
        {"uuid": "u-win", "name": "beaststack-windows-runner",
         "status": "idle", "labels": "windows, self-hosted",
         "version": "dev",
         "telemetry": {"cpu_percent": 3.5, "cpu_cores": 56,
                       "mem_used_bytes": 2 * 10 ** 9, "mem_limit_bytes": 0,
                       "disk": {"used_bytes": 600 * 10 ** 9,
                                "total_bytes": 1000 * 10 ** 9,
                                "percent": 60}}},
        {"uuid": "u-mac", "name": "beaststack-macos-sequoia",
         "status": "offline", "labels": "macos", "version": "v12.0.1",
         "telemetry": None},
    ],
}


def spec_cards():
    """A controller runner in each of the six cells."""
    out = []
    for i, (provider, platform, arch) in enumerate(
            (p.key, pl, P.X64) for p in P.ALL for pl in P.PLATFORMS):
        caps = {"kind": {"linux": "linux-container",
                         "windows": "windows-process",
                         "macos": "macos-appliance"}[platform],
                "job_containers": platform == "linux",
                "nested_builds": platform == "linux",
                "supports_drain": False, "clear_cache": True}
        out.append(cards.from_spec({
            "runner_id": f"3f2504e0-4f89-41d3-9a0c-0305e82c33{i:02d}",
            "display_name": f"rnr-{provider}-{platform}",
            "provider": provider, "platform": platform, "architecture": arch,
            "host_id": f"{platform}-worker-1", "actual_state": "idle",
            "last_seen_at": "2026-09-18T05:00:00Z",
            "current_operation": "op-123" if i == 0 else None,
            "last_error": "registered with other labels" if i == 1 else None,
            "capabilities": caps, "fleet_id": f"{provider}-{platform}-{arch}"},
            telemetry={"cpu_percent": 10.0, "mem_used_bytes": 10 ** 9,
                       "mem_limit_bytes": 8 * 10 ** 9},
            worker_reachable=True))
    return out


def every_card():
    return ([cards.from_legacy(r, SNAPSHOT["host"], SNAPSHOT["generated"])
             for r in SNAPSHOT["runners"]]
            + [cards.from_unmanaged(e, SNAPSHOT["generated"])
               for e in SNAPSHOT["elsewhere"]]
            + spec_cards())


def fleet_row(**over):
    """A bare fleet dict - just enough of design 14.4's shape for
    fleetHeadHTML, which reads title, runners, desired, available, reason
    and actions besides labels."""
    f = {"title": "GitHub · Linux · x64", "runners": [], "desired": None,
         "available": True, "reason": None, "actions": [], "labels": []}
    f.update(over)
    return f


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------

class TestThePayload:
    @pytest.mark.parametrize("card", every_card(),
                             ids=lambda c: c["key"])
    def test_every_card_carries_every_field_of_14_1(self, card):
        assert [k for k in cards.FIELDS if k not in card] == []

    def test_the_six_cells_are_six_cards_of_one_shape(self):
        shapes = {tuple(sorted(c)) for c in spec_cards()}
        assert len(shapes) == 1
        assert {(c["provider"], c["platform"]) for c in spec_cards()} == \
            {(p.key, pl) for p in P.ALL for pl in P.PLATFORMS}

    def test_a_false_capability_becomes_its_annotation(self):
        mac = next(c for c in spec_cards() if c["platform"] == "macos")
        assert "no job containers" in mac["annotations"]
        linux = next(c for c in spec_cards() if c["platform"] == "linux")
        assert "no job containers" not in linux["annotations"]

    def test_a_capability_that_is_false_disables_its_action_with_the_reason(
            self):
        c = spec_cards()[0]                 # supports_drain is False here
        drain = next(a for a in c["actions"] if a["verb"] == "drain")
        assert drain["enabled"] is False
        assert drain["reason"] == "cannot be drained"

    def test_every_card_offers_the_same_actions_in_the_same_order(self):
        for c in every_card():
            assert [a["verb"] for a in c["actions"]] == list(cards.ACTIONS)

    def test_a_runner_the_forge_knows_but_nobody_manages_says_so(self):
        win = cards.from_unmanaged(SNAPSHOT["elsewhere"][0])
        assert win["platform"] == "windows"
        assert all(not a["enabled"] for a in win["actions"])
        assert all("T-0802" in a["reason"] for a in win["actions"])

    def test_a_platform_the_labels_do_not_name_is_unknown_not_guessed(self):
        c = cards.from_unmanaged({"uuid": "x", "name": "n", "status": "idle",
                                  "labels": "self-hosted"})
        assert c["platform"] is None

    def test_absent_telemetry_is_unknown_and_says_why(self):
        mac = cards.from_unmanaged(SNAPSHOT["elsewhere"][1])
        assert mac["reachable"] is False
        assert mac["cpu"]["percent"] is None
        assert "no telemetry" in mac["last_error"]

    def test_todays_containers_keep_their_v1_routes_as_data(self):
        c = cards.from_legacy(SNAPSHOT["runners"][0])
        stop = next(a for a in c["actions"] if a["verb"] == "stop")
        assert (stop["url"], stop["body"]) == ("/api/runner/stop",
                                               {"name": "github-runner-3"})
        assert c["runner_id"] is None


# ---------------------------------------------------------------------------
# the renderer, under node
# ---------------------------------------------------------------------------

NODE = shutil.which("node")


def render(fn, payload):
    script = ("const m = require(%s);\n"
              "let s = ''; process.stdin.on('data', d => s += d);\n"
              "process.stdin.on('end', () => {\n"
              "  const items = JSON.parse(s);\n"
              "  process.stdout.write(JSON.stringify(items.map(m.%s)));\n"
              "});\n") % (json.dumps(CARD_JS), fn)
    p = subprocess.run([NODE, "-e", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestTheRenderer:
    def test_draining_is_shown_without_claiming_a_job(self):
        card = dict(spec_cards()[0], lifecycle_state="draining", state="unknown",
                    job=None)
        html = render("cardHTML", [card])[0]
        assert '>draining</div>' in html
        assert "running a job" not in html
        assert "no active job" not in html

    def test_generated_runner_number_is_hidden_on_the_card(self):
        card = dict(spec_cards()[0], display_name="github-linux-x64-2")
        html = render("cardHTML", [card])[0]
        assert '>github-linux-x64</a>' in html
        assert "github-linux-x64-2" not in html

    def test_one_function_draws_all_six_cells_and_every_field(self):
        specs = spec_cards()
        for c, html in zip(specs, render("cardHTML", specs)):
            for value in (c["display_name"], c["state"], c["platform"],
                          c["architecture"], c["worker"], c["runtime"]):
                assert value in html, (c["key"], value)
            assert "CPU" in html and "Memory" in html
            assert "reachable" in html
            assert "seen 2026-09-18 05:00:00 UTC" in html
            for note in c["annotations"]:
                assert note in html

    def test_the_operation_and_the_last_error_are_drawn(self):
        specs = spec_cards()
        html = render("cardHTML", specs[:2])
        assert "operation op-123" in html[0]
        assert "registered with other labels" in html[1]

    def test_a_storage_total_is_drawn_like_memory_is(self):
        """T-27: once a total arrives, storage reads exactly as memory does -
        percent, then used / total - not as a bare used figure."""
        c = dict(spec_cards()[0],
                 storage={"used_bytes": 12 * 10 ** 7, "total_bytes": 100 * 10 ** 9})
        html = render("cardHTML", [c])[0]
        assert "0.1%" in html and "0.12 GB / 100 GB" in html

    def test_a_disabled_action_carries_its_reason(self):
        html = render("cardHTML", [cards.from_unmanaged(
            SNAPSHOT["elsewhere"][0])])[0]
        assert "disabled" in html and "T-0802" in html

    def test_unknown_is_drawn_as_unknown_not_zero(self):
        html = render("cardHTML", [cards.from_unmanaged(
            SNAPSHOT["elsewhere"][1])])[0]
        assert 'mval">unknown' in html
        # The bar is drawn empty; no measured-looking figure is shown.
        assert not re.search(r'mval">0(\.0)?%', html)

    def test_text_is_escaped(self):
        c = dict(spec_cards()[0], display_name="<img src=x onerror=1>")
        html = render("cardHTML", [c])[0]
        assert "<img" not in html and "&lt;img" in html

    def test_labels_not_yet_seen_are_drawn_as_unknown(self):
        """The distinction this feature exists for: a runner whose labels
        have never been observed says so, rather than reading as a runner
        registered with none."""
        c = dict(spec_cards()[0], labels=None)
        html = render("cardHTML", [c])[0]
        assert "unknown" in html
        assert 'class="chip"' not in html

    def test_a_cards_labels_are_drawn_one_chip_each(self):
        c = dict(spec_cards()[0],
                 labels=["self-hosted", "Linux", "X64", "beast-unit"])
        html = render("cardHTML", [c])[0]
        assert html.count('class="chip"') == 4
        for label in c["labels"]:
            assert f'<span class="chip">{label}</span>' in html

    def test_a_labels_html_is_escaped(self):
        """A label comes from the forge's registration record, not from
        anything this page controls, so it is escaped exactly like any
        other forge-supplied text."""
        c = dict(spec_cards()[0], labels=["<img src=x onerror=1>"])
        html = render("cardHTML", [c])[0]
        assert "<img" not in html and "&lt;img" in html

    def test_label_drift_is_drawn_only_when_set(self):
        drifted = dict(spec_cards()[0],
                       label_drift="registered with other labels than the "
                                   "fleet's: unexpected labels macos-13")
        clean = dict(spec_cards()[1], label_drift=None)
        html_drift, html_clean = render("cardHTML", [drifted, clean])
        assert 'class="cwarn"' in html_drift
        assert "registered with other labels" in html_drift
        assert 'class="cwarn"' not in html_clean

    def test_a_drift_warning_is_drawn_once_not_twice(self):
        """T-17: last_note holds the same sentence as label_drift, with a
        timestamp in front - seen live on beaststack-macos-sequoia,
        2026-09-22, as the sentence twice: once amber, once grey."""
        sentence = ("registered with other labels than the fleet wants: "
                    "unexpected labels macos-13")
        c = dict(spec_cards()[0],
                 last_note=f"2026-09-22T05:00:00Z {sentence}",
                 label_drift=sentence)
        html = render("cardHTML", [c])[0]
        assert html.count(sentence) == 1
        assert 'class="cwarn"' in html
        assert 'class="cnote"' not in html

    def test_a_non_drift_note_is_still_drawn(self):
        c = dict(spec_cards()[0],
                 last_note="2026-09-22T05:00:00Z registered with labels of "
                           "its own",
                 label_drift=None)
        html = render("cardHTML", [c])[0]
        assert 'class="cnote"' in html
        assert "registered with labels of its own" in html

    def test_a_fleet_head_lists_a_chip_per_label(self):
        f = fleet_row(labels=["self-hosted", "Linux", "X64", "beast-unit"])
        html = render("fleetHeadHTML", [f])[0]
        assert "runs-on:" in html
        assert html.count('class="chip"') == 4
        for label in f["labels"]:
            assert f'<span class="chip">{label}</span>' in html

    def test_a_fleet_head_says_why_it_needs_limits(self):
        why = ("github-linux-x64 has no memory limit, and a Linux runner is "
               "never created without one; set it on the Settings page")
        html = render("fleetHeadHTML", [fleet_row(limits_problem=why)])[0]
        assert 'class="cwarn flimits"' in html and why in html
        html = render("fleetHeadHTML", [fleet_row(limits_problem=None)])[0]
        assert "flimits" not in html

    def test_a_fleet_head_always_shows_its_runner_group(self):
        open_ = {"known": True, "group": "Default", "visibility": "all",
                 "allows_public_repositories": True, "default": True,
                 "source": "GitHub default group"}
        html = render("fleetHeadHTML", [fleet_row(runner_group_policy=open_)])[0]
        assert 'class="chip danger rgroup"' in html
        assert "Default" in html and "public repositories allowed" in html
        closed = dict(open_, group="Stoney", visibility="selected",
                      allows_public_repositories=False)
        html = render("fleetHeadHTML", [fleet_row(runner_group_policy=closed)])[0]
        assert 'class="chip rgroup"' in html
        assert "Stoney" in html and "no public repositories" in html
        unknown = {"known": False, "group": None, "why": "GitHub could not be read"}
        html = render("fleetHeadHTML", [fleet_row(runner_group_policy=unknown)])[0]
        assert 'class="chip unknown rgroup"' in html
        assert "runner group unknown" in html
        assert 'title="GitHub could not be read"' in html
        html = render("fleetHeadHTML", [fleet_row(runner_group_policy=None)])[0]
        assert "rgroup" not in html

    def test_a_fleet_head_with_no_labels_says_nothing(self):
        f = fleet_row(labels=[])
        html = render("fleetHeadHTML", [f])[0]
        assert "runs-on:" not in html
        assert 'class="flabels"' not in html

    def test_the_fields_list_is_the_designs(self):
        p = subprocess.run([NODE, "-e", "process.stdout.write(JSON.stringify("
                            "require(%s).CARD_FIELDS))" % json.dumps(CARD_JS)],
                           capture_output=True, text=True, timeout=60)
        assert tuple(json.loads(p.stdout)) == cards.FIELDS


# ---------------------------------------------------------------------------
# no platform or provider is ever tested in the page
# ---------------------------------------------------------------------------

class TestNoConditionalNamesAPlatformOrAForge:
    WORDS = ("github", "forgejo", "linux", "windows", "macos")

    def sources(self):
        for path in (CARD_JS, PAGE):
            with open(path, encoding="utf-8") as fh:
                yield os.path.basename(path), fh.read()

    def test_no_comparison_names_one(self):
        offenders = []
        for name, src in self.sources():
            for m in re.finditer(r"(===?|!==?)\s*['\"](\w+)['\"]|"
                                 r"['\"](\w+)['\"]\s*(===?|!==?)", src):
                word = (m.group(2) or m.group(3) or "").lower()
                if word in self.WORDS:
                    offenders.append(f"{name}: {m.group(0)}")
            for m in re.finditer(r"\b(provider|platform)\s*(===?|!==?)", src):
                offenders.append(f"{name}: {m.group(0)}")
        assert offenders == []

    def test_the_check_would_catch_one(self):
        src = "if (c.platform === 'macos') {}"
        assert re.search(r"(===?|!==?)\s*['\"](\w+)['\"]", src)


# ---------------------------------------------------------------------------
# the page's data
# ---------------------------------------------------------------------------

@pytest.fixture
def no_control_plane(tmp_path, monkeypatch):
    monkeypatch.setattr(api_v2, "_db_path",
                        lambda: str(tmp_path / "control.db"))
    monkeypatch.setitem(api_v2._status, "fn", lambda: SNAPSHOT)
    return tmp_path


class TestThePagesData:
    def test_eight_fleets_and_every_runner_in_one(self, client,
                                                no_control_plane):
        body = client.get("/api/v2/fleet").get_json()
        assert len(body["fleets"]) == 8
        assert body["control_plane"]["running"] is False
        placed = [k for f in body["fleets"] for k in f["runners"]]
        assert sorted(placed) == sorted(c["key"] for c in body["runners"])

    def test_the_windows_service_sits_in_its_own_fleet(self, client,
                                                       no_control_plane):
        body = client.get("/api/v2/fleet").get_json()
        fleet = next(f for f in body["fleets"]
                     if f["fleet_id"] == "forgejo-windows-x64")
        assert fleet["runners"] == ["forge:u-win"]

    def test_reading_it_creates_no_control_database(self, client,
                                                    no_control_plane):
        client.get("/api/v2/fleet")
        assert not (no_control_plane / "control.db").exists()

    def test_the_page_is_served(self, client, no_control_plane):
        r = client.get("/")
        assert r.status_code == 200
        assert b"cardHTML" in r.data and b"/api/v2/fleet" in r.data
        assert b'href="/users">Access</a>' in r.data


# ---------------------------------------------------------------------------
# the page, in a real browser
# ---------------------------------------------------------------------------

def test_the_page_renders_all_eight_fleets_in_a_real_browser(tmp_path,
                                                             monkeypatch):
    """A stub serving the page and the v2 data - no docker, no forge, no
    sign-in - loaded by headless Edge, whose DOM is then read back."""
    from flask import Flask, render_template
    from werkzeug.serving import make_server

    import browser
    if not browser.works(tmp_path):
        pytest.skip(browser.reason())

    monkeypatch.setattr(api_v2, "_db_path",
                        lambda: str(tmp_path / "control.db"))
    stub = Flask("stub", template_folder=os.path.join(HERE, "templates"))
    # init() sets the module's status source; restored after the test.
    monkeypatch.setitem(api_v2._status, "fn", api_v2._status["fn"])
    api_v2.init(stub, lambda: SNAPSHOT)
    stub.add_url_rule("/v2", "page",
                      lambda: render_template("fleet_v2.html", role="admin"))
    server = make_server("127.0.0.1", 0, stub)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        dom = browser.dump_dom(f"http://127.0.0.1:{port}/v2",
                               tmp_path / "edge")
    finally:
        server.shutdown()
    assert dom.count('class="fleet"') == 8, dom[-2000:]
    assert dom.count("<article") == 4, "two containers, two forge-only"
    assert 'id="s-total">4</div>' in dom
    assert '<div class="k">Total runners</div>' in dom
    assert '<div class="k">Online</div>' in dom
    assert "beaststack-windows-runner" in dom
    assert "no job containers" in dom
    assert "T-0802" in dom
    assert "wsl:github-runners" in dom


def test_every_kind_of_input_the_pages_use_is_styled():
    """A field the stylesheet does not name is a white box on a dark page,
    which is what the capacity field was until 2026-09-20."""
    import re
    base = open(os.path.join(HERE, "templates", "base.html"),
                encoding="utf-8").read()
    styled = set(re.findall(r"input\[type=(\w+)\]", base))
    used = set()
    for name in os.listdir(os.path.join(HERE, "templates")):
        if not name.endswith(".html"):
            continue
        page = open(os.path.join(HERE, "templates", name),
                    encoding="utf-8").read()
        used |= set(re.findall(r"<input[^>]*type=[\"'](\w+)[\"']", page))
    assert used - styled - {"hidden", "checkbox", "radio", "submit"} == set()


def test_the_page_asks_for_no_numbers():
    """A fleet has the runners you add, and keeps them until you remove one.
    Typing a number was a second way of saying the same thing, and the box
    beside the buttons read as a stray box (2026-09-20)."""
    card_js = open(os.path.join(HERE, "templates", "_card.js"),
                   encoding="utf-8").read()
    assert 'type="number"' not in card_js
    assert "data-capacity" not in card_js
