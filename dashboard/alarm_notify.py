"""Alarms sent out, to wherever the owner chose (GitHub #11).

Optional. ALARM_WEBHOOK_URL - from the environment, or set in Settings like
the forge tokens, where it is sealed in the SecretStore - names the
receiver, and ALARM_WEBHOOK_FORMAT how to speak to it: `ntfy`, `discord`,
`slack` or `json`. Unset, the format follows the URL (a Discord or Slack
webhook, an ntfy host) and is otherwise plain JSON. Without a URL there is
the banner and nothing else.

**Once each.** Every raise and every resolve becomes one row in
`alarm_outbox`, unique on (alarm, event, raised_at), so a second pass, a
restart or a retry cannot send the same thing twice. A raise that happened
while no URL was set is marked skipped, not sent late when one is.

**Again, later, when it fails.** A failed send waits 30 s, then 60, 120 ...
up to half an hour, and is given up after MAX_ATTEMPTS.

**The URL is a credential** - a Discord or Slack webhook URL is all it takes
to post as it - so no log line, error column or payload carries it.
"""
import fnmatch
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import alarms
from control.secrets import http_url
from store import schema

FORMATS = ("ntfy", "discord", "slack", "json")
MAX_ATTEMPTS = 8
TIMEOUT = 10


def _now_of(event):
    """When the event happened: an outbox row is due from then."""
    return alarms.epoch(event.get("resolved_at") or event.get("raised_at")) or time.time()


#: The alarm kinds that are a runner's own online and offline status, which
#: the Settings page can turn off for Discord (job_notify.get_settings).
RUNNER_KINDS = ("runner_offline", "runner_planned")


def enqueue(path, event, now=None):
    """One outbox row for a raise or a resolve, or a job's start or finish;
    a second is ignored. An acknowledgement itself is not sent; the resolve
    that follows one is, since "it is back" is still worth hearing (the
    owner's choice, 2026-10-08)."""
    if event.get("event") not in ("raised", "resolved", "started", "finished"):
        return
    if event.get("kind") in RUNNER_KINDS:
        import job_notify
        if not job_notify.get_settings(path)["runners"]:
            return
    now = _now_of(event) if now is None else now
    with schema.connect(path) as c:
        c.execute("INSERT OR IGNORE INTO alarm_outbox (alarm_key, event, raised_at,"
                  " payload, created_at, next_at) VALUES (?,?,?,?,?,?)",
                  (event["key"], event["event"], event.get("raised_at") or "",
                   json.dumps(event), alarms.iso(now), alarms.iso(now)))


def format_of(env, url):
    named = str((env or {}).get("ALARM_WEBHOOK_FORMAT") or "").strip().lower()
    if named in FORMATS:
        return named
    parsed = urllib.parse.urlsplit(url or "")
    host = (parsed.hostname or "").lower()
    if host in ("discord.com", "discordapp.com") and parsed.path.startswith("/api/webhooks/"):
        return "discord"
    if host == "hooks.slack.com":
        return "slack"
    if "ntfy" in host:
        return "ntfy"
    return "json"


def _duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    return f"{minutes // 60}h {minutes % 60}m"


def _job_lines(event):
    forge = alarms.FORGES.get(event.get("forge"), event.get("forge") or "")
    where = " · ".join(x for x in (event.get("repo"), event.get("workflow")) if x)
    who = " · ".join(x for x in (forge, event.get("runner"),
                                 _duration(event.get("duration_s"))
                                 if event.get("event") == "finished" else None) if x)
    return where, who


def _job_title(event):
    word = "Started" if event.get("event") == "started" else (event.get("result") or "Finished")
    return f"{word}: {event.get('job') or 'job'}"


def text_of(event):
    if event.get("kind") == "job":
        where, who = _job_lines(event)
        text = "\n".join(x for x in (_job_title(event), where, who) if x)
        return f"{text}\n{event['url']}" if event.get("url") else text
    if event.get("kind") == "runner_planned":
        why = (event.get("detail") or {}).get("why") or "offline on purpose"
        if event["event"] == "raised":
            return f"{why.capitalize()}: {event.get('subject')} - {event.get('message')}"
        return f"RESOLVED: {event.get('subject')} {event.get('reason') or 'back online'}"
    word = "ALARM" if event["event"] == "raised" else "RESOLVED"
    text = f"{word}: {event['message']}"
    url = (event.get("detail") or {}).get("url")
    return f"{text}\n{url}" if url else text


def _header(value):
    """A header value http.client can send: latin-1, one line."""
    return " ".join(str(value).split()).encode("latin-1", "replace").decode("latin-1")


#: Embed colours: an alarm, its resolve, and the monitor itself; grey for
#: a runner taken down on purpose and a job canceled, blue for a job begun.
RED, GREEN, AMBER, GREY, BLUE = 0xE5484D, 0x30A46C, 0xF5A524, 0x8B8D98, 0x3E63DD

#: A finished job's colour by its result: green, red, otherwise grey.
RESULT_COLOURS = {"Succeeded": GREEN, "Failed": RED, "TimedOut": RED}


def job_embed(event):
    """One job, compact - about 500 a day: a title that is the result (or
    "Started") and the job, two short lines, the link, the time."""
    where, who = _job_lines(event)
    started = event.get("event") == "started"
    embed = {"title": _cut(_job_title(event), 256),
             "description": _cut("\n".join(x for x in (where, who) if x), 4096),
             "color": BLUE if started else RESULT_COLOURS.get(event.get("result"), GREY),
             "timestamp": (event.get("started_at") if started
                           else event.get("ended_at")) or event.get("started_at")}
    url = event.get("url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        embed["url"] = url
    return embed

_TITLES = {("runner_offline", True): "Runner offline", ("runner_offline", False): "Back online",
           ("job_queued", True): "Job waiting", ("job_queued", False): "Job picked up",
           ("monitor", True): "Alarm monitor blind", ("monitor", False): "Alarm monitor sees again"}


def _cut(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def discord_embed(event):
    """A card like the dev journal's posts: a coloured bar, a title, the
    sentence, named fields, and a timestamp Discord shows in each reader's
    own time zone. Field times are in the dashboard's local time."""
    raised = event["event"] == "raised"
    kind = event.get("kind") or "monitor"
    detail = event.get("detail") or {}
    forge = alarms.FORGES.get(event.get("forge"), event.get("forge") or "")
    fields = []
    if kind != "monitor":
        fields.append({"name": "Forge", "value": _cut(forge, 1024), "inline": True})
    labels = ", ".join(detail.get("labels") or [])
    if labels:
        fields.append({"name": "Labels", "value": _cut(labels, 1024), "inline": True})
    since_name = {"runner_offline": "Offline since", "runner_planned": "Offline since",
                  "job_queued": "Queued since"}.get(kind, "Since")
    fields.append({"name": since_name, "value": alarms.local_time(event.get("since")), "inline": False})
    if not raised:
        start, end = alarms.epoch(event.get("since")), alarms.epoch(event.get("resolved_at"))
        if start is not None and end is not None:
            fields.append({"name": "Down for" if kind in RUNNER_KINDS else "Lasted",
                           "value": alarms.duration(end - start), "inline": True})
        if event.get("resolved_at"):
            fields.append({"name": "Back since", "value": alarms.local_time(event["resolved_at"]),
                           "inline": True})
    title = f"{_TITLES.get((kind, raised), 'Alarm' if raised else 'Resolved')}: {event.get('subject') or forge}"
    colour = (AMBER if kind == "monitor" else RED) if raised else GREEN
    if kind == "runner_planned":
        why = detail.get("why") or "offline on purpose"
        title = (f"{why[:1].upper()}{why[1:]}: {event.get('subject')}" if raised
                 else f"{'Back online' if (event.get('reason') or '') == 'back online' else 'Resolved'}"
                      f": {event.get('subject')}")
        colour = GREY if raised else GREEN
    embed = {"title": _cut(title, 256),
             "description": _cut(event.get("message"), 4096),
             "color": colour,
             "fields": fields[:25],
             "footer": {"text": "NoMercy runners · alarm monitor"},
             "timestamp": (event.get("since") if raised else event.get("resolved_at")) or event.get("since")}
    url = detail.get("url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        embed["url"] = url
    return embed


#: A Discord user id: a snowflake, 17 to 20 digits. Nothing else is ever
#: put in a mention.
_SNOWFLAKE = re.compile(r"^[0-9]{17,20}$")


def _pairs(text):
    """"a=b, c=d" as [(a, b), ...], lower-cased keys, blanks dropped."""
    out = []
    for part in re.split(r"[,;\n]", str(text or "")):
        if "=" in part:
            key, value = part.split("=", 1)
            if key.strip() and value.strip():
                out.append((key.strip().lower(), value.strip()))
    return out


def owners_of(event, env):
    """Who an alarm is for - by the runner's name (ALARM_RUNNER_OWNERS,
    shell patterns), by a waiting job's labels (ALARM_LABEL_OWNERS), else
    ALARM_DISCORD_DEFAULT - as Discord user ids (ALARM_DISCORD_PEOPLE).
    The platform's own runners have no entry and fall to the default."""
    env = env or {}
    people = {name: value for name, value in _pairs(env.get("ALARM_DISCORD_PEOPLE"))
              if _SNOWFLAKE.match(value)}
    if not people:
        return []
    kind = event.get("kind")
    names = []
    if kind == "runner_offline":
        subject = str(event.get("subject") or "").lower()
        names = [owner for pattern, owner in _pairs(env.get("ALARM_RUNNER_OWNERS"))
                 if fnmatch.fnmatchcase(subject, pattern)][:1]
    elif kind == "job_queued":
        labels = {str(l).lower() for l in (event.get("detail") or {}).get("labels") or []}
        names = [owner for label, owner in _pairs(env.get("ALARM_LABEL_OWNERS"))
                 if label in labels]
    if not names:
        names = [str(env.get("ALARM_DISCORD_DEFAULT") or "").strip()]
    ids = []
    for name in names:
        user = people.get(name.lower())
        if user and user not in ids:
            ids.append(user)
    return ids


def render(fmt, event, env=None):
    """(body bytes, headers) of one event in one format."""
    raised = event["event"] == "raised"
    text = text_of(event)
    if fmt == "discord" and event.get("kind") == "job":
        return (json.dumps({"embeds": [job_embed(event)], "allowed_mentions": {"parse": []}})
                .encode("utf-8"), {"Content-Type": "application/json"})
    if fmt == "ntfy":
        headers = {"Content-Type": "text/plain; charset=utf-8",
                   "Title": _header(("Runner alarm: " if raised else "Runner alarm resolved: ")
                                    + str(event.get("subject") or "")),
                   "Priority": "urgent" if raised else "default",
                   "Tags": "rotating_light" if raised else "white_check_mark"}
        url = (event.get("detail") or {}).get("url") or event.get("url")
        if url:
            headers["Click"] = _header(url)
        return text.encode("utf-8"), headers
    if fmt == "discord":
        # No mentions: a job or runner name is not ours to choose, and
        # "@everyone" in one must not page a whole server.
        # Only the owners' own ids may ping, and only on the red message;
        # a name in a runner or job ("@everyone") stays text.
        body = {"embeds": [discord_embed(event)], "allowed_mentions": {"parse": []}}
        users = owners_of(event, env) if raised and event.get("kind") in (
            "runner_offline", "job_queued", "monitor", None) else []
        if users:
            body["content"] = " ".join(f"<@{u}>" for u in users)
            body["allowed_mentions"]["users"] = users
    elif fmt == "slack":
        # Slack's own escaping: with &, < and > as entities, no <!channel>,
        # <!here> or <@user> in a job or runner name can mention anyone.
        body = {"text": text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;")}
    elif event.get("kind") == "job":
        body = {"event": event["event"],
                "job": {k: event.get(k) for k in (
                    "key", "forge", "repo", "workflow", "job", "runner", "url",
                    "result", "duration_s", "started_at", "ended_at")}}
    else:
        body = {"event": event["event"],
                "alarm": {k: event.get(k) for k in (
                    "key", "kind", "forge", "subject", "since", "raised_at",
                    "resolved_at", "reason", "message", "detail")}}
    return json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


def _post(url, body, headers):
    """(sent, why not). `why` never carries the URL: a status code or the
    name of the error, nothing an exception's text could have copied."""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=dict(headers, **{"User-Agent": "nomercy-runner-alarms"}))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status = getattr(r, "status", 200)
            return (200 <= status < 300), (None if 200 <= status < 300 else f"HTTP {status}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return False, "HTTP 429", _retry_after(e)
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _retry_after(error):
    """Seconds Discord asks to wait: its JSON body's retry_after, else the
    Retry-After header, else a few seconds."""
    try:
        return max(0.5, float(json.loads(error.read().decode() or "{}")["retry_after"]))
    except Exception:  # noqa: BLE001
        pass
    try:
        return max(0.5, float((error.headers or {}).get("Retry-After")))
    except Exception:  # noqa: BLE001
        return 5.0


#: How often the delivery thread looks for what is due.
DELIVERY_SECONDS = 5

#: Pause between two posts of one pass: Discord takes about five a second
#: per webhook before it answers 429.
PACE_SECONDS = 0.6

#: Job messages per post: Discord carries up to ten embeds in one message.
BATCH = 10


def run_forever(get_plane, sleep=time.sleep, clock=time.time):
    """The delivery thread, apart from the runner checks: a receiver that
    hangs for every send of a pass delays only other sends. `get_plane`
    answers (path, env, specs) or None, like the monitor's."""
    while True:
        try:
            plane = get_plane()
            if plane is not None:
                path, env, _ = plane
                deliver_due(path, env, clock())
        except Exception as e:  # noqa: BLE001 - a pass never stops the loop
            print(f"[alarms] delivery: {type(e).__name__}")
        sleep(DELIVERY_SECONDS)


def backoff(attempts):
    return min(30 * 2 ** (attempts - 1), 1800)


def deliver_due(path, env, now, post=None, pace=PACE_SECONDS):
    """Send every outbox row that is due. Called by the delivery thread.

    Job messages to Discord go ten to a post. A 429 is waited out: every row
    still to send waits its retry_after, and none of them counts it as a
    failed attempt."""
    post = post or _post
    url = str((env or {}).get("ALARM_WEBHOOK_URL") or "").strip()
    stamp = alarms.iso(now)
    with schema.connect(path) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM alarm_outbox WHERE state='pending' AND next_at<=?"
            " ORDER BY id LIMIT 100", (stamp,))]
        if not url:
            c.executemany("UPDATE alarm_outbox SET state='skipped', done_at=? WHERE id=?",
                          [(stamp, r["id"]) for r in rows])
            return []
        if not http_url(url):
            # Set in the environment, where nothing checked it. Said once
            # per row, without the value.
            c.executemany("UPDATE alarm_outbox SET state='skipped', done_at=?,"
                          " last_error='ALARM_WEBHOOK_URL is not an http(s) URL'"
                          " WHERE id=?", [(stamp, r["id"]) for r in rows])
            if rows:
                print("[alarms] not sent: ALARM_WEBHOOK_URL is not an http(s) URL")
            return []
    fmt = format_of(env, url)
    batches, jobs = [], []
    for row in rows:
        event = json.loads(row["payload"])
        if fmt == "discord" and event.get("kind") == "job":
            jobs.append((row, event))
            if len(jobs) == BATCH:
                batches.append(jobs)
                jobs = []
        else:
            if jobs:
                batches.append(jobs)
                jobs = []
            batches.append([(row, event)])
    if jobs:
        batches.append(jobs)
    sent = []
    for n, batch in enumerate(batches):
        if n and pace:
            time.sleep(pace)
        try:
            if len(batch) > 1 or batch[0][1].get("kind") == "job" and fmt == "discord":
                body = json.dumps({"embeds": [job_embed(e) for _, e in batch],
                                   "allowed_mentions": {"parse": []}}).encode("utf-8")
                headers = {"Content-Type": "application/json"}
            else:
                body, headers = render(fmt, batch[0][1], env)
            answer = post(url, body, headers)
        except Exception as e:  # noqa: BLE001
            answer = (False, type(e).__name__)
        ok, why = answer[0], answer[1]
        wait = answer[2] if len(answer) > 2 else None
        if wait is not None:
            # Rate limited: this batch and every one after it wait.
            later = [row for b in batches[n:] for row, _ in b]
            with schema.connect(path) as c:
                c.executemany("UPDATE alarm_outbox SET next_at=?, last_error=? WHERE id=?",
                              [(alarms.iso(now + wait), why, r["id"]) for r in later])
            print(f"[alarms] rate limited; {len(later)} messages wait {wait:.1f} s")
            break
        for row, _ in batch:
            attempts = row["attempts"] + 1
            with schema.connect(path) as c:
                if ok:
                    c.execute("UPDATE alarm_outbox SET state='sent', attempts=?, done_at=?,"
                              " last_error=NULL WHERE id=?", (attempts, stamp, row["id"]))
                    sent.append(row["id"])
                elif attempts >= MAX_ATTEMPTS:
                    c.execute("UPDATE alarm_outbox SET state='failed', attempts=?, done_at=?,"
                              " last_error=? WHERE id=?", (attempts, stamp, why, row["id"]))
                    print(f"[alarms] gave up sending {row['event']} of {row['alarm_key']}"
                          f" after {attempts} attempts: {why}")
                else:
                    c.execute("UPDATE alarm_outbox SET attempts=?, next_at=?, last_error=?"
                              " WHERE id=?", (attempts, alarms.iso(now + backoff(attempts)),
                                              why, row["id"]))
                    print(f"[alarms] sending {row['event']} of {row['alarm_key']} failed"
                          f" ({why}); trying again in {backoff(attempts)} s")
    return sent
