"""The control plane's private certificate authority.

Every agent gets a certificate from here whose subject is its `host_id`, and the
controller gets one whose subject is `controller`. Both sides require the
other's certificate to chain to this authority, and each checks the other's
subject by name - so an agent's certificate cannot be used to command another
agent, even though both were issued here.

The controller additionally pins each agent's certificate by fingerprint, in
`workers.certificate_fingerprint`. A certificate that chains correctly and
names the right host but is not the one pinned is refused: that is what a
re-issued certificate the operator did not expect looks like, and the case a
chain check alone cannot see.

Uses the `cryptography` package, imported only by the functions that issue -
the controller needs it to mint certificates, but nothing that merely checks
one does. The agent never needs it: TLS itself is the standard library's.
"""
import datetime
import hashlib
import ssl

#: The subject every controller certificate carries. The agent refuses a client
#: whose certificate names anything else - including another agent.
CONTROLLER_SUBJECT = "controller"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _key():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _pem(cert=None, key=None):
    from cryptography.hazmat.primitives import serialization
    if cert is not None:
        return cert.public_bytes(serialization.Encoding.PEM)
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def create_ca(name="runner control plane CA", days=3650):
    """A new authority. Returns (certificate PEM, private key PEM)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    key = _key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(minutes=5))
            .not_valid_after(_now() + datetime.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                           critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return _pem(cert=cert), _pem(key=key)


def issue(ca_cert_pem, ca_key_pem, subject, role, days=365):
    """A certificate for one end of the channel. Returns (cert PEM, key PEM).

    `role` is "agent" (it serves the control verbs), "controller" (it
    connects to agents) or "receiver" (the controller's own endpoint, which
    agents connect to for heartbeats and events). The extended key usage is
    set to match, so a certificate issued for one role is not accepted in
    another.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    if role not in ("agent", "controller", "receiver"):
        raise ValueError(f"unknown role {role!r}")
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("certificate lifetime must be a positive number of days")
    if not ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("the issuer is not a certificate authority")
    encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if ca_cert.public_key().public_bytes(encoding, form) != ca_key.public_key().public_bytes(encoding, form):
        raise ValueError("the authority certificate and key do not match")
    now = _now()
    if not ca_cert.not_valid_before_utc <= now < ca_cert.not_valid_after_utc:
        raise ValueError("the certificate authority is not currently valid")
    key = _key()
    # An agent is a server to the controller's calls and a client when it
    # sends heartbeats, so it needs both; OpenSSL checks the purpose of a
    # client certificate and would refuse an agent's beat without clientAuth.
    # That an agent's certificate can therefore authenticate as a client is
    # why the agent also checks the client's subject by name.
    usage = {"agent": [ExtendedKeyUsageOID.SERVER_AUTH,
                       ExtendedKeyUsageOID.CLIENT_AUTH],
             "controller": [ExtendedKeyUsageOID.CLIENT_AUTH],
             "receiver": [ExtendedKeyUsageOID.SERVER_AUTH]}[role]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                        subject)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(max(now - datetime.timedelta(minutes=5), ca_cert.not_valid_before_utc))
            .not_valid_after(min(now + datetime.timedelta(days=days), ca_cert.not_valid_after_utc))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(x509.ExtendedKeyUsage(usage), critical=False)
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(subject)]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return _pem(cert=cert), _pem(key=key)


def fingerprint(cert):
    """`sha256:<hex>` of a certificate, given as PEM text, PEM bytes or DER.

    The form stored in `workers.certificate_fingerprint`. Computed over the DER
    encoding, which is what a TLS peer actually presents, so the value from an
    issued file and the value read off a live connection agree.
    """
    if isinstance(cert, str):
        cert = cert.encode()
    if cert.lstrip().startswith(b"-----BEGIN"):
        cert = ssl.PEM_cert_to_DER_cert(cert.decode())
    return "sha256:" + hashlib.sha256(cert).hexdigest()
