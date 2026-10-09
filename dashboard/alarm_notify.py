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


def enqueue(path, event, now=None):
    """One outbox row for a raise or a resolve; a second is ignored. An
    acknowledgement itself is not sent; the resolve that follows one is, since
    "it is back" is still worth hearing (the owner's choice, 2026-10-08)."""
    if event.get("event") not in ("raised", "resolved"):
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
    lowered = (url or "").lower()
    if "discord.com/api/webhooks" in lowered or "discordapp.com/api/webhooks" in lowered:
        return "discord"
    if "hooks.slack.com" in lowered:
        return "slack"
    host = lowered.split("://", 1)[-1].split("/", 1)[0]
    if "ntfy" in host:
        return "ntfy"
    return "json"


def text_of(event):
    word = "ALARM" if event["event"] == "raised" else "RESOLVED"
    text = f"{word}: {event['message']}"
    url = (event.get("detail") or {}).get("url")
    return f"{text}\n{url}" if url else text


def _header(value):
    """A header value http.client can send: latin-1, one line."""
    return " ".join(str(value).split()).encode("latin-1", "replace").decode("latin-1")


#: Embed colours: an alarm, its resolve, and the monitor itself.
RED, GREEN, AMBER = 0xE5484D, 0x30A46C, 0xF5A524

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
    since_name = {"runner_offline": "Offline since", "job_queued": "Queued since"}.get(kind, "Since")
    fields.append({"name": since_name, "value": alarms.local_time(event.get("since")), "inline": False})
    if not raised:
        start, end = alarms.epoch(event.get("since")), alarms.epoch(event.get("resolved_at"))
        if start is not None and end is not None:
            fields.append({"name": "Down for" if kind == "runner_offline" else "Lasted",
                           "value": alarms.duration(end - start), "inline": True})
        if event.get("resolved_at"):
            fields.append({"name": "Back since", "value": alarms.local_time(event["resolved_at"]),
                           "inline": True})
    title = f"{_TITLES.get((kind, raised), 'Alarm' if raised else 'Resolved')}: {event.get('subject') or forge}"
    embed = {"title": _cut(title, 256),
             "description": _cut(event.get("message"), 4096),
             "color": (AMBER if kind == "monitor" else RED) if raised else GREEN,
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
    if fmt == "ntfy":
        headers = {"Content-Type": "text/plain; charset=utf-8",
                   "Title": _header(("Runner alarm: " if raised else "Runner alarm resolved: ")
                                    + str(event.get("subject") or "")),
                   "Priority": "urgent" if raised else "default",
                   "Tags": "rotating_light" if raised else "white_check_mark"}
        url = (event.get("detail") or {}).get("url")
        if url:
            headers["Click"] = _header(url)
        return text.encode("utf-8"), headers
    if fmt == "discord":
        # No mentions: a job or runner name is not ours to choose, and
        # "@everyone" in one must not page a whole server.
        # Only the owners' own ids may ping, and only on the red message;
        # a name in a runner or job ("@everyone") stays text.
        body = {"embeds": [discord_embed(event)], "allowed_mentions": {"parse": []}}
        users = owners_of(event, env) if raised else []
        if users:
            body["content"] = " ".join(f"<@{u}>" for u in users)
            body["allowed_mentions"]["users"] = users
    elif fmt == "slack":
        # Slack's own escaping: with &, < and > as entities, no <!channel>,
        # <!here> or <@user> in a job or runner name can mention anyone.
        body = {"text": text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;")}
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
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


#: How often the delivery thread looks for what is due.
DELIVERY_SECONDS = 15


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


def deliver_due(path, env, now, post=None):
    """Send every outbox row that is due. Called by the delivery thread."""
    post = post or _post
    url = str((env or {}).get("ALARM_WEBHOOK_URL") or "").strip()
    stamp = alarms.iso(now)
    with schema.connect(path) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM alarm_outbox WHERE state='pending' AND next_at<=?"
            " ORDER BY id LIMIT 50", (stamp,))]
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
    sent = []
    for row in rows:
        event = json.loads(row["payload"])
        try:
            body, headers = render(fmt, event, env)
            ok, why = post(url, body, headers)
        except Exception as e:  # noqa: BLE001
            ok, why = False, type(e).__name__
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
