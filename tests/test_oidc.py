"""Tests for OpenID Connect authentication, provisioning, and passwordless account management."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from conftest import make_doc, register_user
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from backend import db
from backend.services import oidc as oidc_svc


@dataclass
class _MockIdP:
    issuer: str
    client_id: str = "typst-client"
    client_secret: str = "super-secret"
    alg: str = "RS256"
    kid: str = "key-1"
    auth_methods: list[str] | None = None
    id_token_claims: dict[str, Any] | None = None
    userinfo_claims: dict[str, Any] | None = None
    token_calls: list[dict[str, Any]] = None  # type: ignore[assignment]
    jwks_calls: int = 0
    _priv: Any = None
    _jwk: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.token_calls = []
        self.rotate_key(self.kid, self.alg)

    def rotate_key(self, kid: str, alg: str | None = None) -> None:
        self.kid = kid
        if alg:
            self.alg = alg
        if self.alg.startswith("ES"):
            self._priv = ec.generate_private_key(ec.SECP256R1())
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self._priv.public_key()))
        else:
            self._priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._priv.public_key()))
        jwk["kid"] = self.kid
        jwk["use"] = "sig"
        jwk["alg"] = self.alg
        self._jwk = jwk

    def build_transport(self) -> httpx.MockTransport:
        base = self.issuer.rstrip("/")

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == f"{base}/.well-known/openid-configuration":
                doc: dict[str, Any] = {
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "userinfo_endpoint": f"{base}/userinfo",
                    "jwks_uri": f"{base}/jwks.json",
                    "id_token_signing_alg_values_supported": ["RS256", "ES256"],
                }
                if self.auth_methods is not None:
                    doc["token_endpoint_auth_methods_supported"] = self.auth_methods
                return httpx.Response(200, json=doc)

            if url == f"{base}/jwks.json":
                self.jwks_calls += 1
                return httpx.Response(200, json={"keys": [self._jwk]})

            if url == f"{base}/token":
                body = request.content.decode("utf-8")
                form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
                self.token_calls.append({
                    "form": form,
                    "authorization": request.headers.get("authorization", ""),
                })
                now = int(time.time())
                claims: dict[str, Any] = {
                    "iss": self.issuer,
                    "sub": "sub-123",
                    "aud": self.client_id,
                    "exp": now + 300,
                    "iat": now,
                    "nonce": form.get("_nonce", ""),
                    "preferred_username": "alice",
                }
                if self.id_token_claims:
                    claims.update(self.id_token_claims)
                id_tok = jwt.encode(claims, self._priv, algorithm=self.alg, headers={"kid": self.kid})
                return httpx.Response(200, json={
                    "access_token": "at-xyz",
                    "token_type": "Bearer",
                    "id_token": id_tok,
                })

            if url == f"{base}/userinfo":
                ui = dict(self.userinfo_claims or {"sub": "sub-123"})
                return httpx.Response(200, json=ui)

            return httpx.Response(404, json={"error": "not_found"})

        return httpx.MockTransport(handler)


@pytest.fixture()
def oidc_env(monkeypatch):
    oidc_svc.clear_cache()

    def setup(idp: _MockIdP, **env_overrides: str) -> _MockIdP:
        monkeypatch.setenv("OIDC_ISSUER_URL", idp.issuer)
        monkeypatch.setenv("OIDC_CLIENT_ID", idp.client_id)
        monkeypatch.setenv("OIDC_CLIENT_SECRET", idp.client_secret)
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
        transport = idp.build_transport()
        monkeypatch.setattr(
            oidc_svc,
            "_http_client",
            lambda: httpx.AsyncClient(transport=transport, timeout=10.0, follow_redirects=False),
        )
        return idp

    yield setup
    oidc_svc.clear_cache()


def _run_oidc_flow(c, idp: _MockIdP, *, join: str = ""):
    url = f"/api/oidc/login?join={join}" if join else "/api/oidc/login"
    start = c.get(url, follow_redirects=False)
    assert start.status_code == 302, start.text
    parsed = urlparse(start.headers["location"])
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert qs["response_type"] == "code"
    assert qs["client_id"] == idp.client_id
    assert qs["code_challenge_method"] == "S256"
    nonce = qs["nonce"]
    state = qs["state"]

    # Inject expected nonce into id_token unless caller explicitly overrides it.
    orig_claims = dict(idp.id_token_claims or {})
    if "nonce" not in orig_claims:
        idp.id_token_claims = {**orig_claims, "nonce": nonce}
    try:
        cb = c.get(f"/api/oidc/callback?code=auth-code-1&state={state}", follow_redirects=False)
    finally:
        idp.id_token_claims = orig_claims or None
    return cb, qs


def test_auth_config_modes(c, monkeypatch):
    r = c.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json() == {
        "oidc": {"enabled": False, "provider_name": "SSO", "auto_redirect": False},
        "password_login": True,
        "registration": "open",
    }

    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "typst")
    monkeypatch.setenv("OIDC_PROVIDER_NAME", "Kanidm")
    monkeypatch.setenv("OIDC_AUTO_REDIRECT", "true")
    monkeypatch.setenv("DISABLE_PASSWORD_LOGIN", "true")
    r2 = c.get("/api/auth/config")
    assert r2.status_code == 200
    assert r2.json() == {
        "oidc": {"enabled": True, "provider_name": "Kanidm", "auto_redirect": True},
        "password_login": False,
        "registration": "closed",
    }


def test_oidc_pkce_rs256_and_tutorial_provisioning(c, oidc_env):
    idp = oidc_env(_MockIdP("https://idp.example.com", alg="RS256"))
    cb, qs = _run_oidc_flow(c, idp)
    assert cb.status_code == 302
    assert cb.headers["location"] == "/"
    assert "groups" not in qs["scope"].split()

    # Verify PKCE verifier matches S256 challenge and client_secret_basic was sent.
    assert len(idp.token_calls) == 1
    call = idp.token_calls[0]
    verifier = call["form"]["code_verifier"]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert expected_challenge == qs["code_challenge"]
    assert call["authorization"].startswith("Basic ")

    me = c.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"] == "alice"
    assert me.json()["has_password"] is False

    # Starter Tutorial doc created.
    docs = c.get("/api/docs").json()["own"]
    assert any(d["title"] == "Tutorial" for d in docs)


def test_oidc_kanidm_es256_public_client_and_spn_collision(c, oidc_env):
    register_user(c, "alice")
    c.post("/api/logout")

    idp = oidc_env(
        _MockIdP(
            "https://idm.example.com/oauth2/openid/typst",
            client_secret="",
            alg="ES256",
            id_token_claims={"sub": "kanidm-uuid-1", "preferred_username": "alice@idm.example.com"},
        )
    )
    cb, _ = _run_oidc_flow(c, idp)
    assert cb.status_code == 302
    assert cb.headers["location"] == "/"
    assert idp.token_calls[0]["authorization"] == ""
    assert c.get("/api/me").json()["user"] == "alice-2"

    # Second login with same sub but changed claim stays mapped to alice-2.
    c.post("/api/logout")
    idp.id_token_claims = {"sub": "kanidm-uuid-1", "preferred_username": "someone_else@idm.example.com"}
    cb2, _ = _run_oidc_flow(c, idp)
    assert cb2.status_code == 302
    assert c.get("/api/me").json()["user"] == "alice-2"


def test_oidc_authentik_trailing_slash_and_secret_post(c, oidc_env):
    idp = oidc_env(
        _MockIdP(
            "https://auth.example.com/application/o/typst/",
            auth_methods=["client_secret_post"],
            id_token_claims={"sub": "ak-1", "preferred_username": "Bob.Smith"},
        ),
        OIDC_ISSUER_URL="https://auth.example.com/application/o/typst",
    )
    cb, _ = _run_oidc_flow(c, idp)
    assert cb.status_code == 302
    assert idp.token_calls[0]["form"]["client_secret"] == "super-secret"
    assert idp.token_calls[0]["authorization"] == ""
    assert c.get("/api/me").json()["user"] == "Bob_Smith"


def test_oidc_authelia_userinfo_fallback_and_groups(c, oidc_env):
    idp = oidc_env(
        _MockIdP(
            "https://authelia.example.com",
            id_token_claims={"sub": "authelia-1", "preferred_username": None},
            userinfo_claims={"sub": "authelia-1", "preferred_username": "carol", "groups": ["admins", "writers"]},
        ),
        OIDC_ALLOWED_GROUPS="writers, editors",
    )
    # Remove preferred_username from id_token so userinfo fallback triggers.
    idp.id_token_claims = {"sub": "authelia-1"}
    # Note: _MockIdP defaults preferred_username unless explicitly stripped; test via custom transport override below.
    orig_transport = idp.build_transport()

    def strip_claim_transport(request: httpx.Request) -> httpx.Response:
        resp = orig_transport.handle_request(request)
        if str(request.url).endswith("/token"):
            data = resp.json()
            claims = jwt.decode(data["id_token"], options={"verify_signature": False})
            claims.pop("preferred_username", None)
            data["id_token"] = jwt.encode(claims, idp._priv, algorithm=idp.alg, headers={"kid": idp.kid})
            return httpx.Response(200, json=data)
        return resp

    oidc_svc._http_client = lambda: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(strip_claim_transport), timeout=10.0, follow_redirects=False
    )
    cb, qs = _run_oidc_flow(c, idp)
    assert cb.status_code == 302
    assert "groups" in qs["scope"].split()
    assert c.get("/api/me").json()["user"] == "carol"

    # Disallowed group rejected with access_denied.
    c.post("/api/logout")
    idp.userinfo_claims = {"sub": "authelia-2", "preferred_username": "mallory", "groups": ["guests"]}
    idp.id_token_claims = {"sub": "authelia-2"}
    cb_denied, _ = _run_oidc_flow(c, idp)
    assert cb_denied.status_code == 302
    assert cb_denied.headers["location"] == "/?oidc_error=access_denied"
    assert c.get("/api/me").status_code == 401


def test_oidc_state_nonce_and_token_validation_errors(c, oidc_env):
    idp = oidc_env(_MockIdP("https://idp.example.com"), RATE_LOGIN_PER_MIN="50")

    # Missing state cookie / mismatched state.
    r_no_cookie = c.get("/api/oidc/callback?code=x&state=bogus", follow_redirects=False)
    assert r_no_cookie.status_code == 302
    assert r_no_cookie.headers["location"] == "/?oidc_error=state_invalid"

    # Mismatched nonce.
    idp.id_token_claims = {"nonce": "wrong-nonce"}
    cb_nonce, _ = _run_oidc_flow(c, idp)
    assert cb_nonce.headers["location"] == "/?oidc_error=failed"

    # Expired token.
    idp.id_token_claims = {"exp": int(time.time()) - 120}
    cb_exp, _ = _run_oidc_flow(c, idp)
    assert cb_exp.headers["location"] == "/?oidc_error=failed"

    # Wrong audience.
    idp.id_token_claims = {"aud": "other-client"}
    cb_aud, _ = _run_oidc_flow(c, idp)
    assert cb_aud.headers["location"] == "/?oidc_error=failed"

    # Wrong issuer.
    idp.id_token_claims = {"iss": "https://evil.example.com"}
    cb_iss, _ = _run_oidc_flow(c, idp)
    assert cb_iss.headers["location"] == "/?oidc_error=failed"

    # Key rotation refreshes JWKS cache when unknown kid appears.
    idp.id_token_claims = None
    idp.rotate_key("key-2", "ES256")
    cb_rot, _ = _run_oidc_flow(c, idp)
    assert cb_rot.headers["location"] == "/"
    assert idp.jwks_calls >= 2


def test_oidc_rejects_hs256_and_userinfo_sub_mismatch(c, oidc_env):
    idp = oidc_env(_MockIdP("https://idp.example.com"), RATE_LOGIN_PER_MIN="50")
    orig_transport = idp.build_transport()

    def hs256_transport(request: httpx.Request) -> httpx.Response:
        resp = orig_transport.handle_request(request)
        if str(request.url).endswith("/token"):
            data = resp.json()
            claims = jwt.decode(data["id_token"], options={"verify_signature": False})
            data["id_token"] = jwt.encode(claims, "shared-secret-not-jwk-0123456789abcdef", algorithm="HS256")
            return httpx.Response(200, json=data)
        return resp

    oidc_svc._http_client = lambda: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(hs256_transport), timeout=10.0, follow_redirects=False
    )
    cb_hs, _ = _run_oidc_flow(c, idp)
    assert cb_hs.headers["location"] == "/?oidc_error=failed"

    # Userinfo sub mismatch rejected.
    idp2 = oidc_env(_MockIdP("https://idp2.example.com"), RATE_LOGIN_PER_MIN="50")
    idp2.userinfo_claims = {"sub": "different-sub", "preferred_username": "carol"}
    orig2 = idp2.build_transport()

    def strip_user_transport(request: httpx.Request) -> httpx.Response:
        resp = orig2.handle_request(request)
        if str(request.url).endswith("/token"):
            data = resp.json()
            claims = jwt.decode(data["id_token"], options={"verify_signature": False})
            claims.pop("preferred_username", None)
            data["id_token"] = jwt.encode(claims, idp2._priv, algorithm=idp2.alg, headers={"kid": idp2.kid})
            return httpx.Response(200, json=data)
        return resp

    oidc_svc._http_client = lambda: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(strip_user_transport), timeout=10.0, follow_redirects=False
    )
    cb_ui, _ = _run_oidc_flow(c, idp2)
    assert cb_ui.headers["location"] == "/?oidc_error=failed"


def test_oidc_auto_register_disabled(c, oidc_env):
    idp = oidc_env(_MockIdP("https://idp.example.com"), OIDC_AUTO_REGISTER="false")
    cb, _ = _run_oidc_flow(c, idp)
    assert cb.headers["location"] == "/?oidc_error=registration_disabled"
    assert c.get("/api/me").status_code == 401

    # Provisioned user can still sign in when auto-register is disabled.
    with db.tx() as con:
        con.execute(
            "INSERT INTO users (name, hash, oidc_issuer, oidc_sub) VALUES (?, '!oidc', ?, ?)",
            ("existing_sso", idp.issuer, "sub-123"),
        )
    cb2, _ = _run_oidc_flow(c, idp)
    assert cb2.headers["location"] == "/"
    assert c.get("/api/me").json()["user"] == "existing_sso"


def test_oidc_preserves_join_token(c, oidc_env):
    register_user(c, "owner1")
    did = make_doc(c, "SharedDoc")
    join_tok = c.post(f"/api/docs/{did}/invite", json={"role": "editor"}).json()["token"]
    c.post("/api/logout")

    idp = oidc_env(_MockIdP("https://idp.example.com", id_token_claims={"sub": "sub-join", "preferred_username": "joiner"}))
    cb, _ = _run_oidc_flow(c, idp, join=join_tok)
    assert cb.status_code == 302
    assert cb.headers["location"] == f"/?join={join_tok}"
    assert c.post("/api/join", json={"token": join_tok}).status_code == 200
    assert c.get(f"/api/docs/{did}").status_code == 200


def test_disable_password_login_and_startup_guard(c, monkeypatch):
    register_user(c, "alice")
    monkeypatch.setenv("DISABLE_PASSWORD_LOGIN", "true")

    assert c.post("/api/login", json={"username": "alice", "password": "pass1234"}).status_code == 403
    assert c.post("/api/register", json={"username": "bob", "password": "pass1234"}).status_code == 403
    assert c.post("/api/me/password", json={"old": "pass1234", "new": "newpass123"}).status_code == 403

    # Startup check refuses DISABLE_PASSWORD_LOGIN=true without OIDC configured.
    monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    from backend import config as cfg_mod

    with pytest.raises(SystemExit):
        oidc_svc.oidc_startup_check(cfg_mod.load())

    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "typst")
    oidc_svc.oidc_startup_check(cfg_mod.load())


def test_oidc_passwordless_rename_and_delete(c, oidc_env):
    idp = oidc_env(_MockIdP("https://idp.example.com", id_token_claims={"sub": "sub-life", "preferred_username": "alice"}))
    _run_oidc_flow(c, idp)
    assert c.get("/api/me").json()["has_password"] is False

    # Password login fails closed for !oidc sentinel.
    assert c.post("/api/login", json={"username": "alice", "password": "!oidc"}).status_code == 401
    assert c.post("/api/me/password", json={"old": "", "new": "newpass123"}).status_code == 401

    # Rename without password preserves OIDC identity.
    r_ren = c.post("/api/me/name", json={"name": "alice_new", "password": ""})
    assert r_ren.status_code == 200
    assert r_ren.json()["user"] == "alice_new"

    # Case-only rename also works with unique OIDC index.
    r_case = c.post("/api/me/name", json={"name": "Alice_New", "password": ""})
    assert r_case.status_code == 200
    assert r_case.json()["user"] == "Alice_New"

    # Subsequent OIDC login resolves to renamed user.
    c.post("/api/logout")
    _run_oidc_flow(c, idp)
    assert c.get("/api/me").json()["user"] == "Alice_New"

    # Delete without password requires explicit confirm='DELETE'.
    assert c.post("/api/me/delete", json={"password": ""}).status_code == 400
    assert c.post("/api/me/delete", json={"password": "", "confirm": "DELETE"}).status_code == 200
    assert c.get("/api/me").status_code == 401


def test_oidc_security_hardening_host_replay_jwks_and_claims(c, oidc_env):
    idp = oidc_env(
        _MockIdP("https://idp.example.com", id_token_claims={"sub": "sub-sec", "preferred_username": None}),
        userinfo_claims_unused="1",
        RATE_LOGIN_PER_MIN="50",
    )
    idp.userinfo_claims = {"sub": "sub-sec", "preferred_username": "sec_user"}

    # 1. Host header with query/fragment is rejected.
    bad_host = c.get("/api/oidc/login", headers={"host": "testserver?evil=1#frag"}, follow_redirects=False)
    assert bad_host.status_code == 302
    assert bad_host.headers["location"] == "/?oidc_error=failed"

    # 2. Multi-tab concurrent login states both remain valid, and each state is single-use.
    s1 = c.get("/api/oidc/login", follow_redirects=False)
    s2 = c.get("/api/oidc/login", follow_redirects=False)
    qs1 = {k: v[0] for k, v in parse_qs(urlparse(s1.headers["location"]).query).items()}
    qs2 = {k: v[0] for k, v in parse_qs(urlparse(s2.headers["location"]).query).items()}

    # Complete tab 1 (even though tab 2 was started afterwards) + null claim falls back to userinfo.
    idp.id_token_claims = {"sub": "sub-sec", "preferred_username": None, "nonce": qs1["nonce"]}
    cb1 = c.get(f"/api/oidc/callback?code=c1&state={qs1['state']}", follow_redirects=False)
    assert cb1.status_code == 302
    assert cb1.headers["location"] == "/"
    assert c.get("/api/me").json()["user"] == "sec_user"

    # Replaying tab 1's state fails even if cookie is re-sent.
    c.cookies.set(oidc_svc.STATE_COOKIE, s2.cookies[oidc_svc.STATE_COOKIE], path=oidc_svc.STATE_COOKIE_PATH)
    cb1_replay = c.get(f"/api/oidc/callback?code=c1&state={qs1['state']}", follow_redirects=False)
    assert cb1_replay.headers["location"] == "/?oidc_error=state_invalid"

    # Redirect URI host mismatch on callback is rejected.
    c.cookies.set(oidc_svc.STATE_COOKIE, s2.cookies[oidc_svc.STATE_COOKIE], path=oidc_svc.STATE_COOKIE_PATH)
    cb_mismatch = c.get(
        f"/api/oidc/callback?code=c2&state={qs2['state']}",
        headers={"host": "other.example.com"},
        follow_redirects=False,
    )
    assert cb_mismatch.headers["location"] == "/?oidc_error=state_invalid"

    # 3. Empty/whitespace sub claim is rejected.
    idp.id_token_claims = {"sub": "   "}
    cb_empty_sub, _ = _run_oidc_flow(c, idp)
    assert cb_empty_sub.headers["location"] == "/?oidc_error=failed"

    # 4. Unverified email is not used to seed username.
    idp.id_token_claims = {
        "sub": "sub-unverified-email",
        "preferred_username": "",
        "email": "spoofed@example.com",
        "email_verified": False,
        "name": "verified_name",
    }
    idp.userinfo_claims = {"sub": "sub-unverified-email"}
    cb_email, _ = _run_oidc_flow(c, idp)
    assert cb_email.headers["location"] == "/"
    assert c.get("/api/me").json()["user"] == "verified_name"


def test_migrate_v31_idempotent(tmp_path):
    p = tmp_path / "legacy.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE users (name TEXT PRIMARY KEY, hash TEXT NOT NULL);
        CREATE TABLE schema_version (v INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO users (name, hash) VALUES ('legacy', 'pbkdf2$1$00$00');
    """)
    for v in range(1, 31):
        con.execute("INSERT INTO schema_version (v, applied_at) VALUES (?, '2026-01-01')", (v,))
    con.commit()

    db._migrate_v31(con)
    db._migrate_v31(con)
    cols = db._cols(con, "users")
    assert "oidc_issuer" in cols
    assert "oidc_sub" in cols
    row = con.execute("SELECT oidc_issuer, oidc_sub FROM users WHERE name='legacy'").fetchone()
    assert row["oidc_issuer"] == ""
    assert row["oidc_sub"] == ""
    con.close()


def test_oidc_rate_limit_redirects_and_single_login_token(c, oidc_env):
    idp = oidc_env(_MockIdP("https://idp.example.com"), RATE_LOGIN_PER_MIN="1")

    # Full login + callback uses only 1 login token (callback only, not login start).
    cb1, _ = _run_oidc_flow(c, idp)
    assert cb1.status_code == 302
    assert cb1.headers["location"] == "/"

    # Second callback within the same window hits 429 and redirects with safe error code.
    c.post("/api/logout")
    cb2, _ = _run_oidc_flow(c, idp)
    assert cb2.status_code == 302
    assert cb2.headers["location"] == "/?oidc_error=rate_limited"
