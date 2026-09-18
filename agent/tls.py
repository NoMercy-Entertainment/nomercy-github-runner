"""TLS for the agent, from the standard library alone.

Mutual: the agent presents its certificate, subject `host_id`, and requires the
connecting side to present one too, issued by the same control-plane authority
and naming the controller. Requiring a certificate from the right authority is
not enough on its own - every agent holds one - so the subject is checked by
name as well. Without that, a compromised worker could use its own certificate
to command every other worker.

The handshake runs on the request's own thread, never in the accept loop, so a
client that connects and then says nothing holds up nobody but itself, and not
for longer than `HANDSHAKE_TIMEOUT`.
"""
import ssl

#: Must match `control.ca.CONTROLLER_SUBJECT`; checked by a dashboard test.
CONTROLLER_SUBJECT = "controller"

HANDSHAKE_TIMEOUT = 10


def server_context(cert_file, key_file, ca_file):
    """The agent's side: present our certificate, demand theirs."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert_file, key_file)
    context.load_verify_locations(ca_file)
    return context


def client_context(cert_file, key_file, ca_file):
    """For the agent's own outbound calls - heartbeats and events.

    Hostname checking is off because identity is checked by subject name and,
    on the controller side, by pinned fingerprint. A hostname match proves
    only that DNS pointed somewhere; the pin proves which certificate it was.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert_file, key_file)
    context.load_verify_locations(ca_file)
    return context


def common_name(peer_cert):
    """The CN of a certificate as `SSLSocket.getpeercert()` returns it."""
    for rdn in (peer_cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None
