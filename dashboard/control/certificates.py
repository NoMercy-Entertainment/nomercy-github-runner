"""Explicit leaf renewal under the existing CA; no process or remote actions.

Local certificate/key pairs live in immutable bundles. One atomic manifest
replacement selects a complete pair; callers must resolve both paths together.
Worker activation changes only the inventory pin, after explicit installation
and stopped-agent attestations while platform maintenance is enabled.
"""
import json
import math
import os
from pathlib import Path
import re
import tempfile
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from store import schema
from . import ca

WARNING_DAYS = 30
MANIFEST = "active-certificates.json"
ROLES = {"agent": {ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH},
         "controller": {ExtendedKeyUsageOID.CLIENT_AUTH},
         "receiver": {ExtendedKeyUsageOID.SERVER_AUTH}}


class CertificateRefused(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _read(path):
    return Path(path).read_bytes()


def _database(db):
    path = Path(db or schema.DB_PATH)
    if not path.is_file():
        raise CertificateRefused("an existing control database is required")
    return path


def _workers(db):
    try:
        with closing(sqlite3.connect(_database(db).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT host_id,certificate_fingerprint FROM workers")]
    except sqlite3.Error as e:
        raise CertificateRefused("worker certificate inventory is unavailable") from e


def _subject(role, subject):
    if role not in ROLES:
        raise CertificateRefused("only agent, controller and receiver leaves can be renewed")
    if not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", subject):
        raise CertificateRefused("invalid certificate subject")
    if (role == "agent") == (subject == ca.CONTROLLER_SUBJECT):
        raise CertificateRefused("controller identity is reserved for controller and receiver certificates")


def _inside(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise CertificateRefused("certificate path escapes the TLS directory")
    return target


def _manifest(tls_dir):
    path = Path(tls_dir) / MANIFEST
    if not path.exists():
        return {"version": 1, "active": {}, "previous": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("active"), dict) or not isinstance(value.get("previous"), dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError) as e:
        raise CertificateRefused("invalid certificate activation manifest") from e


def _entry(tls_dir, role, manifest):
    if role not in ("controller", "receiver"):
        raise CertificateRefused("only controller and receiver have local active pairs")
    entry = manifest["active"].get(role) or {"cert": role + ".crt", "key": role + ".key"}
    return {key: str(_inside(tls_dir, entry[key])) for key in ("cert", "key")}


def resolve_paths(tls_dir, role):
    """Return cert, key and CA paths from one manifest snapshot."""
    entry = _entry(tls_dir, role, _manifest(tls_dir))
    return entry["cert"], entry["key"], str(Path(tls_dir) / "ca.pem")


def _metadata(cert, role, now=None):
    now = now or _now()
    remaining = (cert.not_valid_after_utc - now).total_seconds() / 86400
    subject = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    warning = ("not yet valid" if now < cert.not_valid_before_utc else "expired" if remaining <= 0
               else "expires within 30 days" if remaining <= WARNING_DAYS else None)
    return {"role": role, "subject": subject[0].value if subject else cert.subject.rfc4514_string(),
            "expires_at": cert.not_valid_after_utc.isoformat(),
            "days_remaining": math.floor(remaining),
            "warning": warning,
            "fingerprint": ca.fingerprint(cert.public_bytes(serialization.Encoding.PEM))}


def _validate(cert_pem, key_pem, authority_pem, role, subject, now=None):
    _subject(role, subject)
    now = now or _now()
    try:
        authority = x509.load_pem_x509_certificate(authority_pem)
        cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, password=None)
        authority.verify_directly_issued_by(authority)
        if not authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise CertificateRefused("issuer is not a CA")
        if not authority.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
            raise CertificateRefused("issuer cannot sign certificates")
        for item in (authority, cert):
            if not item.not_valid_before_utc <= now < item.not_valid_after_utc:
                raise CertificateRefused("certificate is expired or not yet valid")
        cert.verify_directly_issued_by(authority)
        if cert.not_valid_after_utc > authority.not_valid_after_utc:
            raise CertificateRefused("leaf outlives its authority")
        expected = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
        if cert.subject != expected:
            raise CertificateRefused("certificate subject does not match the requested identity")
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise CertificateRefused("an authority cannot be installed as a leaf")
        if set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != ROLES[role]:
            raise CertificateRefused("certificate purpose does not match its role")
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if subject not in names:
            raise CertificateRefused("certificate SAN does not contain its identity")
        encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        if cert.public_key().public_bytes(encoding, form) != key.public_key().public_bytes(encoding, form):
            raise CertificateRefused("certificate and private key do not match")
    except CertificateRefused:
        raise
    except Exception as e:
        # Parser/signature errors must never include PEM or private key material.
        raise CertificateRefused("certificate bundle failed cryptographic validation") from e
    return _metadata(cert, role, now)


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def prepare(tls_dir, role, subject=None, db=None, days=365):
    """Create a unique private bundle without changing any active pair or pin."""
    subject = subject or ca.CONTROLLER_SUBJECT
    _subject(role, subject)
    if role == "agent":
        worker = next((row for row in _workers(db) if row["host_id"] == subject), None)
        if worker is None or not worker.get("certificate_fingerprint"):
            raise CertificateRefused("renewal requires an enrolled worker with an existing pin")
        previous = worker["certificate_fingerprint"]
    else:
        previous = ca.fingerprint(_read(resolve_paths(tls_dir, role)[0]))
    root = Path(tls_dir).resolve()
    authority = _read(root / "ca.pem")
    try:
        cert, key = ca.issue(authority, _read(root / "ca.key"), subject, role, days=days)
    except Exception as e:
        raise CertificateRefused("the existing authority could not issue the renewal") from e
    metadata = _validate(cert, key, authority, role, subject)
    renewals = _inside(root, "renewals")
    renewals.mkdir(mode=0o700, exist_ok=True)
    bundle = Path(tempfile.mkdtemp(prefix=role + "-", dir=renewals))
    os.chmod(bundle, 0o700)
    descriptor = {"version": 1, "role": role, "subject": subject,
                  "previous_fingerprint": previous, "fingerprint": metadata["fingerprint"],
                  "authority_fingerprint": ca.fingerprint(authority)}
    for name, data in ((role + ".crt", cert), (role + ".key", key), ("ca.pem", authority),
                       ("renewal.json", json.dumps(descriptor, sort_keys=True).encode())):
        _write_new(bundle / name, data)
    return {"bundle": str(bundle), **metadata, "previous_fingerprint": previous}


def _prepared(tls_dir, bundle, role, subject):
    bundle = _inside(tls_dir, bundle)
    if not bundle.is_relative_to((Path(tls_dir) / "renewals").resolve()):
        raise CertificateRefused("activation requires a prepared renewal bundle")
    try:
        descriptor = json.loads(_read(bundle / "renewal.json"))
        authority = _read(Path(tls_dir) / "ca.pem")
        if descriptor.get("version") != 1 or descriptor.get("role") != role or descriptor.get("subject") != subject:
            raise CertificateRefused("prepared renewal has a different role or identity")
        if descriptor.get("authority_fingerprint") != ca.fingerprint(authority) or _read(bundle / "ca.pem") != authority:
            raise CertificateRefused("prepared renewal does not use the current authority")
        metadata = _validate(_read(bundle / (role + ".crt")), _read(bundle / (role + ".key")), authority, role, subject)
        if descriptor.get("fingerprint") != metadata["fingerprint"]:
            raise CertificateRefused("prepared certificate has changed")
    except (OSError, ValueError, TypeError, AttributeError) as e:
        if isinstance(e, CertificateRefused):
            raise
        raise CertificateRefused("invalid prepared renewal bundle") from e
    return bundle, descriptor, metadata


def _maintenance(connection):
    row = connection.execute("SELECT value FROM platform_settings WHERE key='maintenance'").fetchone()
    if not row or str(row[0]).lower() not in ("true", "1"):
        raise CertificateRefused("certificate activation requires explicit platform maintenance")


def activate_worker(tls_dir, bundle, host_id, db, *, agent_stopped=False, bundle_installed=False):
    """Pin a validated installed renewal; no remote file or process changes."""
    if agent_stopped is not True or bundle_installed is not True:
        raise CertificateRefused("stop the agent and install its prepared bundle before changing the pin")
    _, descriptor, metadata = _prepared(tls_dir, bundle, "agent", host_id)
    with schema.connect(str(_database(db))) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _maintenance(connection)
        worker = connection.execute("SELECT certificate_fingerprint FROM workers WHERE host_id=?", (host_id,)).fetchone()
        if not worker:
            raise CertificateRefused("worker is not enrolled")
        if worker[0] == metadata["fingerprint"]:
            return metadata
        if worker[0] != descriptor.get("previous_fingerprint"):
            raise CertificateRefused("worker pin changed after this renewal was prepared")
        connection.execute("UPDATE workers SET certificate_fingerprint=?,last_seen_at=NULL,state='unknown' WHERE host_id=?",
                           (metadata["fingerprint"], host_id))
        _audit(connection, "activate_worker_certificate", metadata)
    return metadata


def _audit(connection, verb, metadata):
    connection.execute("INSERT INTO audit(at,actor,verb,decision,parameters) VALUES(?,?,?,?,?)",
                       (_now().isoformat(), "certificate-operator", verb, "accepted", json.dumps(metadata)))


def _save_manifest(tls_dir, manifest):
    root = Path(tls_dir)
    fd, pending = tempfile.mkstemp(prefix=".certificate-manifest-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, root / MANIFEST)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def activate_local(tls_dir, bundle, role, db, *, services_stopped=False):
    """Atomically select a prepared local pair; restart both TLS consumers after."""
    if services_stopped is not True or role not in ("controller", "receiver"):
        raise CertificateRefused("stop the controller and dashboard before activating a local TLS pair")
    root = Path(tls_dir).resolve()
    bundle, descriptor, metadata = _prepared(root, bundle, role, ca.CONTROLLER_SUBJECT)
    with schema.connect(str(_database(db))) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _maintenance(connection)
        manifest = _manifest(root)
        current = _entry(root, role, manifest)
        fingerprint = ca.fingerprint(_read(current["cert"]))
        if fingerprint == metadata["fingerprint"]:
            return metadata
        if fingerprint != descriptor.get("previous_fingerprint"):
            raise CertificateRefused("local certificate changed after this renewal was prepared")
        manifest["previous"][role] = {k: str(Path(v).relative_to(root)) for k, v in current.items()}
        manifest["active"][role] = {k: str((bundle / (role + suffix)).relative_to(root))
                                    for k, suffix in (("cert", ".crt"), ("key", ".key"))}
        _audit(connection, "activate_local_certificate", metadata)
        _save_manifest(root, manifest)
    return metadata


def rollback_local(tls_dir, role, db, *, services_stopped=False):
    """Select the previous still-valid pair with the same guarded atomic switch."""
    if services_stopped is not True or role not in ("controller", "receiver"):
        raise CertificateRefused("stop the controller and dashboard before rolling back a local TLS pair")
    root = Path(tls_dir).resolve()
    with schema.connect(str(_database(db))) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _maintenance(connection)
        manifest = _manifest(root)
        previous = manifest["previous"].get(role)
        if not previous:
            raise CertificateRefused("no previous certificate pair is recorded")
        cert, key = (_inside(root, previous[k]) for k in ("cert", "key"))
        metadata = _validate(_read(cert), _read(key), _read(root / "ca.pem"), role, ca.CONTROLLER_SUBJECT)
        current = _entry(root, role, manifest)
        manifest["active"][role] = previous
        manifest["previous"][role] = {k: str(Path(v).relative_to(root)) for k, v in current.items()}
        _audit(connection, "rollback_local_certificate", metadata)
        _save_manifest(root, manifest)
    return metadata


def status(tls_dir, db=None, now=None):
    """Public metadata only. Never opens CA or leaf private keys."""
    root = Path(tls_dir)
    files = [("ca", "authority", root / "ca.pem")]
    for role in ("controller", "receiver"):
        try:
            path = Path(resolve_paths(root, role)[0])
        except (CertificateRefused, KeyError, TypeError):
            path = None
        files.append((role, ca.CONTROLLER_SUBJECT, path))
    candidates = list(root.glob("workers/*/agent.crt")) + list(root.glob("renewals/*/agent.crt"))
    if db:
        for worker in _workers(db):
            match = None
            for candidate in candidates:
                try:
                    if ca.fingerprint(_read(candidate)) == worker["certificate_fingerprint"]:
                        match = candidate
                        break
                except (OSError, ValueError):
                    continue
            files.append(("agent", worker["host_id"], match))
    else:
        files += [("agent", path.parent.name, path) for path in root.glob("workers/*/agent.crt")]
    result = []
    for role, subject, path in files:
        try:
            item = _metadata(x509.load_pem_x509_certificate(_read(path)), role, now)
        except (OSError, ValueError, TypeError):
            item = {"role": role, "subject": subject, "expires_at": None,
                    "days_remaining": None, "warning": "certificate unavailable or invalid", "fingerprint": None}
        result.append(item)
    return result
