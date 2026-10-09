"""Who wrote the code a job is about to run: one table of events and the
answer each runner's job-started hook must give, shared by the tests of all
three (Linux `runner_guard.py`, the Windows copy of it, macOS `lib.sh`).

The rule: a pull request whose head is a fork - another repository, or one
deleted since - runs only when its author is trusted: `author_association`
OWNER or MEMBER, or a login in RUNNER_TRUSTED_AUTHORS. Not COLLABORATOR: the
owner wants nothing from outside the org. Every
other event runs. An event the hook cannot read lets the job run, with a
warning: a fault of ours never stops every job.

Each case: `event` (GITHUB_EVENT_NAME, None for unset), `payload` (an object
written as the event file, a str written as it is, None for no file),
`env` (anything else the hook is given), `allowed`, and the lines `out` must
contain and `absent` must not.
"""
import copy

ORG = "NoMercy-Entertainment"
REFUSED = "::error title=Outside code refused::"
UNREAD = "::warning title=Runner guard::could not read the event; origin not checked"


def pull_request(author="mallory", association="NONE", head="mallory/app",
                 base=f"{ORG}/app", number=7, deleted=False, owner=None,
                 action="opened", sender=None):
    head_repo = None if deleted else {
        "full_name": head, "fork": True,
        "owner": {"login": owner or head.split("/")[0]}}
    return {"action": action, "number": number,
            "pull_request": {"number": number, "user": {"login": author},
                             "author_association": association,
                             "title": "Fix ::error title=x::spoofed\n::warning",
                             "body": '"full_name": "NoMercy-Entertainment/app"',
                             "head": {"ref": "main", "repo": head_repo},
                             "base": {"ref": "main", "repo": {
                                 "full_name": base,
                                 "owner": {"login": base.split("/")[0]}}}},
            "repository": {"full_name": base},
            "sender": {"login": sender or author}}


def _without_owner(payload):
    payload = copy.deepcopy(payload)
    del payload["pull_request"]["head"]["repo"]["owner"]
    del payload["pull_request"]["base"]["repo"]["owner"]
    return payload


def _without_pr_object(payload):
    payload = copy.deepcopy(payload)
    del payload["pull_request"]["head"]
    return payload


def _case(id, event, payload, allowed, out=(), absent=(), env=None):
    return {"id": id, "event": event, "payload": payload, "allowed": allowed,
            "out": list(out), "absent": list(absent), "env": dict(env or {})}


REFUSAL_TEXT = (REFUSED + f"Self-hosted runners only run code from {ORG} members and "
                "known maintainers. Pull request #7 by mallory (NONE) comes from fork "
                "mallory/app, so this job was stopped before any of its code ran. If "
                "mallory is a maintainer whose org membership is private, add them to "
                "RUNNER_TRUSTED_AUTHORS.")

CASES = [
    # ---- refused: a fork, and an author nobody vouched for -------------------
    _case("fork-by-outsider", "pull_request", pull_request(), False,
          out=[REFUSAL_TEXT], absent=["Origin:", "— allowed"]),
    _case("fork-by-private-member-reads-contributor", "pull_request",
          pull_request(association="CONTRIBUTOR"), False,
          out=[REFUSED, "by mallory (CONTRIBUTOR)"]),
    _case("fork-by-first-time-contributor", "pull_request",
          pull_request(association="FIRST_TIME_CONTRIBUTOR"), False, out=[REFUSED]),
    _case("fork-by-first-timer", "pull_request",
          pull_request(association="FIRST_TIMER"), False, out=[REFUSED]),
    _case("fork-pull-request-target", "pull_request_target", pull_request(), False,
          out=[REFUSED]),
    _case("fork-review", "pull_request_review", pull_request(), False, out=[REFUSED]),
    _case("fork-review-comment", "pull_request_review_comment", pull_request(), False,
          out=[REFUSED]),
    _case("comment-carrying-a-fork-pull-request", "issue_comment", pull_request(), False,
          out=[REFUSED]),
    _case("deleted-fork", "pull_request", pull_request(deleted=True), False,
          out=[REFUSED + f"Self-hosted runners only run code from {ORG} members and "
               "known maintainers. Pull request #7 by mallory (NONE) comes from a "
               "deleted fork, whose owner cannot be checked, so this job was stopped "
               "before any of its code ran."], absent=["RUNNER_TRUSTED_AUTHORS"]),
    _case("deleted-fork-even-by-a-member", "pull_request",
          pull_request(author="alice", association="MEMBER", deleted=True), False,
          out=[REFUSED, "by alice (MEMBER) comes from a deleted fork"]),
    _case("member-opens-it-from-an-outsiders-fork", "pull_request",
          pull_request(author="alice", association="MEMBER", head="mallory/app"), False,
          out=[REFUSED + f"Self-hosted runners only run code from {ORG} members and "
               "known maintainers. Pull request #7 by alice (MEMBER) comes from fork "
               "mallory/app, which belongs to mallory, so this job was stopped before any "
               "of its code ran. If mallory is a maintainer whose org membership is "
               "private, add them to RUNNER_TRUSTED_AUTHORS."]),
    _case("the-fork-owner-not-the-repository-name-decides", "pull_request",
          pull_request(author="alice", association="MEMBER", head="alice/app",
                       owner="mallory"), False,
          out=[REFUSED, "which belongs to mallory"]),
    _case("an-outsider-pushes-to-a-members-fork", "pull_request",
          pull_request(author="alice", association="MEMBER", head="alice/app",
                       action="synchronize", sender="eve"), False,
          out=[REFUSED + f"Self-hosted runners only run code from {ORG} members and "
               "known maintainers. Pull request #7 by alice (MEMBER) comes from fork "
               "alice/app, last pushed to by eve, so this job was stopped before any of "
               "its code ran. If eve is a maintainer whose org membership is private, "
               "add them to RUNNER_TRUSTED_AUTHORS."]),
    _case("an-allowlisted-author-and-an-outside-owner", "pull_request",
          pull_request(author="bob", head="mallory/app"), False,
          env={"RUNNER_TRUSTED_AUTHORS": "bob"}, out=[REFUSED, "which belongs to mallory"]),
    _case("allowlist-names-someone-else", "pull_request", pull_request(), False,
          env={"RUNNER_TRUSTED_AUTHORS": "alice,bob"}, out=[REFUSED]),
    _case("allowlist-is-not-a-substring-match", "pull_request", pull_request(), False,
          env={"RUNNER_TRUSTED_AUTHORS": "mallory2,mal"}, out=[REFUSED]),
    _case("no-event-name-but-a-fork-payload", None, pull_request(), False,
          out=[REFUSED]),
    _case("lookalike-owner-name", "pull_request",
          pull_request(head=f"{ORG}-x/app"), False, out=[REFUSED]),

    # ---- allowed: the org's own code, or a trusted author's fork -------------
    _case("same-repository-pull-request", "pull_request",
          pull_request(author="bob", association="MEMBER", head=f"{ORG}/app"), True,
          out=[f"Origin: pull_request from {ORG}/app by bob — allowed"],
          absent=["::error", "::warning"]),
    _case("same-repository-even-by-an-outsider-association", "pull_request",
          pull_request(head=f"{ORG}/app"), True,
          out=[f"Origin: pull_request from {ORG}/app by mallory — allowed"]),
    _case("same-repository-other-capitals", "pull_request",
          pull_request(head="nomercy-entertainment/APP"), True,
          out=["— allowed"], absent=["::error"]),
    _case("fork-by-member", "pull_request",
          pull_request(author="alice", association="MEMBER", head="alice/app"), True,
          out=["Origin: pull_request from fork alice/app by alice — allowed"],
          absent=["::error", "::warning"]),
    _case("fork-by-owner", "pull_request_target",
          pull_request(author="stoney", association="OWNER", head="stoney/app"), True,
          out=["Origin: pull_request_target from fork stoney/app by stoney — allowed"]),
    _case("fork-by-outside-collaborator", "pull_request",
          pull_request(author="carol", association="COLLABORATOR", head="carol/app"), False,
          out=[REFUSED, "by carol (COLLABORATOR)"], absent=["— allowed"]),
    _case("fork-by-allowlisted-author", "pull_request",
          pull_request(author="Mallory", association="CONTRIBUTOR"), True,
          env={"RUNNER_TRUSTED_AUTHORS": " alice , mallory ,"},
          out=["Origin: pull_request from fork mallory/app by Mallory — allowed"]),
    _case("allowlisted-bot-compared-as-it-is", "pull_request",
          pull_request(author="renovate[bot]", association="NONE", head="renovate/app",
                       owner="renovate[bot]"), True,
          env={"RUNNER_TRUSTED_AUTHORS": "renovate[bot]"},
          out=["Origin: pull_request from fork renovate/app by renovate?bot? — allowed"]),
    _case("a-name-made-safe-for-printing-is-not-trusted", "pull_request",
          pull_request(author="renovate[bot]", association="NONE", head="renovate/app",
                       owner="renovate[bot]"), False,
          env={"RUNNER_TRUSTED_AUTHORS": "renovate?bot?"}, out=[REFUSED]),
    _case("a-fork-the-org-owns", "pull_request",
          pull_request(head=f"{ORG}/app-fork"), True,
          out=[f"Origin: pull_request from fork {ORG}/app-fork by mallory — allowed"]),
    _case("a-member-pushes-to-their-own-fork", "pull_request",
          pull_request(author="alice", association="MEMBER", head="alice/app",
                       action="synchronize"), True, out=["— allowed"], absent=["::error"]),
    _case("an-allowlisted-maintainer-pushes-to-a-members-fork", "pull_request",
          pull_request(author="alice", association="MEMBER", head="alice/app",
                       action="synchronize", sender="Bob"), True,
          env={"RUNNER_TRUSTED_AUTHORS": "bob"}, out=["— allowed"]),
    _case("an-outsider-reviewing-a-members-pull-request-changes-nothing",
          "pull_request_review",
          pull_request(author="alice", association="MEMBER", head="alice/app",
                       action="submitted", sender="eve"), True, out=["— allowed"]),
    _case("the-owner-read-from-the-name-when-github-gives-none", "pull_request",
          _without_owner(pull_request(author="alice", association="MEMBER",
                                      head="alice/app")), True,
          out=["— allowed"]),
    _case("a-same-repository-push-by-anyone-with-write-access", "pull_request",
          pull_request(head=f"{ORG}/app", action="synchronize", sender="eve"), True,
          out=["— allowed"]),
    _case("push", "push",
          {"ref": "refs/heads/main", "repository": {"full_name": f"{ORG}/app"},
           "sender": {"login": "bob"}}, True,
          out=[f"Origin: push from {ORG}/app by bob — allowed"],
          absent=["::error", "::warning"]),
    _case("release", "release",
          {"action": "published", "repository": {"full_name": f"{ORG}/app"},
           "sender": {"login": "bob"}}, True,
          out=[f"Origin: release from {ORG}/app by bob — allowed"]),
    _case("workflow-dispatch", "workflow_dispatch",
          {"inputs": {}, "repository": {"full_name": f"{ORG}/app"},
           "sender": {"login": "bob"}}, True,
          out=[f"Origin: workflow_dispatch from {ORG}/app by bob — allowed"]),
    _case("schedule-names-the-repository-from-the-environment", "schedule",
          {"schedule": "0 4 * * *"}, True,
          env={"GITHUB_REPOSITORY": f"{ORG}/app", "GITHUB_ACTOR": "stoney"},
          out=[f"Origin: schedule from {ORG}/app by stoney — allowed"]),
    _case("comment-on-an-issue", "issue_comment",
          {"issue": {"number": 3, "pull_request": {"url": "https://api.github.com/x"}},
           "repository": {"full_name": f"{ORG}/app"}, "sender": {"login": "mallory"}},
          True, out=[f"Origin: issue_comment from {ORG}/app by mallory — allowed"]),
    _case("workflow-run-carries-no-pull-request-object", "workflow_run",
          {"workflow_run": {"head_repository": {"full_name": "mallory/app"},
                            "pull_requests": []},
           "repository": {"full_name": f"{ORG}/app"}, "sender": {"login": "mallory"}},
          True, out=[f"Origin: workflow_run from {ORG}/app by mallory — allowed"]),
    _case("a-repository-name-that-is-not-text-falls-back", "push",
          {"repository": {"full_name": 5}, "sender": {"login": ["x"]}}, True,
          env={"GITHUB_REPOSITORY": f"{ORG}/app", "GITHUB_ACTOR": "bob"},
          out=[f"Origin: push from {ORG}/app by bob — allowed"]),
    _case("a-null-pull-request-is-no-pull-request", "push",
          {"pull_request": None, "repository": {"full_name": f"{ORG}/app"},
           "sender": {"login": "bob"}}, True, out=["— allowed"]),

    # ---- unreadable: our fault, so the job runs and is told -----------------
    _case("no-event-file-named", "push", None, True, out=[UNREAD],
          absent=["::error"]),
    _case("garbage-json", "pull_request", "{not json at all", True, out=[UNREAD],
          absent=["::error"]),
    _case("json-that-is-not-an-object", "pull_request", "[1, 2, 3]", True,
          out=[UNREAD], absent=["::error"]),
    _case("a-pull-request-without-a-head", "pull_request",
          _without_pr_object(pull_request()), True, out=[UNREAD], absent=["::error"]),
]

#: The cases a hook is run against end to end: one of each answer.
END_TO_END = ("fork-by-outsider", "deleted-fork", "fork-by-allowlisted-author",
              "member-opens-it-from-an-outsiders-fork",
              "fork-by-member", "same-repository-pull-request", "push",
              "no-event-file-named", "garbage-json")


def by_id(name):
    return next(case for case in CASES if case["id"] == name)


def write_event(case, directory):
    """The event file for `case` in `directory`, or None when it has none."""
    import json
    from pathlib import Path
    payload = case["payload"]
    if payload is None:
        return None
    path = Path(directory) / "event.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def hook_env(case, directory, base=None):
    """What the runner gives a hook for `case`: the event's name and path,
    and the case's own variables, over `base` with any GITHUB_ or trust
    variable of the machine running the tests taken out."""
    env = {k: v for k, v in (base or {}).items()
           if not k.startswith("GITHUB_") and k != "RUNNER_TRUSTED_AUTHORS"}
    if case["event"] is not None:
        env["GITHUB_EVENT_NAME"] = case["event"]
    path = write_event(case, directory)
    if path is not None:
        env["GITHUB_EVENT_PATH"] = str(path)
    env.update(case["env"])
    return env
