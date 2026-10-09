// Whose code is this job about to run? The macOS job-started hook's first
// check, run by lib.sh under the runner's own node, because bash 3.2 cannot
// read JSON and a pattern over the payload could be fooled by a pull
// request's own title or body.
//
// The rule and every line it prints are runner_guard.py's
// (images/linux/unit/runner/runner_guard.py), and agent/tests/origin_cases.py
// holds the two to the same answers. Values from the payload are compared as
// they are and only made safe for printing.
//
// Exit status: 0 to let the job run, 75 to refuse it. A fault in here is a
// ::warning and 0: a bug of ours never stops every job. Node's own modules
// only, and ASCII only.
"use strict";

const REFUSE = 75;

// What a runner reports about its hook (agent/origin_guard.py); the same
// number as runner_guard.py's.
const GUARD_VERSION = 1;

const TRUSTED_ASSOCIATIONS = new Set(["OWNER", "MEMBER"]);
const ALLOWED = "\u2014 allowed";
const UNREAD = "::warning title=Runner guard::could not read the event; origin not checked";

function isObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function get(v, ...keys) {
  for (const k of keys) {
    if (!isObject(v)) return undefined;
    v = Object.prototype.hasOwnProperty.call(v, k) ? v[k] : undefined;
  }
  return v;
}

function text(v) {
  return typeof v === "string" && v !== "" ? v : null;
}

// Only what GitHub allows in a login or a repository name, so nothing from
// the payload can end the line or start a workflow command of its own.
function shown(v, fallback) {
  const t = text(v);
  return t === null ? fallback : t.replace(/[^A-Za-z0-9._\/-]/gu, "?").slice(0, 100);
}

function trustedAuthors(list) {
  return new Set(String(list || "").split(",").map((n) => n.trim().toLowerCase()).filter(Boolean));
}

function origin(payload) {
  const facts = { kind: "none", base: null, head: null, login: null, association: null, number: null,
    org: null, owner: null, sender: text(get(payload, "sender", "login")), action: text(payload.action) };
  const pr = payload.pull_request;
  if (!isObject(pr)) return facts;
  const association = pr.author_association;
  const number = pr.number;
  facts.base = text(get(pr, "base", "repo", "full_name")) || text(get(payload, "repository", "full_name"));
  facts.login = text(get(pr, "user", "login"));
  facts.association = typeof association === "string" ? association.toUpperCase() : null;
  facts.number = Number.isInteger(number) && number > 0 ? number : null;
  facts.org = text(get(pr, "base", "repo", "owner", "login")) || (facts.base || "").split("/")[0] || null;
  const head = pr.head;
  if (!isObject(head) || !Object.prototype.hasOwnProperty.call(head, "repo")) {
    facts.kind = "unknown";
  } else if (head.repo === null) {
    facts.kind = "deleted";
  } else {
    const name = text(get(head, "repo", "full_name"));
    if (!name || !facts.base) {
      facts.kind = "unknown";
    } else {
      facts.head = name;
      facts.owner = text(get(head, "repo", "owner", "login")) || name.split("/")[0] || null;
      facts.kind = name.toLowerCase() === facts.base.toLowerCase() ? "same" : "fork";
    }
  }
  return facts;
}

// [allowed, the line to print]
function decide(event, payload, env) {
  const name = shown(event, "an unnamed event");
  const facts = origin(payload);
  if (facts.kind === "unknown") return [true, UNREAD];
  if (facts.kind === "none") {
    const repo = shown(text(get(payload, "repository", "full_name")) || env.GITHUB_REPOSITORY,
      "an unknown repository");
    const who = shown(text(get(payload, "sender", "login")) || env.GITHUB_ACTOR, "an unknown account");
    return [true, `Origin: ${name} from ${repo} by ${who} ${ALLOWED}`];
  }
  const who = shown(facts.login, "an unknown account");
  const where = { same: shown(facts.base, "an unknown repository"),
    fork: "fork " + shown(facts.head, "an unknown repository"),
    deleted: "a deleted fork" }[facts.kind];
  const allowed = [true, `Origin: ${name} from ${where} by ${who} ${ALLOWED}`];
  if (facts.kind === "same") return allowed;
  if (facts.kind === "fork" && facts.org && facts.owner
      && facts.owner.toLowerCase() === facts.org.toLowerCase()) return allowed;
  const listed = trustedAuthors(env.RUNNER_TRUSTED_AUTHORS);
  const author = (facts.login || "").toLowerCase();
  const trusted = (login) => {
    const l = (login || "").toLowerCase();
    return Boolean(l) && (listed.has(l) || (l === author && TRUSTED_ASSOCIATIONS.has(facts.association)));
  };
  // Who is not trusted, and how to say it: the author, the fork's owner, the
  // last pusher - or nobody can tell, for a deleted fork.
  let untrusted = null;
  let how = "";
  if (facts.kind === "deleted") {
    how = ", whose owner cannot be checked";
  } else if (!trusted(facts.login)) {
    untrusted = facts.login;
  } else if (!trusted(facts.owner)) {
    untrusted = facts.owner;
    how = `, which belongs to ${shown(untrusted, "an unknown account")}`;
  } else if (facts.action === "synchronize" && !trusted(facts.sender)) {
    untrusted = facts.sender;
    how = `, last pushed to by ${shown(untrusted, "an unknown account")}`;
  } else {
    return allowed;
  }
  const org = shown(facts.org, "the organisation");
  const pr = facts.number ? `Pull request #${facts.number}` : "The pull request";
  const association = shown(facts.association, "UNKNOWN");
  let line = "::error title=Outside code refused::Self-hosted runners only run code "
    + `from ${org} members and known maintainers. ${pr} by ${who} (${association}) `
    + `comes from ${where}${how}, so this job was stopped before any of its code ran.`;
  if (facts.kind !== "deleted") {
    const named = shown(untrusted, "an unknown account");
    line += ` If ${named} is a maintainer whose org membership is private, add them `
      + "to RUNNER_TRUSTED_AUTHORS.";
  }
  return [false, line];
}

function check(env) {
  try {
    let payload = null;
    try {
      payload = JSON.parse(require("fs").readFileSync(env.GITHUB_EVENT_PATH, "utf8"));
    } catch (error) {
      payload = null;
    }
    if (!isObject(payload)) {
      process.stdout.write(UNREAD + "\n");
      return 0;
    }
    const [allowed, line] = decide(env.GITHUB_EVENT_NAME, payload, env);
    process.stdout.write(line + "\n");
    return allowed ? 0 : REFUSE;
  } catch (error) {
    try {
      process.stdout.write("::warning title=Runner guard::the origin check failed ("
        + shown(error && error.name, "error") + "); origin not checked\n");
    } catch (ignored) {
      // The job still runs.
    }
    return 0;
  }
}

module.exports = { GUARD_VERSION, REFUSE, check, decide, origin, shown };

if (require.main === module) {
  process.exitCode = check(process.env);
}
