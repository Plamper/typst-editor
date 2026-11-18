"""OpenID Connect authentication and user provisioning."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import jwt
from fastapi import Request

from backend import auth, config, db
from backend.constants import NAME_RE
from backend.services.locks import _named_lock
from backend.services.proxy import _proxy_trusted

log = logging.getLogger("typst.main")

STATE_COOKIE = "typst_oidc_state"
STATE_TTL = 600
CACHE_TTL_S = 3600.0
ALLOWED_ALGS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
SAFE_ERRORS = frozenset({"failed", "access_denied", "registration_disabled", "state_invalid", "rate_limited"})

_STATE_KEY = secrets.token_bytes(32)
_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_JWKS_MISS_KIDS: dict[str, set[str]] = {}


class OidcError(Exception):
    """OIDC protocol or policy failure mapped to a safe redirect error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in SAFE_ERRORS else "failed"
        self.message = message


def is_enabled(cfg: config.Config | None = None) -> bool:
    return config.oidc_enabled(cfg)


def oidc_startup_check(cfg: config.Config) -> None:
    if bool(cfg.OIDC_ISSUER_URL) != bool(cfg.OIDC_CLIENT_ID):
        log.warning("OIDC partially configured (need both OIDC_ISSUER_URL and OIDC_CLIENT_ID); OIDC disabled")
    if cfg.DISABLE_PASSWORD_LOGIN and not is_enabled(cfg):
        log.error("refusing to start: DISABLE_PASSWORD_LOGIN=true requires OIDC_ISSUER_URL and OIDC_CLIENT_ID")
        raise SystemExit(1)


def clear_cache() -> None:
    _DISCOVERY_CACHE.clear()
    _JWKS_CACHE.clear()
    _JWKS_MISS_KIDS.clear()


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


def redirect_uri(req: Request, cfg: config.Config) -> str:
    if cfg.OIDC_REDIRECT_URI:
        return cfg.OIDC_REDIRECT_URI
    proto = req.url.scheme or "http"
    host = ""
    if _proxy_trusted(req):
        xfp = (req.headers.get("x-forwarded-proto", "") or "").split(",")[-1].strip().lower()
        if xfp in ("http", "https"):
            proto = xfp
        host = (req.headers.get("x-forwarded-host", "") or "").split(",")[-1].strip()
    if not host:
        host = (req.headers.get("host", "") or "").split(",")[-1].strip()
    if not host:
        host = req.url.netloc
    if not host or any(c in host for c in "/\\@ \t\r\n"):
        raise OidcError("failed", "Invalid host header")
    return f"{proto}://{host}/api/oidc/callback"


def _sign_state(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_STATE_KEY, b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_state(cookie_val: str, query_state: str) -> dict[str, Any]:
    if not cookie_val or not query_state or "." not in cookie_val:
        raise OidcError("state_invalid", "Missing OIDC state")
    b64, sig = cookie_val.split(".", 1)
    expected = hmac.new(_STATE_KEY, b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise OidcError("state_invalid", "Invalid OIDC state signature")
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise OidcError("state_invalid", "Malformed OIDC state") from exc
    if not isinstance(data, dict):
        raise OidcError("state_invalid", "Malformed OIDC state")
    if int(data.get("exp", 0)) < time.time():
        raise OidcError("state_invalid", "Expired OIDC state")
    st = data.get("state")
    if not isinstance(st, str) or not hmac.compare_digest(st, query_state):
        raise OidcError("state_invalid", "Mismatched OIDC state")
    if not isinstance(data.get("nonce"), str) or not isinstance(data.get("cv"), str):
        raise OidcError("state_invalid", "Incomplete OIDC state")
    return data


async def _fetch_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        async with _http_client() as client:
            resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise OidcError("failed", f"OIDC HTTP request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise OidcError("failed", "OIDC endpoint returned non-object JSON")
    return data


async def _get_discovery(issuer_url: str) -> dict[str, Any]:
    base = issuer_url.rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise OidcError("failed", "Invalid OIDC issuer URL")
    now = time.monotonic()
    cached = _DISCOVERY_CACHE.get(base)
    if cached and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    doc = await _fetch_json(f"{base}/.well-known/openid-configuration")
    for req_key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(doc.get(req_key), str) or not doc[req_key]:
            raise OidcError("failed", f"OIDC discovery missing {req_key}")
    if str(doc["issuer"]).rstrip("/") != base:
        raise OidcError("failed", "OIDC discovery issuer mismatch")
    _DISCOVERY_CACHE[base] = (now, doc)
    return doc


async def _get_jwks(jwks_uri: str, *, force: bool = False) -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _JWKS_CACHE.get(jwks_uri)
    if not force and cached and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    data = await _fetch_json(jwks_uri)
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise OidcError("failed", "OIDC JWKS missing keys")
    clean = [k for k in keys if isinstance(k, dict)]
    if not clean:
        raise OidcError("failed", "OIDC JWKS empty")
    _JWKS_CACHE[jwks_uri] = (now, clean)
    _JWKS_MISS_KIDS.pop(jwks_uri, None)
    return clean


async def start_login(req: Request, cfg: config.Config, join: str = "") -> tuple[str, str]:
    disc = await _get_discovery(cfg.OIDC_ISSUER_URL)
    cb = redirect_uri(req, cfg)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    scopes = cfg.OIDC_SCOPES.split()
    if "openid" not in scopes:
        scopes.insert(0, "openid")
    if any(g.strip() for g in cfg.OIDC_ALLOWED_GROUPS.split(",")) and "groups" not in scopes:
        scopes.append("groups")

    parts = urlsplit(str(disc["authorization_endpoint"]))
    params = parse_qsl(parts.query, keep_blank_values=True)
    params.extend([
        ("response_type", "code"),
        ("client_id", cfg.OIDC_CLIENT_ID),
        ("redirect_uri", cb),
        ("scope", " ".join(scopes)),
        ("state", state),
        ("nonce", nonce),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
    ])
    auth_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
    cookie_val = _sign_state({
        "state": state,
        "nonce": nonce,
        "cv": verifier,
        "join": join[:200],
        "exp": int(time.time()) + STATE_TTL,
    })
    return auth_url, cookie_val


async def _exchange_code(
    disc: dict[str, Any],
    cfg: config.Config,
    code: str,
    cb: str,
    verifier: str,
) -> dict[str, Any]:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cb,
        "client_id": cfg.OIDC_CLIENT_ID,
        "code_verifier": verifier,
    }
    headers = {"Accept": "application/json"}
    if cfg.OIDC_CLIENT_SECRET:
        methods = disc.get("token_endpoint_auth_methods_supported")
        use_post = (
            isinstance(methods, list)
            and "client_secret_post" in methods
            and "client_secret_basic" not in methods
        )
        if use_post:
            data["client_secret"] = cfg.OIDC_CLIENT_SECRET
        else:
            raw = f"{quote(cfg.OIDC_CLIENT_ID, safe='')}:{quote(cfg.OIDC_CLIENT_SECRET, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    try:
        async with _http_client() as client:
            resp = await client.post(str(disc["token_endpoint"]), data=data, headers=headers)
        resp.raise_for_status()
        tok = resp.json()
    except Exception as exc:
        raise OidcError("failed", f"Token exchange failed: {exc}") from exc
    if not isinstance(tok, dict) or not isinstance(tok.get("id_token"), str) or not tok["id_token"]:
        raise OidcError("failed", "Token response missing id_token")
    return tok


async def _verify_id_token(
    id_token: str,
    disc: dict[str, Any],
    cfg: config.Config,
    expected_nonce: str,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError as exc:
        raise OidcError("failed", f"Malformed id_token header: {exc}") from exc
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in ALLOWED_ALGS:
        raise OidcError("failed", f"Disallowed id_token alg: {alg!r}")
    supported = disc.get("id_token_signing_alg_values_supported")
    if isinstance(supported, list) and supported and alg not in supported:
        raise OidcError("failed", f"Unsupported id_token alg: {alg!r}")

    jwks_uri = str(disc["jwks_uri"])
    kid = header.get("kid")
    keys = await _get_jwks(jwks_uri)
    if isinstance(kid, str) and kid:
        candidates = [k for k in keys if k.get("kid") == kid]
        if not candidates and kid not in _JWKS_MISS_KIDS.get(jwks_uri, ()):
            keys = await _get_jwks(jwks_uri, force=True)
            candidates = [k for k in keys if k.get("kid") == kid]
            if not candidates:
                misses = _JWKS_MISS_KIDS.setdefault(jwks_uri, set())
                if len(misses) >= 128:
                    misses.clear()
                misses.add(kid)
    else:
        candidates = [
            k for k in keys
            if k.get("use", "sig") == "sig" and (not k.get("alg") or k.get("alg") == alg)
        ]
    if not candidates:
        raise OidcError("failed", "No matching JWK for id_token")

    claims: dict[str, Any] | None = None
    last_err: Exception | None = None
    for k_dict in candidates:
        try:
            jwk = jwt.PyJWK.from_dict(k_dict)
            claims = jwt.decode(
                id_token,
                jwk,
                algorithms=[alg],
                audience=cfg.OIDC_CLIENT_ID,
                issuer=str(disc["issuer"]),
                leeway=30,
                options={"require": ["exp", "sub"]},
            )
            break
        except Exception as exc:
            last_err = exc
    if claims is None:
        raise OidcError("failed", f"Invalid id_token signature/claims: {last_err}")

    aud = claims.get("aud")
    if (isinstance(aud, list) and len(aud) > 1) or "azp" in claims:
        if claims.get("azp") != cfg.OIDC_CLIENT_ID:
            raise OidcError("failed", "id_token azp mismatch")
    if "iat" in claims:
        iat = claims["iat"]
        if not isinstance(iat, (int, float)) or iat > time.time() + 60:
            raise OidcError("failed", "id_token iat invalid")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
        raise OidcError("failed", "id_token nonce mismatch")
    return claims


async def _maybe_merge_userinfo(
    claims: dict[str, Any],
    tok: dict[str, Any],
    disc: dict[str, Any],
    cfg: config.Config,
) -> dict[str, Any]:
    need_user = cfg.OIDC_USERNAME_CLAIM not in claims
    need_groups = bool(cfg.OIDC_ALLOWED_GROUPS) and "groups" not in claims
    endpoint = disc.get("userinfo_endpoint")
    access_token = tok.get("access_token")
    if not (need_user or need_groups) or not isinstance(endpoint, str) or not endpoint or not isinstance(access_token, str) or not access_token:
        return claims
    ui = await _fetch_json(endpoint, headers={"Authorization": f"Bearer {access_token}"})
    if ui.get("sub") != claims.get("sub"):
        raise OidcError("failed", "Userinfo sub mismatch")
    merged = dict(claims)
    for k, v in ui.items():
        if k not in merged:
            merged[k] = v
    return merged


def _check_groups(claims: dict[str, Any], cfg: config.Config) -> None:
    if not cfg.OIDC_ALLOWED_GROUPS:
        return
    allowed = {g.strip() for g in cfg.OIDC_ALLOWED_GROUPS.split(",") if g.strip()}
    if not allowed:
        return
    raw = claims.get("groups")
    if isinstance(raw, list):
        have = {str(g).strip() for g in raw}
    elif isinstance(raw, str):
        have = {g.strip() for g in raw.split(",") if g.strip()}
    else:
        have = set()
    if not (have & allowed):
        raise OidcError("access_denied", "User not in allowed OIDC groups")


def sanitize_username(raw: str) -> str:
    s = (raw or "").strip()
    if "@" in s:
        s = s.split("@", 1)[0]
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_-")
    if len(s) < 2:
        s = f"u_{s}" if s else "user"
    s = s[:20].strip("_-")
    if len(s) < 2 or not re.fullmatch(NAME_RE, s):
        return "user"
    return s


def _pick_username_claim(claims: dict[str, Any], cfg: config.Config) -> str:
    for key in (cfg.OIDC_USERNAME_CLAIM, "preferred_username", "nickname", "email", "name"):
        val = claims.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return "user"


def _resolve_or_create_user(
    issuer: str,
    sub: str,
    claims: dict[str, Any],
    cfg: config.Config,
) -> str:
    con = db.connect()
    try:
        row = con.execute(
            "SELECT name FROM users WHERE oidc_issuer=? AND oidc_sub=?",
            (issuer, sub),
        ).fetchone()
        if row:
            return str(row["name"])
    finally:
        con.close()

    if not cfg.OIDC_AUTO_REGISTER:
        raise OidcError("registration_disabled", "OIDC auto-registration disabled")

    base = sanitize_username(_pick_username_claim(claims, cfg))
    with _named_lock("register"):
        with db.tx() as con:
            row = con.execute(
                "SELECT name FROM users WHERE oidc_issuer=? AND oidc_sub=?",
                (issuer, sub),
            ).fetchone()
            if row:
                return str(row["name"])
            candidate = base
            n = 2
            while con.execute("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", (candidate,)).fetchone():
                suffix = f"-{n}"
                candidate = f"{base[:20 - len(suffix)]}{suffix}"
                n += 1
            auth.provision_user(
                con,
                candidate,
                auth.OIDC_HASH_SENTINEL,
                oidc_issuer=issuer,
                oidc_sub=sub,
            )
            return candidate


async def complete_login(req: Request, cfg: config.Config) -> tuple[str, str]:
    err = req.query_params.get("error", "")
    if err:
        code = "access_denied" if err == "access_denied" else "failed"
        raise OidcError(code, f"IdP returned error: {err}")

    code = req.query_params.get("code", "")
    state = req.query_params.get("state", "")
    if not code or not state:
        raise OidcError("state_invalid", "Missing code or state")

    st_data = _verify_state(req.cookies.get(STATE_COOKIE, ""), state)
    disc = await _get_discovery(cfg.OIDC_ISSUER_URL)
    cb = redirect_uri(req, cfg)
    tok = await _exchange_code(disc, cfg, code, cb, st_data["cv"])
    claims = await _verify_id_token(tok["id_token"], disc, cfg, st_data["nonce"])
    claims = await _maybe_merge_userinfo(claims, tok, disc, cfg)
    _check_groups(claims, cfg)

    issuer = str(disc["issuer"])
    sub = str(claims["sub"])
    username = await asyncio.to_thread(_resolve_or_create_user, issuer, sub, claims, cfg)
    join_tok = st_data.get("join")
    return username, join_tok if isinstance(join_tok, str) else ""
