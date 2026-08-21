"""The OIDC client, driven against a stubbed IdP. No network in tests.

Shape of the flow, and why it is allowed to be this small: authorization code
with PKCE S256 and a confidential client, with the code exchanged in a direct
server-to-server TLS call. Claims come from userinfo over that same direct
call, not from parsing an id_token - which is the case OIDC Core 3.1.3.7
covers, and the reason no JWT verifier is pulled in. See the design doc.
"""
import base64
import hashlib
import urllib.parse

import pytest

import oidc

ISSUER = "https://auth.nomercy.tv/realms/NoMercyTV"

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
    "token_endpoint": ISSUER + "/protocol/openid-connect/token",
    "userinfo_endpoint": ISSUER + "/protocol/openid-connect/userinfo",
}

CLAIMS = {
    "sub": "b3f1-uuid",
    "preferred_username": "phill",
    "name": "Phil Pelzer",
    "email": "phil@example.invalid",
}


@pytest.fixture
def idp(monkeypatch):
    """A stub IdP that records what it was asked."""
    calls = []

    def http_json(url, data=None, headers=None, timeout=15):
        calls.append({"url": url, "data": data, "headers": headers or {}})
        if url.endswith("/.well-known/openid-configuration"):
            return DISCOVERY
        if url == DISCOVERY["token_endpoint"]:
            return {"access_token": "at-123", "token_type": "Bearer"}
        if url == DISCOVERY["userinfo_endpoint"]:
            return CLAIMS
        raise AssertionError(f"unexpected call to {url}")

    monkeypatch.setattr(oidc, "_http_json", http_json)
    monkeypatch.setattr(oidc, "_discovery_cache", {})
    client = oidc.OIDC(ISSUER, "phillippepelzer.me", "s3cret",
                       "https://gh-runners.phillippepelzer.me/callback")
    return client, calls


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# ------------------------------------------------------------------ authorize

def test_the_authorize_url_carries_what_keycloak_needs(idp):
    client, _ = idp
    url, state, verifier = client.authorize_url()
    q = _query(url)
    assert url.startswith(DISCOVERY["authorization_endpoint"])
    assert q["client_id"] == "phillippepelzer.me"
    assert q["redirect_uri"] == "https://gh-runners.phillippepelzer.me/callback"
    assert q["response_type"] == "code"
    assert "openid" in q["scope"]
    assert q["state"] == state


def test_the_challenge_is_the_s256_hash_of_the_verifier(idp):
    """If these ever drift, Keycloak rejects the exchange and login just fails."""
    client, _ = idp
    url, _, verifier = client.authorize_url()
    q = _query(url)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert q["code_challenge_method"] == "S256"


def test_every_login_attempt_gets_a_fresh_state_and_verifier(idp):
    client, _ = idp
    _, s1, v1 = client.authorize_url()
    _, s2, v2 = client.authorize_url()
    assert s1 != s2 and v1 != v2


# ------------------------------------------------------------------- exchange

def test_exchange_returns_the_identity_claims(idp):
    client, _ = idp
    assert client.exchange("the-code", "the-verifier") == CLAIMS


def test_exchange_sends_the_verifier_and_the_client_credentials(idp):
    client, calls = idp
    client.exchange("the-code", "the-verifier")
    token_call = next(c for c in calls if c["url"] == DISCOVERY["token_endpoint"])
    assert token_call["data"]["code"] == "the-code"
    assert token_call["data"]["code_verifier"] == "the-verifier"
    assert token_call["data"]["grant_type"] == "authorization_code"
    # Confidential client: without this Keycloak refuses the exchange.
    assert token_call["data"]["client_secret"] == "s3cret"


def test_exchange_presents_the_access_token_to_userinfo(idp):
    client, calls = idp
    client.exchange("the-code", "the-verifier")
    ui = next(c for c in calls if c["url"] == DISCOVERY["userinfo_endpoint"])
    assert ui["headers"]["Authorization"] == "Bearer at-123"


def test_a_token_endpoint_without_an_access_token_is_an_error(idp, monkeypatch):
    client, _ = idp

    def http_json(url, data=None, headers=None, timeout=15):
        if url.endswith("/.well-known/openid-configuration"):
            return DISCOVERY
        return {"error": "invalid_grant"}

    monkeypatch.setattr(oidc, "_http_json", http_json)
    with pytest.raises(oidc.OIDCError):
        client.exchange("stale-code", "the-verifier")


def test_claims_without_a_sub_are_refused(idp, monkeypatch):
    """sub is the allowlist key. No sub means no identity, not a blank one."""
    client, _ = idp

    def http_json(url, data=None, headers=None, timeout=15):
        if url.endswith("/.well-known/openid-configuration"):
            return DISCOVERY
        if url == DISCOVERY["token_endpoint"]:
            return {"access_token": "at-123"}
        return {"preferred_username": "nobody"}

    monkeypatch.setattr(oidc, "_http_json", http_json)
    with pytest.raises(oidc.OIDCError):
        client.exchange("the-code", "the-verifier")


# ------------------------------------------------------------------ discovery

def test_discovery_is_fetched_once_and_reused(idp):
    client, calls = idp
    client.authorize_url()
    client.authorize_url()
    client.exchange("c", "v")
    disco = [c for c in calls if c["url"].endswith("openid-configuration")]
    assert len(disco) == 1


# --------------------------------------------------------------- configuration

def test_a_client_missing_settings_reports_itself_unconfigured():
    assert oidc.OIDC("", "", "", "").configured() is False
    assert oidc.OIDC(ISSUER, "id", "secret", "https://x/callback").configured()
