"""The plain-HTTP warning must describe how the request actually arrived.

The dashboard is reachable two ways: directly on http://192.168.178.19:9200,
and through a reverse proxy that terminates TLS at
https://gh-runners.phillippepelzer.me. A warning hardcoded into the login page
is therefore wrong half the time - and a security notice that is visibly wrong
is worse than none, because it teaches the reader to ignore it.
"""
WARNING = "Plain HTTP"


def test_the_warning_shows_when_the_request_arrived_over_plain_http(anon_client):
    body = anon_client.get("/login").get_data(as_text=True)
    assert WARNING in body


def test_the_warning_is_gone_behind_a_tls_terminating_proxy(anon_client):
    body = anon_client.get(
        "/login", headers={"X-Forwarded-Proto": "https"}).get_data(as_text=True)
    assert WARNING not in body


def test_a_forwarded_proto_of_http_still_warns(anon_client):
    """A proxy that does not terminate TLS must not silence the warning."""
    body = anon_client.get(
        "/login", headers={"X-Forwarded-Proto": "http"}).get_data(as_text=True)
    assert WARNING in body
