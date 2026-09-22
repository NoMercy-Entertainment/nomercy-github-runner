import json
import os
from pathlib import Path
from datetime import timedelta

import pytest
from cryptography import x509

from control import ca, certificates as certs
from control.inventory import Inventory
from store import schema


@pytest.fixture
def pki(tmp_path):
    root = tmp_path / "tls"
    root.mkdir()
    authority, secret = ca.create_ca()
    (root / "ca.pem").write_bytes(authority)
    (root / "ca.key").write_bytes(secret)
    for role in ("controller", "receiver"):
        cert, key = ca.issue(authority, secret, "controller", role)
        (root / (role + ".crt")).write_bytes(cert)
        (root / (role + ".key")).write_bytes(key)
    db = str(tmp_path / "control.db")
    schema.init(db)
    worker = root / "workers" / "linux-1"
    worker.mkdir(parents=True)
    cert, key = ca.issue(authority, secret, "linux-1", "agent")
    (worker / "agent.crt").write_bytes(cert)
    (worker / "agent.key").write_bytes(key)
    (worker / "ca.pem").write_bytes(authority)
    Inventory(db).register_worker("linux-1", "hyperv-linux", certificate_fingerprint=ca.fingerprint(cert))
    return root, db


def maintenance(db):
    with schema.connect(db) as c:
        c.execute("INSERT INTO platform_settings(key,value) VALUES('maintenance','true')")


def test_cli_inspects_prepares_and_requires_explicit_activation_attestation(pki, capsys):
    from control.main import main
    root, db = pki
    args = ["certificates", "--tls-dir", str(root), "--db", db]
    assert main([*args, "status"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    assert main([*args, "prepare", "controller"]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert main([*args, "activate", "controller", prepared["bundle"]]) == 2
    assert "stop the controller" in capsys.readouterr().out
    maintenance(db)
    assert main([*args, "activate", "controller", prepared["bundle"], "--services-stopped"]) == 0
    capsys.readouterr()
    assert certs.resolve_paths(root, "controller")[0] == str(Path(prepared["bundle"]) / "controller.crt")
    assert main([*args, "rollback", "controller", "--services-stopped"]) == 0
    assert certs.resolve_paths(root, "controller")[0] == str(root / "controller.crt")


def test_dashboard_tls_client_uses_the_activated_pair(pki, monkeypatch):
    import api_v2
    import control.agent_client
    root, db = pki
    prepared = certs.prepare(root, "controller", db=db)
    maintenance(db)
    certs.activate_local(root, prepared["bundle"], "controller", db, services_stopped=True)
    called = []
    class Client:
        def __init__(self, inventory, cert, key, ca_path, **kwargs):
            called.append((cert, key, ca_path))
        def call_and_wait(self, *args, **kwargs):
            return {"ok": True}
    monkeypatch.setattr(control.agent_client, "AgentClient", Client)
    assert api_v2._LazyAgentClient(None, str(root), db).call_and_wait("hello") == {"ok": True}
    assert called == [certs.resolve_paths(root, "controller")]


def test_prepare_is_unique_private_and_changes_no_active_identity(pki):
    root, db = pki
    ca_before = (root / "ca.pem").read_bytes(), (root / "ca.key").read_bytes()
    original_pin = Inventory(db).get("linux-1")["certificate_fingerprint"]
    one = certs.prepare(root, "agent", "linux-1", db)
    two = certs.prepare(root, "agent", "linux-1", db)
    assert one["bundle"] != two["bundle"]
    assert one["fingerprint"] != two["fingerprint"]
    assert Inventory(db).get("linux-1")["certificate_fingerprint"] == original_pin
    assert ((root / "ca.pem").read_bytes(), (root / "ca.key").read_bytes()) == ca_before
    assert not (Path(one["bundle"]) / "ca.key").exists()
    assert "PRIVATE KEY" not in json.dumps(one)
    if os.name != "nt":
        assert Path(one["bundle"]).stat().st_mode & 0o777 == 0o700
        assert (Path(one["bundle"]) / "agent.key").stat().st_mode & 0o777 == 0o600


def test_public_status_warns_before_expiry_and_never_reads_private_keys(pki, monkeypatch):
    root, db = pki
    leaf = x509.load_pem_x509_certificate((root / "controller.crt").read_bytes())
    read = certs._read
    def public_only(path):
        assert not str(path).endswith(".key")
        return read(path)
    monkeypatch.setattr(certs, "_read", public_only)
    rows = certs.status(root, db, now=leaf.not_valid_after_utc - timedelta(days=20))
    assert len(rows) == 4
    controller = next(row for row in rows if row["role"] == "controller")
    assert controller["warning"] == "expires within 30 days"
    assert controller["days_remaining"] == 20
    assert "PRIVATE KEY" not in json.dumps(rows)
    expired = certs.status(root, db, now=leaf.not_valid_after_utc + timedelta(seconds=1))
    assert next(row for row in expired if row["role"] == "controller")["warning"] == "expired"


def test_worker_activation_requires_maintenance_and_installation_attestations(pki):
    root, db = pki
    renewal = certs.prepare(root, "agent", "linux-1", db)
    original = Inventory(db).get("linux-1")["certificate_fingerprint"]
    with pytest.raises(certs.CertificateRefused, match="stop the agent"):
        certs.activate_worker(root, renewal["bundle"], "linux-1", db)
    with pytest.raises(certs.CertificateRefused, match="maintenance"):
        certs.activate_worker(root, renewal["bundle"], "linux-1", db, agent_stopped=True, bundle_installed=True)
    assert Inventory(db).get("linux-1")["certificate_fingerprint"] == original
    maintenance(db)
    for _ in range(2):
        certs.activate_worker(root, renewal["bundle"], "linux-1", db, agent_stopped=True, bundle_installed=True)
    assert Inventory(db).get("linux-1")["certificate_fingerprint"] == renewal["fingerprint"]
    current = next(row for row in certs.status(root, db) if row["subject"] == "linux-1")
    assert current["fingerprint"] == renewal["fingerprint"]


@pytest.mark.parametrize("tamper", ["subject", "eku", "foreign-ca", "key", "expired"])
def test_invalid_renewals_never_change_worker_pin(pki, monkeypatch, tamper):
    root, db = pki
    renewal = certs.prepare(root, "agent", "linux-1", db)
    bundle = Path(renewal["bundle"])
    original = Inventory(db).get("linux-1")["certificate_fingerprint"]
    authority, secret = (root / "ca.pem").read_bytes(), (root / "ca.key").read_bytes()
    if tamper == "expired":
        monkeypatch.setattr(certs, "_now", lambda: ca._now() + timedelta(days=366))
    elif tamper == "key":
        (bundle / "agent.key").write_text("not a key")
    else:
        if tamper == "foreign-ca":
            authority, secret = ca.create_ca("foreign")
        cert, key = ca.issue(authority, secret, "other-worker" if tamper == "subject" else "linux-1",
                             "controller" if tamper == "eku" else "agent")
        (bundle / "agent.crt").write_bytes(cert)
        (bundle / "agent.key").write_bytes(key)
        descriptor = json.loads((bundle / "renewal.json").read_text())
        descriptor["fingerprint"] = ca.fingerprint(cert)
        (bundle / "renewal.json").write_text(json.dumps(descriptor))
    maintenance(db)
    with pytest.raises(certs.CertificateRefused):
        certs.activate_worker(root, bundle, "linux-1", db, agent_stopped=True, bundle_installed=True)
    assert Inventory(db).get("linux-1")["certificate_fingerprint"] == original


def test_a_stale_prepared_worker_bundle_cannot_undo_a_later_rotation(pki):
    root, db = pki
    first = certs.prepare(root, "agent", "linux-1", db)
    stale = certs.prepare(root, "agent", "linux-1", db)
    maintenance(db)
    certs.activate_worker(root, first["bundle"], "linux-1", db, agent_stopped=True, bundle_installed=True)
    with pytest.raises(certs.CertificateRefused, match="pin changed"):
        certs.activate_worker(root, stale["bundle"], "linux-1", db, agent_stopped=True, bundle_installed=True)


@pytest.mark.parametrize("role", ["controller", "receiver"])
def test_local_activation_switches_a_complete_pair_and_supports_rollback(pki, role):
    root, db = pki
    old = certs.resolve_paths(root, role)
    before = tuple(Path(path).read_bytes() for path in old)
    renewal = certs.prepare(root, role)
    with pytest.raises(certs.CertificateRefused, match="maintenance"):
        certs.activate_local(root, renewal["bundle"], role, db, services_stopped=True)
    maintenance(db)
    certs.activate_local(root, renewal["bundle"], role, db, services_stopped=True)
    current = certs.resolve_paths(root, role)
    assert Path(current[0]).parent == Path(current[1]).parent == Path(renewal["bundle"])
    assert tuple(Path(path).read_bytes() for path in old) == before
    certs.rollback_local(root, role, db, services_stopped=True)
    assert certs.resolve_paths(root, role) == old


def test_failed_atomic_manifest_replace_keeps_original_pair(pki, monkeypatch):
    root, db = pki
    renewal = certs.prepare(root, "controller")
    old = certs.resolve_paths(root, "controller")
    maintenance(db)
    def fail(*args):
        raise OSError("injected rename failure")
    monkeypatch.setattr(certs.os, "replace", fail)
    with pytest.raises(OSError):
        certs.activate_local(root, renewal["bundle"], "controller", db, services_stopped=True)
    assert certs.resolve_paths(root, "controller") == old
    assert not (root / certs.MANIFEST).exists()


def test_leaf_expiry_is_bounded_by_authority_and_wrong_ca_key_is_refused():
    authority, secret = ca.create_ca(days=10)
    cert, _ = ca.issue(authority, secret, "linux-1", "agent", days=365)
    assert x509.load_pem_x509_certificate(cert).not_valid_after_utc <= x509.load_pem_x509_certificate(authority).not_valid_after_utc
    _, other_secret = ca.create_ca()
    with pytest.raises(ValueError, match="do not match"):
        ca.issue(authority, other_secret, "linux-1", "agent")


def test_cannot_prepare_an_authority_or_agent_with_controller_identity(pki):
    root, db = pki
    for role, subject in (("ca", "ca"), ("agent", "controller"), ("receiver", "linux-1")):
        with pytest.raises(certs.CertificateRefused):
            certs.prepare(root, role, subject, db)


def test_status_and_worker_preparation_never_create_a_missing_database(pki, tmp_path):
    root, _ = pki
    missing = tmp_path / "missing.db"
    with pytest.raises(certs.CertificateRefused, match="existing control database"):
        certs.status(root, missing)
    with pytest.raises(certs.CertificateRefused, match="existing control database"):
        certs.prepare(root, "agent", "linux-1", missing)
    assert not missing.exists()


def test_status_reports_a_broken_activation_manifest_without_reading_a_key(pki):
    root, _ = pki
    (root / certs.MANIFEST).write_text("corrupt manifest")
    rows = certs.status(root)
    assert next(row for row in rows if row["role"] == "controller")["warning"] == "certificate unavailable or invalid"
