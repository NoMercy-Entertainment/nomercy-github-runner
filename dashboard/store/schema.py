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
  -- hyperv-linux | hyperv-windows | macos-appliance-host
  kind                    TEXT NOT NULL,
  endpoint                TEXT,
  agent_version           TEXT,
  capabilities            TEXT,
  last_seen_at            TEXT,
  state                   TEXT NOT NULL DEFAULT 'unknown',
  certificate_fingerprint TEXT
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

CREATE TABLE IF NOT EXISTS audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT NOT NULL,
  actor        TEXT,
  verb         TEXT NOT NULL,
  runner_id    TEXT,
  operation_id TEXT,
  decision     TEXT,
  -- Redacted before it gets here. Never the raw request.
  parameters   TEXT
);
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

    Empty today, and that is the point of having it: the next column goes here
    rather than only into SCHEMA, where a deployed database would never see it.
    """
    return


def init(path=None):
    with _lock, connect(path) as c:
        c.executescript(SCHEMA)
        _migrate(c)
