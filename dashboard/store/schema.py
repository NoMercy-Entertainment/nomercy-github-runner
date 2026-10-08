"""The control plane's tables, and the connection that opens them.

Five tables, exactly as design sections 11.1 and 11.3 describe them:

  runner_specs - one row per runner instance: what it should be, what it is
  workers      - the machines instances are placed on
  fleets       - the six provider x platform cells, and how many each wants
  operations   - every requested change, with its outcome
  audit        - append-only: who asked for what, and what was decided

Written in the style of `history.py`: CREATE TABLE IF NOT EXISTS, WAL, foreign
keys on. The idiom matters more than it looks. A deployed database never gains
a column from the schema text alone, because CREATE TABLE IF NOT EXISTS does
nothing to a table that already exists - so a column added later needs an
explicit ALTER in `init()`, the way history.py adds `provider` and
`forge_task_id`. `_migrate()` below is where those go.

**The circular reference is deliberate.** `runner_specs.current_operation`
points at `operations`, and `operations.runner_id` points back. Both are
nullable, so either row can be written first and updated afterwards. Modelling
it any other way would mean an operation could not name its subject, or a
runner could not say what is happening to it.

**No column here holds a token.** A registration token exists for minutes and
is a credential; what stays is the forge's id for the runner, which is not one.
A test asserts no column is named after anything in `providers.REDACTED_FIELDS`.
"""
import os
import sqlite3
import threading

#: Its own file. See the package docstring for why this is not history.db.
DB_PATH = os.path.join(os.environ.get("DASH_DATA", "/data"), "control.db")

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
  host_id                 TEXT PRIMARY KEY,
  display_name            TEXT,
  -- hyperv-linux | hyperv-windows. A macOS appliance host is a Linux
  -- worker that declares it drives an appliance (placement.RUNTIME_KIND);
  -- a third kind here is where "uniform" would quietly stop being true.
  kind                    TEXT NOT NULL,
  endpoint                TEXT,
  agent_version           TEXT,
  capabilities            TEXT,
  last_seen_at            TEXT,
  state                   TEXT NOT NULL DEFAULT 'unknown',
  certificate_fingerprint TEXT,
  -- Why a worker was marked degraded, when it was marked rather than merely
  -- silent - a protocol version the controller does not speak (T-0406).
  -- Cleared by the next compatible heartbeat.
  state_reason            TEXT
);

CREATE TABLE IF NOT EXISTS fleets (
  fleet_id          TEXT PRIMARY KEY,
  provider          TEXT NOT NULL,
  platform          TEXT NOT NULL,
  architecture      TEXT NOT NULL,
  desired_capacity  INTEGER NOT NULL DEFAULT 0,
  labels            TEXT,
  runner_group      TEXT,
  template          TEXT,
  unit_template     TEXT,
  cpu_limit         TEXT,
  memory_limit      INTEGER,
  memory_swap_limit INTEGER,
  disk_limit        INTEGER,
  resource_defaults TEXT,
  cache_policy      TEXT,
  -- Whether the cell exists at all, answered from provider.supports(). A
  -- fleet nobody can build is visibly unavailable rather than silently empty.
  available         INTEGER NOT NULL DEFAULT 1,
  unavailable_reason TEXT,
  UNIQUE(provider, platform, architecture)
);

CREATE TABLE IF NOT EXISTS runner_specs (
  -- UUIDv4, generated here and never by a caller. The only identity: a name
  -- is presentation, and three different names for one runner is the problem
  -- this table exists to end.
  runner_id        TEXT PRIMARY KEY,
  display_name     TEXT,
  provider         TEXT NOT NULL,
  platform         TEXT NOT NULL,
  architecture     TEXT NOT NULL DEFAULT 'x64',
  runtime_template TEXT,
  host_id          TEXT REFERENCES workers(host_id),
  labels           TEXT,
  runner_group     TEXT,
  -- Adapter-interpreted: a cpuset width, a Job Object cap, or a vCPU count.
  -- TEXT because those are not the same unit and must not be flattened.
  cpu_limit        TEXT,
  memory_limit     INTEGER,
  memory_swap_limit INTEGER,
  disk_limit       INTEGER,
  cache_policy     TEXT,
  desired_state    TEXT NOT NULL DEFAULT 'running',
  actual_state     TEXT NOT NULL DEFAULT 'unknown',
  registration_id  TEXT,
  registration_uuid TEXT,
  created_at       TEXT NOT NULL,
  last_seen_at     TEXT,
  current_operation TEXT REFERENCES operations(operation_id),
  last_error       TEXT,
  capabilities     TEXT,
  -- Opaque to the controller. A container name today, something else later.
  exec_unit_ref    TEXT,
  fleet_id         TEXT REFERENCES fleets(fleet_id),
  spec_version     INTEGER NOT NULL DEFAULT 1,
  -- What the worker last said about the execution unit: running, stopped,
  -- absent or unknown. Written by heartbeats, never by the reconciler, and
  -- outside spec_version - an observation arriving every ten seconds must not
  -- turn every change a person makes into a stale-version conflict.
  unit_state       TEXT,
  -- T-1803, observations of the same kind: what the unit last used (JSON,
  -- from heartbeats), what the forge last said of the runner, and when it
  -- last said anything (from the reconciler's observation).
  telemetry        TEXT,
  forge_state      TEXT,
  forge_seen_at    TEXT,
  forge_labels     TEXT,
  forge_labels_at  TEXT,
  -- Something true and worth saying that is not a failure: a runner that
  -- registered with labels other than its fleet's still works, it just
  -- takes different jobs. It was written into last_error before, where the
  -- page paints it red and every healthy runner looked broken.
  last_note        TEXT,
  -- T-0802: what this runner was adopted from, when it was already serving
  -- before the controller knew it - the execution unit that exists, named in
  -- the worker's own terms (a launchd label and the directory it runs from).
  -- Null for every runner this controller made, which is nearly all of them.
  adopt_unit       TEXT,
  -- GitHub #5: an admin's own CPU and memory for this one runner. They
  -- outrank the fleet's setting and the deployment's, and survive every
  -- recreate; cpu_limit and memory_limit stay the values actually resolved
  -- (on a pinned platform cpu_limit is the cpuset cut for this width).
  cpu_override     TEXT,
  memory_override  INTEGER,
  -- Soft delete. History keeps a referent, so a run from a runner that no
  -- longer exists still resolves to something that says what it was.
  deleted_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_specs_fleet ON runner_specs(fleet_id);
CREATE INDEX IF NOT EXISTS idx_specs_host  ON runner_specs(host_id);
CREATE INDEX IF NOT EXISTS idx_specs_live  ON runner_specs(deleted_at)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS operations (
  operation_id    TEXT PRIMARY KEY,
  -- UNIQUE is the whole mechanism: a retried request carrying the same key
  -- finds the first operation instead of starting a second one.
  idempotency_key TEXT UNIQUE,
  runner_id       TEXT REFERENCES runner_specs(runner_id),
  fleet_id        TEXT REFERENCES fleets(fleet_id),
  verb            TEXT NOT NULL,
  requested_by    TEXT,
  requested_at    TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  deadline_at     TEXT,
  result          TEXT,
  error           TEXT,
  trace           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_runner ON operations(runner_id);
CREATE INDEX IF NOT EXISTS idx_ops_state  ON operations(state, requested_at);

-- One row per exclusive role, held by one process until it expires. The
-- reconciler takes one per pass so that two passes can never both decide a
-- fleet is short and both plan the missing runner - which is the overshoot
-- the design forbids. A new table, so CREATE TABLE IF NOT EXISTS is enough for
-- an existing database; only new columns on old tables need an ALTER.
CREATE TABLE IF NOT EXISTS leases (
  name       TEXT PRIMARY KEY,
  holder     TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controller_status (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  last_seen_at TEXT NOT NULL,
  state        TEXT NOT NULL,
  last_error   TEXT
);
CREATE TABLE IF NOT EXISTS platform_settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT NOT NULL,
  actor        TEXT,
  verb         TEXT NOT NULL,
  runner_id    TEXT,
  operation_id TEXT,
  decision     TEXT,
  -- Redacted before it gets here. Never the raw request.
  parameters   TEXT,
  -- Design 18.4's two further columns (T-1802): the fleet a request was
  -- about, and how it ended - or why it was refused.
  fleet_id     TEXT,
  outcome      TEXT
);

-- T-1901: the forge tokens, sealed. Set and cleared, never read back through
-- any route; `fingerprint` tells two apart without revealing either.
CREATE TABLE IF NOT EXISTS secrets (
  name        TEXT PRIMARY KEY,
  sealed      BLOB NOT NULL,
  fingerprint TEXT NOT NULL,
  set_at      TEXT NOT NULL,
  set_by      TEXT
);

-- GitHub #11: what the alarm monitor is watching. One row per condition
-- that is true now - a runner offline, a job waiting that no online runner
-- can take, the monitor itself unable to read a forge - with when it was
-- first seen, which is what survives a restart. `raised_at` stays null
-- until the condition has lasted its threshold; the row is deleted when it
-- ends, and a raised one is recorded in `audit` both times.
CREATE TABLE IF NOT EXISTS alarm_watch (
  alarm_key  TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  forge      TEXT NOT NULL,
  subject    TEXT NOT NULL,
  detail     TEXT,
  since      TEXT NOT NULL,
  raised_at  TEXT
);
-- Every label a self-hosted runner of a forge has ever carried. A job that
-- asks for a label none ever had is a hosted runner's job, not ours.
CREATE TABLE IF NOT EXISTS alarm_labels (
  forge         TEXT NOT NULL,
  label         TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  PRIMARY KEY (forge, label)
);

-- Each raise and resolve to be sent to ALARM_WEBHOOK_URL, once: the UNIQUE
-- is the dedup, and a send that fails waits for `next_at` to try again.
-- No column holds the URL.
CREATE TABLE IF NOT EXISTS alarm_outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  alarm_key  TEXT NOT NULL,
  event      TEXT NOT NULL,
  raised_at  TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  next_at    TEXT NOT NULL,
  -- pending | sent | failed (gave up) | skipped (no webhook was set)
  state      TEXT NOT NULL DEFAULT 'pending',
  last_error TEXT,
  done_at    TEXT,
  UNIQUE (alarm_key, event, raised_at)
);

-- NFR-7: append-only, by the database rather than by convention. An
-- operation's outcome is a row of its own, never an update to the row that
-- accepted it.
CREATE TRIGGER IF NOT EXISTS audit_is_append_only_update
BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_is_append_only_delete
BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at DESC);
"""


def connect(path=None):
    """A connection with the pragmas this database depends on.

    `foreign_keys=ON` is per connection, not per database: SQLite defaults it
    OFF for backwards compatibility, so a connection that forgets it silently
    accepts a spec pointing at a fleet that does not exist.
    """
    c = sqlite3.connect(path or DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _migrate(c):
    """Columns added after a database was first created.

    Each goes here as well as into SCHEMA: CREATE TABLE IF NOT EXISTS does
    nothing to a table that already exists, so a deployed database only ever
    gains a column through this.
    """
    fleets = {r[1] for r in c.execute("PRAGMA table_info(fleets)")}
    for column, kind in (("unit_template", "TEXT"), ("cpu_limit", "TEXT"),
                         ("memory_limit", "INTEGER"), ("memory_swap_limit", "INTEGER"),
                         ("disk_limit", "INTEGER")):
        if column not in fleets:
            c.execute(f"ALTER TABLE fleets ADD COLUMN {column} {kind}")
    workers = {r[1] for r in c.execute("PRAGMA table_info(workers)")}
    if "state_reason" not in workers:
        # T-0406. Nullable: a worker nobody has marked has no reason.
        c.execute("ALTER TABLE workers ADD COLUMN state_reason TEXT")

    audit = {r[1] for r in c.execute("PRAGMA table_info(audit)")}
    for column in ("fleet_id", "outcome"):
        if column not in audit:
            # T-1802, design 18.4. Nullable: rows written before had none.
            c.execute(f"ALTER TABLE audit ADD COLUMN {column} TEXT")

    have = {r[1] for r in c.execute("PRAGMA table_info(runner_specs)")}
    if "memory_swap_limit" not in have:
        c.execute("ALTER TABLE runner_specs ADD COLUMN memory_swap_limit INTEGER")
    if "unit_state" not in have:
        # T-0404. Nullable: a runner no heartbeat has mentioned yet has no
        # observed unit state, which is not the same as any value.
        c.execute("ALTER TABLE runner_specs ADD COLUMN unit_state TEXT")
    if "last_note" not in have:
        # A note is not an error, and had been living in the error's column.
        c.execute("ALTER TABLE runner_specs ADD COLUMN last_note TEXT")
    if "adopt_unit" not in have:
        # T-0802. Null for a runner the controller made itself; JSON naming
        # the unit that was already there for one it adopted.
        c.execute("ALTER TABLE runner_specs ADD COLUMN adopt_unit TEXT")
    for column in ("telemetry", "forge_state", "forge_seen_at", "forge_labels",
                   "forge_labels_at"):
        # T-1803. Observations, like unit_state: what the unit last used,
        # what the forge last said of the runner, and when it last said
        # anything. Null until observed, which is not any value.
        if column not in have:
            c.execute(f"ALTER TABLE runner_specs ADD COLUMN {column} TEXT")
    for column, kind in (("cpu_override", "TEXT"), ("memory_override", "INTEGER")):
        # GitHub #5. Nullable: a runner nobody overrode takes its fleet's.
        if column not in have:
            c.execute(f"ALTER TABLE runner_specs ADD COLUMN {column} {kind}")


def init(path=None):
    with _lock, connect(path) as c:
        c.executescript(SCHEMA)
        _migrate(c)
