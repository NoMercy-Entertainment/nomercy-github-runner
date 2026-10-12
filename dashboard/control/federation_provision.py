"""Locally bounded lending using standalone specs and requester credentials.

Only the receiver's already-configured GitHub organisation is supported: its
own observer/deletion credential can safely confirm ownership there. Foreign
organisations are refused rather than borrowing a requester's PAT. Existing
fleet rows, counts and normal runner groups are never changed.
"""
import json
import time

import providers
from store import schema
from . import cpusets, placement
from .federation import AllocationPending, PROFILE, Refused
from .secrets import SecretStore

TABLES = """
CREATE TABLE IF NOT EXISTS federation_allocations (
 request_id TEXT NOT NULL REFERENCES federation_requests(id),
 runner_id TEXT PRIMARY KEY REFERENCES runner_specs(runner_id),
 slot INTEGER NOT NULL, organization TEXT NOT NULL, runner_group TEXT NOT NULL,
 sealed_registration BLOB, credential_expires_at INTEGER NOT NULL,
 UNIQUE(request_id,slot)
);
"""


class LocalProvisioner:
    def __init__(self, service, client=None):
        self.service, self.path = service, service.specs.path
        self._client = client
        service.federation_provisioner = self
        with schema.connect(self.path) as c:
            c.executescript(TABLES)

    @property
    def client(self):
        return self._client or providers.by_key("github").forge_client(self.service.env)

    def enabled(self):
        with schema.connect(self.path) as c:
            row = c.execute("SELECT value FROM platform_settings WHERE key='federation_lending'").fetchone()
        return bool(row and row[0] == "true")

    def _profile(self, profile):
        if set(profile) != {"fleet_id", *PROFILE} or any(profile.get(key) != value for key, value in PROFILE.items()):
            raise Refused("Only the owner's fixed resource profile may be allocated")
        fleet = self.service.fleets.get(profile["fleet_id"])
        if not fleet or fleet["provider"] != "github" or not fleet.get("available"):
            raise Refused("The local GitHub profile is unavailable")
        with schema.connect(self.path) as c:
            row = c.execute("SELECT value FROM platform_settings WHERE key='federation_templates'").fetchone()
        selected = json.loads(row[0]) if row else {}
        image = selected.get(profile["fleet_id"]) or self.service.unit_image(fleet)
        if not image:
            raise Refused("No curated local runtime template is available")
        value = {"provider": "github", "platform": fleet["platform"], "architecture": fleet["architecture"],
                "runtime_template": image, "labels": fleet.get("labels") or providers.by_key("github").default_labels(
                    fleet["platform"], fleet["architecture"], self.service.env).split(","),
                "cpu_limit": "8" if fleet["platform"] == "macos" else None,
                "cpu_override": "8", "memory_limit": PROFILE["memory_bytes"],
                "memory_override": PROFILE["memory_bytes"], "disk_limit": PROFILE["disk_bytes"],
                "cache_policy": {"enabled": True, "max_bytes": PROFILE["cache_bytes"], "on_clear": "drain-first"},
                "fleet_id": None, "desired_state": "running", "actual_state": "planned",
                "capabilities": {"federation_owned": True, "refuse_all_forks": True}}
        if fleet["platform"] == "linux":
            value["memory_swap_limit"] = PROFILE["memory_bytes"]
        return value

    def _admit(self, profile, count):
        spec = self._profile(profile)
        workers = [w for w in self.service.inventory.healthy() if placement.declared(w, "federation_fork_guard") is True and
                   spec["runtime_template"] in (placement.declared(w, "federation_templates") or [])]
        occupied = [self.service.reserved_spec(row) for row in self.service.specs.list() if row["actual_state"] != "absent"]
        admitted = []
        for _ in range(count):
            candidate = self.service.for_placement(spec, self.service.effective_spec(spec))
            host, reason = placement.choose(candidate, workers, occupied)
            if not host:
                raise Refused(reason or "No locally admitted worker capacity is available")
            placed = dict(spec, host_id=host)
            if spec["platform"] in ("linux", "windows"):
                taken = [cpusets.parse(row["cpu_limit"]) for row in occupied
                         if row.get("host_id") == host and cpusets.is_cpuset(row.get("cpu_limit"))]
                placed["cpu_limit"] = cpusets.allocate(8, self.service._host_cores(spec["platform"], host), taken)
            occupied.append(dict(placed, runner_id=f"local-admission-{len(admitted)}"))
            admitted.append(placed)
        return admitted

    def available(self, profile):
        if not self.enabled() or not self.client or not self.service.env.get("GITHUB_DRAIN_GROUP"):
            return False
        try:
            self._admit(profile, 1)
            return True
        except Exception:
            return False

    def restricted_group(self, group_name):
        if group_name.casefold() in {"fillz", "default", "stoney"}:
            raise Refused("Ordinary runner groups are never used for lending")
        groups = self.client.runner_groups()
        group = next((g for g in groups or [] if g.get("name") == group_name), None)
        if not group or group.get("visibility") != "selected" or group.get("allows_public_repositories") is not False or group.get("inherited") is not False or group.get("default") is not False:
            raise Refused("The lent group must be local, selected and refuse public repositories")
        repos = self.client._get(f"/orgs/{self.client.org}/actions/runner-groups/{group['id']}/repositories",
                                params={"per_page": 1})
        if not isinstance(repos, dict) or type(repos.get("total_count")) is not int or repos["total_count"] < 1:
            raise Refused("Select deliberate private repositories for the lent group first")
        members = self.client._pages(f"/orgs/{self.client.org}/actions/runner-groups/{group['id']}/runners", "runners")
        with schema.connect(self.path) as c:
            ids = [row[0] for row in c.execute("SELECT runner_id FROM federation_allocations")]
        owned_names = {providers._forge_name(row) for rid in ids if (row := self.service.specs.get(rid))}
        if not isinstance(members, list) or any(not isinstance(member, dict) or member.get("name") not in owned_names for member in members):
            raise Refused("The restricted group must contain only this receiver's privately claimed lent runners")
        group["members"] = members
        return group

    def _current_permission(self, c, request_id, group):
        row = c.execute("SELECT request.*,peer.enabled,peer.policy FROM federation_requests request JOIN federation_peers peer "
                        "ON peer.id=request.peer_id WHERE request.id=?", (request_id,)).fetchone()
        if not row or not row["enabled"] or row["state"] not in ("approved", "activating", "active") or row["expires_at"] <= int(time.time()):
            raise Refused("Lent registration has no current local lease permission")
        intent, limits = json.loads(row["intent"]), json.loads(row["policy"])
        if (group not in limits.get("runner_groups", []) or intent["profile"] not in limits["profiles"] or
                intent["organization"].casefold() not in {org.casefold() for org in limits["organizations"]} or
                intent["count"] > limits["max_runners"] or intent["lease_minutes"] > limits["max_lease_minutes"] or
                time.gmtime().tm_hour in limits["quiet_hours"]):
            raise Refused("Current owner limits do not permit this lent registration")
        occupied = sum(json.loads(other[0])["count"] for other in c.execute("SELECT intent FROM federation_requests WHERE peer_id=? "
            "AND state IN ('approved','activating','active','retiring')", (row["peer_id"],)))
        if occupied > limits["max_runners"] or not self.enabled():
            raise Refused("The current owner reservation budget does not permit registration")
        return row

    def provision(self, request_id, profile, organization, count, lease_expires_at, credential):
        if not self.enabled() or not self.client or organization.casefold() != self.client.org.casefold():
            raise Refused("Only this receiver's own safely observed GitHub organisation can be lent")
        group = credential["runner_group"]
        with schema.connect(self.path) as c:
            request = c.execute("SELECT * FROM federation_requests WHERE id=?", (request_id,)).fetchone()
            peer = c.execute("SELECT * FROM federation_peers WHERE id=? AND enabled=1", (request["peer_id"],)).fetchone() if request else None
        if not request or not peer or request["state"] not in ("approved", "activating", "active"):
            raise Refused("There is no active local allocation permission")
        if group not in json.loads(peer["policy"]).get("runner_groups", []):
            raise Refused("This group is not allowed by the receiving owner")
        with schema.connect(self.path) as c:
            owned = c.execute("SELECT runner_id FROM federation_allocations WHERE request_id=?", (request_id,)).fetchall()
        if owned:
            existing = [self.service.specs.get(row["runner_id"]) for row in owned]
            if any(not row or row["actual_state"] in ("failed", "absent") for row in existing):
                raise Refused("An owned lent runner failed; retiring this allocation")
            if any(row["actual_state"] not in ("idle", "busy") or not row.get("registration_id") for row in existing):
                raise AllocationPending()
        self.restricted_group(group)
        spec = self._profile(profile)
        intent = json.loads(request["intent"])
        if intent != {"profile": profile["fleet_id"], "organization": organization, "count": count,
                      "lease_minutes": intent.get("lease_minutes")} or request["expires_at"] != lease_expires_at:
            raise Refused("Allocation parameters differ from the locally approved intent")
        if int(time.time()) >= lease_expires_at or credential["expires_at"] <= int(time.time()):
            raise Refused("Lease or requester registration credential has expired")
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            # A revocation while checking GitHub must still fence publication.
            permission = c.execute("SELECT request.state,peer.enabled,peer.policy FROM federation_requests request JOIN federation_peers peer "
                                   "ON peer.id=request.peer_id WHERE request.id=?", (request_id,)).fetchone()
            if not permission or not permission["enabled"] or permission["state"] not in ("approved", "activating", "active"):
                raise Refused("Local permission changed before allocation")
            current = json.loads(permission["policy"])
            if (group not in current.get("runner_groups", []) or profile["fleet_id"] not in current["profiles"] or
                    organization.casefold() not in {org.casefold() for org in current["organizations"]} or count > current["max_runners"]):
                raise Refused("Local limits changed before allocation")
            occupied = sum(json.loads(row[0])["count"] for row in c.execute("SELECT intent FROM federation_requests WHERE peer_id=? AND state IN ('approved','activating','active','retiring')", (request["peer_id"],)))
            if occupied > current["max_runners"]:
                raise Refused("The local reservation budget changed before allocation")
            owned = c.execute("SELECT * FROM federation_allocations WHERE request_id=? ORDER BY slot", (request_id,)).fetchall()
            if not owned:
                admitted = self._admit(profile, count)
                sealed = SecretStore(self.path)._fernet(create=True).encrypt(credential["registration_token"].encode())
                for slot in range(count):
                    rid = self.service.specs.create_in(c, **admitted[slot], runner_group=group)
                    c.execute("INSERT INTO federation_allocations VALUES(?,?,?,?,?,?,?)",
                              (request_id, rid, slot, organization, group, sealed, credential["expires_at"]))
                owned = c.execute("SELECT * FROM federation_allocations WHERE request_id=? ORDER BY slot", (request_id,)).fetchall()
        ids = [row["runner_id"] for row in owned]
        for rid in ids:
            row = self.service.specs.get(rid)
            if row["actual_state"] in ("failed", "absent"):
                raise Refused("An owned lent runner failed to register; retiring this allocation")
            if row["actual_state"] not in ("idle", "busy") or not row.get("registration_id"):
                raise AllocationPending()
            proof = (row.get("telemetry") or {}).get("origin_guard") or {}
            try:
                from datetime import datetime
                age = time.time() - datetime.fromisoformat(proof["at"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, TypeError):
                age = float("inf")
            if proof.get("federation_guard") is not True or proof.get("version", 0) < 1 or not -30 <= age <= 900:
                raise AllocationPending()
        confirmed = self.restricted_group(group)["members"]
        for rid in ids:
            row = self.service.specs.get(rid)
            record = next((member for member in confirmed if str(member.get("id")) == str(row["registration_id"])), None)
            if not record or record.get("name") != providers._forge_name(row) or not isinstance(record.get("labels"), list):
                raise Refused("GitHub did not prove this lent registration in its restricted group")
            have = {label["name"].casefold() for label in record["labels"] if isinstance(label, dict) and isinstance(label.get("name"), str)}
            want = {label.casefold() for label in row["labels"]}
            defaults = set(providers.by_key("github").default_labels(row["platform"], row["architecture"]).casefold().split(","))
            if not want <= have or have - defaults - want:
                raise Refused("The lent GitHub registration labels drifted from its locally admitted spec")
        with schema.connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            self._current_permission(c, request_id, group)
        return ids

    def retire(self, request_id, runner_ids):
        with schema.connect(self.path) as c:
            owned = c.execute("SELECT runner_id FROM federation_allocations WHERE request_id=?", (request_id,)).fetchall()
            c.execute("UPDATE federation_allocations SET sealed_registration=NULL WHERE request_id=?", (request_id,))
        absent = True
        for row in owned:
            rid = row["runner_id"]
            spec = self.service.specs.get(rid)
            if not spec or spec["actual_state"] == "absent":
                if spec:
                    retirement_safe(self.service, spec, require_absent=True)
                continue
            absent = False
            if spec["desired_state"] != "absent":
                self.service.specs.update(rid, spec["spec_version"], desired_state="absent")
        return absent

    def running_job(self, runner_ids):
        with schema.connect(self.path) as c:
            owned = {row[0] for row in c.execute("SELECT runner_id FROM federation_allocations")}
        if not runner_ids or any(rid not in owned for rid in runner_ids):
            return False
        rows = [self.service.specs.get(rid) for rid in runner_ids]
        return bool(rows) and any(row and row["actual_state"] == "busy" and row.get("forge_state") == "busy" for row in rows)


def registration_for(service, spec):
    """Return a requester-token plan only for a privately claimed lent spec."""
    path = service.specs.path
    with schema.connect(path) as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_allocations'").fetchone()
        allocation = c.execute("SELECT * FROM federation_allocations WHERE runner_id=?", (spec["runner_id"],)).fetchone() if exists else None
        request = c.execute("SELECT * FROM federation_requests WHERE id=?", (allocation["request_id"],)).fetchone() if allocation else None
        peer = c.execute("SELECT * FROM federation_peers WHERE id=? AND enabled=1", (request["peer_id"],)).fetchone() if request else None
    if not allocation:
        if (spec.get("capabilities") or {}).get("federation_owned"):
            raise Refused("A lent spec without its private ownership claim cannot mint a local token")
        return None
    if not request or not peer or request["state"] not in ("activating", "active") or request["expires_at"] <= int(time.time()):
        raise Refused("Lent registration has no current local lease permission")
    if allocation["organization"].casefold() != str(service.env.get("GITHUB_ORG") or "").casefold() or spec["runner_group"] != allocation["runner_group"]:
        raise Refused("Lent registration scope does not match the receiver's safe observer")
    if not allocation["sealed_registration"] or allocation["credential_expires_at"] <= int(time.time()):
        raise Refused("The requester registration credential has expired or been consumed")
    provisioner = getattr(service, "federation_provisioner", None) or LocalProvisioner(service)
    provisioner.restricted_group(allocation["runner_group"])
    with schema.connect(path) as c:
        c.execute("BEGIN IMMEDIATE")
        provisioner._current_permission(c, allocation["request_id"], allocation["runner_group"])
        allocation = c.execute("SELECT * FROM federation_allocations WHERE runner_id=?", (spec["runner_id"],)).fetchone()
        if not allocation["sealed_registration"] or allocation["credential_expires_at"] <= int(time.time()):
            raise Refused("The requester registration credential expired or was consumed during verification")
        token = SecretStore(path)._fernet().decrypt(allocation["sealed_registration"]).decode()
    return providers.RegistrationPlan(url=f"https://github.com/{allocation['organization']}", token=token,
        name=providers._forge_name(spec), labels=",".join(spec.get("labels") or []),
        runner_group=allocation["runner_group"], replace=not spec.get("registration_id"))


def registration_consumed(service, spec):
    with schema.connect(service.specs.path) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_allocations'").fetchone():
            c.execute("UPDATE federation_allocations SET sealed_registration=NULL WHERE runner_id=?", (spec["runner_id"],))


def retirement_safe(service, spec, require_absent=False):
    """Fresh forge and closed-drain proof, only for privately claimed units.

    Even a stopped/failed worker cannot authorise deletion when GitHub is
    unknown. Normal fleet retirement retains its existing implementation.
    """
    with schema.connect(service.specs.path) as c:
        table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='federation_allocations'").fetchone()
        owned = c.execute("SELECT * FROM federation_allocations WHERE runner_id=?", (spec["runner_id"],)).fetchone() if table else None
    if not owned:
        if (spec.get("capabilities") or {}).get("federation_owned"):
            raise Refused("A lent runner without its private claim cannot be destructively retired")
        return
    adapter = getattr(service, "federation_provisioner", None) or LocalProvisioner(service)
    client = adapter.client
    if not client or client.org.casefold() != owned["organization"].casefold():
        raise Refused("Lent retirement has no safe local forge observer")
    records = client.all_runners()
    if not isinstance(records, list):
        raise Refused("GitHub is unknown; owned capacity is held for graceful retirement")
    name = providers._forge_name(spec)
    record = next((row for row in records if row.get("name") == name), None)
    by_id = next((row for row in records if str(row.get("id")) == str(spec.get("registration_id"))), None) if spec.get("registration_id") else None
    if by_id and by_id.get("name") != name or record and spec.get("registration_id") and str(record.get("id")) != str(spec["registration_id"]):
        raise Refused("The forge registration does not match the private lent ownership claim")
    if record is None:
        return
    if require_absent:
        raise Refused("The private lent forge record must be absent before its unit is removed")
    if type(record.get("busy")) is not bool or record["busy"] or record.get("status") not in ("online", "offline"):
        raise Refused("The owned lent job is running or unknown; retirement remains held")
    drain = service.env.get("GITHUB_DRAIN_GROUP")
    if not drain or client.drain_group_closed(drain) is not True or client.move_runner_to_group(drain, record["id"]) is not True or client.drain_group_closed(drain, record["id"]) is not True:
        raise Refused("GitHub did not confirm this owned runner in the closed drain group")
    fresh = client.all_runners()
    seen = next((row for row in fresh or [] if row.get("id") == record["id"]), None)
    if not isinstance(fresh, list) or seen and (seen.get("busy") is not False or seen.get("name") != name or seen.get("status") not in ("online", "offline")):
        raise Refused("The owned runner's fresh job state is unknown; retirement remains held")
