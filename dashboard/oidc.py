"""OpenID Connect client for the NoMercy Keycloak realm.

Deliberately small, and small for a stated reason rather than by omission.

The flow is authorization code + PKCE S256 with a confidential client, and the
code is redeemed in a direct server-to-server TLS call to the token endpoint.
Identity claims are then read from `userinfo` over that same direct call - the
`id_token` is never parsed. That is the case OIDC Core 3.1.3.7 covers: when
the token arrives by direct communication with the token endpoint, TLS server
authentication stands in for verifying its signature.

So there is no JWT verifier and no JWKS cache here, and that is not an
oversight. PKCE binds the code to this login attempt, the client secret means
only we can redeem it, `state` covers CSRF (checked by the caller, which owns
the session), and TLS authenticates the issuer. Nothing is left for a
signature check to catch.

If the flow ever changes shape - an implicit or hybrid response, or an
id_token taken from anywhere but the token endpoint - that reasoning expires
and a real verifier becomes mandatory.

This module knows who someone is. It has no idea who is allowed; that is
users.py.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

SCOPE = "openid profile email"

# Discovery changes on the order of never. Long enough to keep it off the
# login path, short enough that a realm move is picked up the same day.
DISCOVERY_TTL = 3600

_discovery_cache = {}


class OIDCError(Exception):
    """Anything that means we did not end up with a trustworthy identity."""


def _http_json(url, data=None, headers=None, timeout=15):
    """POST a form (when data is given) or GET, and parse JSON.

    Errors carry the URL but never the body: the token request body holds the
    client secret and the response holds an access token, and this string ends
    up in logs.
    """
    hdrs = {"Accept": "application/json",
            "User-Agent": "nomercy-runner-dashboard"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Keycloak answers a bad grant with 400 and a JSON error object.
        # Returning it lets the caller say which error; raising here would
        # flatten every failure into "the IdP is down".
        try:
            return json.loads(e.read().decode())
        except Exception:      # noqa: BLE001
            raise OIDCError(f"{url}: HTTP {e.code}") from None
    except Exception as e:     # noqa: BLE001
        raise OIDCError(f"{url}: {e}") from None


class OIDC:
    def __init__(self, issuer, client_id, client_secret, redirect_uri):
        self.issuer = (issuer or "").rstrip("/")
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        # Pinned by the caller from configuration, never built from request
        # headers: this app is also reachable on a LAN address where Host and
        # X-Forwarded-Host are attacker-controlled, and a forged one would
        # send the authorization code to another host.
        self.redirect_uri = redirect_uri or ""

    def configured(self):
        return bool(self.issuer and self.client_id
                    and self.client_secret and self.redirect_uri)

    # ------------------------------------------------------------- discovery
    def _discovery(self):
        hit = _discovery_cache.get(self.issuer)
        if hit and time.monotonic() - hit[0] < DISCOVERY_TTL:
            return hit[1]

        doc = _http_json(self.issuer + "/.well-known/openid-configuration")
        missing = [k for k in ("authorization_endpoint", "token_endpoint",
                               "userinfo_endpoint") if not (doc or {}).get(k)]
        if missing:
            # Not cached: a half-answer now must not be replayed for an hour.
            raise OIDCError(f"discovery is missing {', '.join(missing)}")
        _discovery_cache[self.issuer] = (time.monotonic(), doc)
        return doc

    # ------------------------------------------------------------- authorize
    def authorize_url(self):
        """(url, state, verifier). The caller stores state and verifier in the
        session and must compare state on the way back."""
        doc = self._discovery()

        # PKCE: 43-128 unreserved characters. token_urlsafe(64) lands at 86.
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)

        query = urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return f"{doc['authorization_endpoint']}?{query}", state, verifier

    # -------------------------------------------------------------- exchange
    def exchange(self, code, verifier):
        """Redeem the code and return the identity claims.

        Raises rather than returning a blank identity: every failure here has
        to reach a caller that stops, because the alternative is signing
        somebody in as nobody.
        """
        doc = self._discovery()

        token = _http_json(doc["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code_verifier": verifier,
        })
        access = (token or {}).get("access_token")
        if not access:
            raise OIDCError(
                f"token exchange refused: {(token or {}).get('error', 'no access_token')}")

        claims = _http_json(doc["userinfo_endpoint"],
                            headers={"Authorization": f"Bearer {access}"})
        if not (claims or {}).get("sub"):
            # sub is the allowlist key. Without it there is no identity to
            # authorise - and an empty key would collide with every other
            # subless login.
            raise OIDCError("userinfo returned no sub")
        return claims
