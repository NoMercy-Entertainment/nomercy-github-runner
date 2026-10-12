import json
import socket
import ssl
import uuid
from pathlib import Path

import pytest

from control.federation import Refused
from control import federation_direct as direct
from tests.federation_http_harness import SOURCE, certificates
from tests.test_federation_direct import IncrementalTLSServer


def configure(monkeypatch, origin=SOURCE, address="10.203.0.2", ca_file=None):
    ca_file = ca_file or str(Path("peer-ca.pem").resolve())
    monkeypatch.setenv("FEDERATION_DIRECT_PRIVATE_PEERS", json.dumps({origin: {"address": address, "ca_file": ca_file}}))


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "0.0.0.0", "8.8.8.8", "::1", "fd00::1", "192.168.2.0/24"])
def test_private_configuration_refuses_non_rfc1918_hosts_and_ranges(monkeypatch, address):
    configure(monkeypatch, address=address)
    with pytest.raises(Refused):
        direct.private_peer(SOURCE)


@pytest.mark.parametrize("origin,ca", [("http://peer.internal", "/data/peer.pem"), ("https://peer.internal:444", "/data/peer.pem"),
                                     ("https://192.168.2.60", "/data/peer.pem"), (SOURCE, "relative.pem")])
def test_private_configuration_requires_exact_https_hostname_and_absolute_ca(monkeypatch, origin, ca):
    configure(monkeypatch, origin=origin, ca_file=ca)
    with pytest.raises(Refused):
        direct.private_peer(origin)


@pytest.mark.parametrize("raw", ["[]", "null", "{", '{"https://peer.internal":{"address":"10.203.0.2","ca_file":"/data/ca.pem","port":9200}}'])
def test_invalid_private_configuration_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("FEDERATION_DIRECT_PRIVATE_PEERS", raw)
    with pytest.raises(Refused):
        direct.private_peer(SOURCE)


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_real_private_tls_pins_destination_and_verifies_hostname(tmp_path, monkeypatch, wrong_identity):
    certificate, private = certificates(tmp_path)
    server = IncrementalTLSServer(certificate, private, None)
    target = "https://other.dashboard.test" if wrong_identity else SOURCE
    configure(monkeypatch, origin=target, ca_file=certificate)
    create = socket.create_connection
    destinations = []

    def connect(destination, **kwargs):
        destinations.append(destination)
        assert destination == ("10.203.0.2", 443)
        return create(("127.0.0.1", server.port), **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(direct, "public_addresses", lambda host: pytest.fail("A pinned private peer must not resolve DNS"))
    token = "nrf_" + str(uuid.uuid4()) + "." + "a" * 43
    try:
        if wrong_identity:
            with pytest.raises(ssl.SSLCertVerificationError):
                direct._http_exchange(target, "capacity", token)
            assert not server.entered.is_set()  # No bearer sent to an unverified TLS identity.
        else:
            assert json.loads(direct._http_exchange(target, "capacity", token))["pad"] == "x" * 128
            assert server.entered.is_set()
        assert destinations == [("10.203.0.2", 443)]
    finally:
        server.close()


def test_another_origin_and_removed_configuration_cannot_reach_private_addresses(monkeypatch):
    configure(monkeypatch)
    from control import federation_broker
    monkeypatch.setattr(federation_broker.socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, ("10.203.0.2", 443))])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("An unapproved private endpoint must not connect"))
    token = "nrf_" + str(uuid.uuid4()) + "." + "a" * 43
    with pytest.raises(Refused, match="public"):
        direct._http_exchange("https://unapproved.dashboard.test", "capacity", token)
    monkeypatch.delenv("FEDERATION_DIRECT_PRIVATE_PEERS")
    with pytest.raises(Refused, match="public"):
        direct._http_exchange(SOURCE, "capacity", token)
