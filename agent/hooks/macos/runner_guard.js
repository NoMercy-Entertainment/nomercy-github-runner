// Whose code is this job about to run? The macOS job-started hook's first
// check, run by lib.sh under the runner's own node, because bash 3.2 cannot
// read JSON and a pattern over the payload could be fooled by a pull
// request's own title or body.
//
// The rule and every line it prints are runner_guard.py's
// (images/linux/unit/runner/runner_guard.py), and agent/tests/origin_cases.py
// holds the two to the same answers: a pull request runs only when its head
// repository is owned by the org or by an account in RUNNER_TRUSTED_OWNERS.
// Values from the payload are compared as they are and only made safe for
// printing.
//
// Exit status: 0 to let the job run, 75 to refuse it. A fault in here is a
// ::warning and 0: a bug of ours never stops every job. Node's own modules
// only, and ASCII only.
"use strict";

const REFUSE = 75;

// What a runner reports about its hook (agent/origin_guard.py); the same
// number as runner_guard.py's.
const GUARD_VERSION = 1;

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

// RUNNER_TRUSTED_OWNERS: account names separated by commas or spaces,
// compared without case.
function trustedOwners(list) {
  return new Set(String(list || "").split(/[,\s]+/).map((n) => n.toLowerCase()).filter(Boolean));
}

function ownerOf(name) {
  return name && name.includes("/") ? name.split("/")[0] : null;
}

// Where the code of a pull request comes from. kind: "none" (no pull
// request), "head" (its head repository is known: head, owner), "deleted"
// (its head repository is gone) or "unknown" (the payload does not say).
// baseOwner is who owns the repository the pull request is for.
function origin(payload) {
  const facts = { kind: "none", head: null, owner: null, baseOwner: null, login: null };
  const pr = payload.pull_request;
  if (!isObject(pr)) return facts;
  const base = text(get(pr, "base", "repo", "full_name")) || text(get(payload, "repository", "full_name"));
  facts.baseOwner = text(get(pr, "base", "repo", "owner", "login")) || ownerOf(base)
    || text(get(payload, "repository", "owner", "login"));
  facts.login = text(get(pr, "user", "login")) || text(get(payload, "sender", "login"));
  const head = pr.head;
  if (!isObject(head) || !Object.prototype.hasOwnProperty.call(head, "repo")) {
    facts.kind = "unknown";
  } else if (head.repo === null) {
    facts.kind = "deleted";
  } else if (!isObject(head.repo)) {
    facts.kind = "unknown";
  } else {
    const name = text(get(head, "repo", "full_name"));
    const owner = text(get(head, "repo", "owner", "login")) || ownerOf(name);
    if (owner) {
      facts.kind = "head";
      facts.head = name;
      facts.owner = owner;
    } else {
      facts.kind = "unknown";
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
  const baseOwner = (facts.baseOwner || "").toLowerCase();
  let where;
  if (facts.kind === "head") {
    const owner = facts.owner.toLowerCase();
    if (owner === baseOwner || trustedOwners(env.RUNNER_TRUSTED_OWNERS).has(owner)) {
      const from = shown(facts.head || facts.owner, "an unknown repository");
      const who = shown(facts.login, "an unknown account");
      return [true, `Origin: ${name} from ${from} by ${who} ${ALLOWED}`];
    }
    if (!baseOwner) return [true, UNREAD];
    where = shown(facts.head, shown(facts.owner, "?") + "/?");
  } else {
    where = "a repository that has been deleted";
  }
  const org = shown(facts.baseOwner, "the organisation");
  return [false, "::error title=Outside code refused::This pull request comes from "
    + `${where}, and these self-hosted runners only run code from repositories `
    + `owned by ${org} or a trusted owner. Push the branch to the ${org} `
    + "repository instead."];
}

// Where each run leaves its answer, in the runner's log directory, for the
// agent (agent/origin_guard.py): allowed, refused, unread or failed. Never
// throws.
const LAST_RESULT = "origin-guard.json";

function record(env, result) {
  try {
    const fs = require("fs");
    const path = require("path");
    const logs = env.RUNNER_LOG_DIR;
    if (!logs || !fs.statSync(logs).isDirectory()) return;
    const file = path.join(logs, LAST_RESULT);
    const at = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
    fs.writeFileSync(file + ".new", JSON.stringify({ version: GUARD_VERSION, result, at }) + "\n");
    fs.renameSync(file + ".new", file);
  } catch (error) {
    // A record that cannot be written changes no answer.
  }
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
      record(env, "unread");
      return 0;
    }
    const [allowed, line] = decide(env.GITHUB_EVENT_NAME, payload, env);
    process.stdout.write(line + "\n");
    record(env, !allowed ? "refused" : line === UNREAD ? "unread" : "allowed");
    return allowed ? 0 : REFUSE;
  } catch (error) {
    try {
      process.stdout.write("::warning title=Runner guard::the origin check failed ("
        + shown(error && error.name, "error") + "); origin not checked\n");
    } catch (ignored) {
      // The job still runs.
    }
    record(env, "failed");
    return 0;
  }
}

module.exports = { GUARD_VERSION, LAST_RESULT, REFUSE, check, decide, origin, record, shown };

if (require.main === module) {
  process.exitCode = check(process.env);
}
