"""Registration health stays observable while a lifecycle step is waiting.

This replaces the controller's routine per-pass forge reads. It observes
registration state and labels only: job results and lifecycle actions retain
their separate authority. Cached replies keep their original capture time.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import time

import providers
from store import schema

from .forges import LiveForges
from . import retry

MAX_AGE = 120


def _now():
    return datetime.now(timezone.utc)


def event_marker(spec):
    return ((spec.get("telemetry") or {}).get("forge_job_event") or {}).get("event_id")


class ForgeHealth:
    def __init__(self, service, get_env, interval=15, forges=LiveForges,
                 clock=_now, monotonic=time.monotonic):
        self.service, self.get_env = service, get_env
        self.interval, self.forges = interval, forges
        self.clock, self.monotonic = clock, monotonic
        self._lock = threading.Lock()
        self._reads = {provider.key: threading.Lock() for provider in providers.ALL}
        self._snapshots = {}
        self.active = False

    def snapshot(self, provider, spec=None):
        with self._lock:
            item = self._snapshots.get(provider.key)
            if item is None:
                return None, None
            records, captured, attempted, epochs = item
            age = (self.clock() - captured).total_seconds()
            stamp = captured.strftime("%Y-%m-%dT%H:%M:%SZ")
            if spec is not None and (epochs.get(spec["runner_id"]) !=
                    (spec.get("registration_id"), spec.get("registration_uuid"), event_marker(spec))
                    or (spec.get("forge_seen_at") or "") > stamp):
                return None, None
            return (deepcopy(records) if 0 <= age <= MAX_AGE else None), stamp

    def read(self, provider, *, due=False, stopped=None, spec=None):
        """An explicit fresh read, or the one scheduled routine read.

        A fresh lifecycle read and a scheduled read cannot overlap for a
        provider. A lifecycle read also satisfies that cadence's routine read.
        """
        with self._reads[provider.key]:
            if stopped is not None and stopped.is_set():
                return
            if due:
                with self._lock:
                    item = self._snapshots.get(provider.key)
                    if item and self.monotonic() - item[2] < self.interval:
                        return
            specs = [s for s in self.service.specs.list()
                     if s.get("provider") == provider.key and s.get("actual_state") != "absent"
                     and (s.get("registration_id") or s.get("registration_uuid"))]
            captured, attempted = self.clock(), self.monotonic()
            try:
                transport = self.forges(self.get_env())
                records = retry.call(retry.FORGE_STATUS, transport.records, provider)
            except Exception:
                records = None
            with self._lock:
                epochs = {s["runner_id"]: (s.get("registration_id"), s.get("registration_uuid"), event_marker(s))
                          for s in specs}
                self._snapshots[provider.key] = (deepcopy(records), captured, attempted, epochs)
            self._record(provider, specs, records, captured)
            return self.snapshot(provider, spec=spec)

    def _record(self, provider, specs, records, captured):
        stamp = captured.strftime("%Y-%m-%dT%H:%M:%SZ")
        with schema.connect(self.service.specs.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for spec in specs:
                current = connection.execute("SELECT telemetry FROM runner_specs WHERE runner_id=?",
                                             (spec["runner_id"],)).fetchone()
                try:
                    new_event = event_marker({"telemetry": json.loads(current[0] or "{}")}) if current else None
                except (ValueError, TypeError):
                    continue
                word = provider.job_state(spec, records)
                known = word in (providers.IDLE, providers.BUSY, providers.OFFLINE)
                identity = (spec["runner_id"], provider.key,
                            spec.get("registration_id"), spec.get("registration_uuid"))
                fence = (" WHERE runner_id=? AND provider=? AND registration_id IS ?"
                         " AND registration_uuid IS ? AND actual_state!='absent'")
                # A reply begun before a signed event or a replacement cannot
                # overwrite that newer evidence or the new registration.
                if new_event != event_marker(spec):
                    # Signed events can arrive within the same timestamp
                    # second. Their durable identity also fences old reads.
                    continue
                if known:
                    connection.execute("UPDATE runner_specs SET forge_state=?,forge_seen_at=?" + fence
                                       + " AND (forge_seen_at IS NULL OR forge_seen_at<=?)",
                                       (word, stamp, *identity, stamp))
                else:
                    connection.execute("UPDATE runner_specs SET forge_state='unknown'" + fence
                                       + " AND (forge_seen_at IS NULL OR forge_seen_at<=?)",
                                       (*identity, stamp))
                names = provider.record_labels(provider.record_for(spec, records)) if records is not None else None
                if names is not None:
                    connection.execute("UPDATE runner_specs SET forge_labels=?,forge_labels_at=?" + fence
                                       + " AND (forge_labels_at IS NULL OR forge_labels_at<=?)",
                                       (json.dumps(names), stamp, *identity, stamp))

    def run(self, stopped, log=print):
        self.active = True
        try:
            while not stopped.is_set():
                started = self.monotonic()
                try:
                    present = {s.get("provider") for s in self.service.specs.list()
                               if s.get("actual_state") != "absent"}
                except Exception:
                    present = set()
                    log("controller: registration health inventory unavailable")
                for provider in providers.ALL:
                    if stopped.is_set():
                        break
                    if provider.key in present:
                        try:
                            self.read(provider, due=True, stopped=stopped)
                        except Exception:
                            # Error details may contain a forge credential.
                            log("controller: registration health observation unavailable")
                stopped.wait(max(0.0, self.interval - (self.monotonic() - started)))
        finally:
            self.active = False
