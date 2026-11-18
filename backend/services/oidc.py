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
from fastapi import HTTPException, Request

from backend import auth, config, db, deps
from backend.constants import NAME_RE
from backend.services.locks import _named_lock
from backend.services.proxy import _proxy_trusted

log = logging.getLogger("typst.main")

STATE_COOKIE = "typst_oidc_state"
STATE_COOKIE_PATH = "/api/oidc"
STATE_TTL = 600
CACHE_TTL_S = 3600.0
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_STATE_FLOWS = 4
_MAX_CONSUMED_STATES = 2048
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9a-fA-F:]+\])(?::(\d{1,5}))?$")
_JOIN_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
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
_CONSUMED_STATES: dict[str, float] = {}


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
    if is_enabled(cfg):
        if cfg.OIDC_REDIRECT_URI and not cfg.OIDC_REDIRECT_URI.startswith(("https://", "http://")):
            log.error("refusing to start: OIDC_REDIRECT_URI must start with https:// or http://")
            raise SystemExit(1)
        if not cfg.OIDC_REDIRECT_URI:
            log.warning("OIDC_REDIRECT_URI is not set: callback URL will be derived from the request Host header; set OIDC_REDIRECT_URI in production")


def clear_cache() -> None:
    _DISCOVERY_CACHE.clear()
    _JWKS_CACHE.clear()
    _JWKS_MISS_KIDS.clear()
    _CONSUMED_STATES.clear()


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


def _valid_host(host: str) -> bool:
    if not host:
        return False
    m = _HOST_RE.fullmatch(host)
    if not m:
        return False
    port = m.group(1)
    if port is not None and not 1 <= int(port) <= 65535:
        return False
    return True


def _validate_endpoint_url(url: str, *, allow_http: bool, name: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise OidcError("failed", f"Missing OIDC {name}")
    u = url.strip()
    parts = urlsplit(u)
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in allowed_schemes or not _valid_host(parts.netloc):
        raise OidcError("failed", f"Invalid OIDC {name} URL")
    return u


def redirect_uri(req: Request, cfg: config.Config) -> str:
    if cfg.OIDC_REDIRECT_URI:
        return _validate_endpoint_url(cfg.OIDC_REDIRECT_URI, allow_http=True, name="redirect_uri")
    proto = (req.url.scheme or "http").lower()
    if proto not in ("http", "https"):
        raise OidcError("failed", "Invalid request scheme")
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
    if not _valid_host(host):
        raise OidcError("failed", "Invalid host header")
    return f"{proto}://{host}/api/oidc/callback"


def _sign_state(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_STATE_KEY, b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _decode_signed_state(cookie_val: str) -> dict[str, Any] | None:
    if not cookie_val or "." not in cookie_val:
        return None
    b64, sig = cookie_val.split(".", 1)
    expected = hmac.new(_STATE_KEY, b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _record_consumed_state(state: str, exp: float) -> None:
    now = time.time()
    if len(_CONSUMED_STATES) >= _MAX_CONSUMED_STATES:
        expired = [k for k, v in _CONSUMED_STATES.items() if v < now]
        for k in expired:
            _CONSUMED_STATES.pop(k, None)
        if len(_CONSUMED_STATES) >= _MAX_CONSUMED_STATES:
            _CONSUMED_STATES.clear()
    _CONSUMED_STATES[state] = exp


def _verify_state(cookie_val: str, query_state: str) -> dict[str, Any]:
    if not cookie_val or not query_state or "." not in cookie_val:
        raise OidcError("state_invalid", "Missing OIDC state")
    data = _decode_signed_state(cookie_val)
    if data is None:
        raise OidcError("state_invalid", "Invalid OIDC state")
    now = time.time()
    candidates: list[dict[str, Any]] = [data]
    prev = data.get("prev")
    if isinstance(prev, list):
        candidates.extend(p for p in prev if isinstance(p, dict))
    matched: dict[str, Any] | None = None
    for entry in candidates:
        st = entry.get("state")
        if isinstance(st, str) and hmac.compare_digest(st, query_state):
            matched = entry
            break
    if matched is None:
        raise OidcError("state_invalid", "Mismatched OIDC state")
    try:
        exp = int(matched.get("exp", 0))
    except (ValueError, TypeError) as exc:
        raise OidcError("state_invalid", "Malformed OIDC state") from exc
    if exp < now:
        raise OidcError("state_invalid", "Expired OIDC state")
    if not isinstance(matched.get("nonce"), str) or not isinstance(matched.get("cv"), str):
        raise OidcError("state_invalid", "Incomplete OIDC state")
    st_str = str(matched["state"])
    if st_str in _CONSUMED_STATES and _CONSUMED_STATES[st_str] >= now:
        raise OidcError("state_invalid", "Replayed OIDC state")
    _record_consumed_state(st_str, float(exp))
    return matched


async def _fetch_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        async with _http_client() as client:
            resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise OidcError("failed", "OIDC response too large")
        data = resp.json()
    except OidcError:
        raise
    except Exception as exc:
        raise OidcError("failed", f"OIDC HTTP request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise OidcError("failed", "OIDC endpoint returned non-object JSON")
    return data


async def _get_discovery(issuer_url: str) -> dict[str, Any]:
    base = issuer_url.rstrip("/")
    _validate_endpoint_url(base, allow_http=True, name="issuer")
    allow_http = base.startswith("http://")
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
    for ep_key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        _validate_endpoint_url(str(doc[ep_key]), allow_http=allow_http, name=ep_key)
    if doc.get("userinfo_endpoint"):
        _validate_endpoint_url(str(doc["userinfo_endpoint"]), allow_http=allow_http, name="userinfo_endpoint")
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
    old_kids = {k.get("kid") for k in (cached[1] if cached else ()) if isinstance(k.get("kid"), str)}
    new_kids = {k.get("kid") for k in clean if isinstance(k.get("kid"), str)}
    _JWKS_CACHE[jwks_uri] = (now, clean)
    if not force or old_kids != new_kids:
        _JWKS_MISS_KIDS.pop(jwks_uri, None)
    return clean


def _sanitize_join(join: str) -> str:
    s = (join or "").strip()
    return s if _JOIN_RE.fullmatch(s) else ""


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
    now = int(time.time())
    payload: dict[str, Any] = {
        "state": state,
        "nonce": nonce,
        "cv": verifier,
        "join": _sanitize_join(join),
        "cb": cb,
        "exp": now + STATE_TTL,
    }
    existing = _decode_signed_state(req.cookies.get(STATE_COOKIE, ""))
    if existing is not None:
        prev_entries: list[dict[str, Any]] = []
        raw_prev = existing.get("prev")
        prev_items: list[Any] = [existing, *raw_prev] if isinstance(raw_prev, list) else [existing]
        for item in prev_items:
            if not isinstance(item, dict):
                continue
            try:
                item_exp = int(item.get("exp", 0))
            except (ValueError, TypeError):
                continue
            if item_exp >= now and isinstance(item.get("state"), str) and isinstance(item.get("nonce"), str) and isinstance(item.get("cv"), str):
                prev_entries.append({
                    "state": item["state"],
                    "nonce": item["nonce"],
                    "cv": item["cv"],
                    "join": _sanitize_join(str(item.get("join") or "")),
                    "cb": str(item.get("cb") or ""),
                    "exp": item_exp,
                })
        if prev_entries:
            payload["prev"] = prev_entries[: _MAX_STATE_FLOWS - 1]
    return auth_url, _sign_state(payload)


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
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise OidcError("failed", "Token response too large")
        tok = resp.json()
    except OidcError:
        raise
    except Exception as exc:
        raise OidcError("failed", f"Token exchange failed: {exc}") from exc
    if not isinstance(tok, dict) or not isinstance(tok.get("id_token"), str) or not tok["id_token"]:
        raise OidcError("failed", "Token response missing id_token")
    return tok


def _jwk_usable(k: dict[str, Any], alg: str) -> bool:
    return (
        k.get("use", "sig") == "sig"
        and (not k.get("alg") or k.get("alg") == alg)
        and k.get("kty") not in ("oct", "", None)
    )


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
        candidates = [k for k in keys if k.get("kid") == kid and _jwk_usable(k, alg)]
        if not candidates and kid not in _JWKS_MISS_KIDS.get(jwks_uri, ()):
            keys = await _get_jwks(jwks_uri, force=True)
            candidates = [k for k in keys if k.get("kid") == kid and _jwk_usable(k, alg)]
            if not candidates:
                misses = _JWKS_MISS_KIDS.setdefault(jwks_uri, set())
                if len(misses) >= 128:
                    misses.clear()
                misses.add(kid)
    else:
        candidates = [k for k in keys if _jwk_usable(k, alg)]
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
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
            break
        except Exception as exc:
            last_err = exc
    if claims is None:
        raise OidcError("failed", f"Invalid id_token signature/claims: {last_err}")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip() or len(sub) > 255:
        raise OidcError("failed", "id_token sub invalid")
    aud = claims.get("aud")
    if (isinstance(aud, list) and len(aud) > 1) or "azp" in claims:
        if claims.get("azp") != cfg.OIDC_CLIENT_ID:
            raise OidcError("failed", "id_token azp mismatch")
    if "iat" in claims:
        iat = claims["iat"]
        if not isinstance(iat, (int, float)) or isinstance(iat, bool) or iat > time.time() + 60:
            raise OidcError("failed", "id_token iat invalid")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
        raise OidcError("failed", "id_token nonce mismatch")
    return claims


def _has_claim(claims: dict[str, Any], key: str) -> bool:
    val = claims.get(key)
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict, set, tuple)):
        return bool(val)
    return True


async def _maybe_merge_userinfo(
    claims: dict[str, Any],
    tok: dict[str, Any],
    disc: dict[str, Any],
    cfg: config.Config,
) -> dict[str, Any]:
    need_user = not _has_claim(claims, cfg.OIDC_USERNAME_CLAIM)
    need_groups = bool(cfg.OIDC_ALLOWED_GROUPS) and not _has_claim(claims, "groups")
    endpoint = disc.get("userinfo_endpoint")
    access_token = tok.get("access_token")
    if not (need_user or need_groups) or not isinstance(endpoint, str) or not endpoint or not isinstance(access_token, str) or not access_token:
        return claims
    ui = await _fetch_json(endpoint, headers={"Authorization": f"Bearer {access_token}"})
    ui_sub = ui.get("sub")
    if not isinstance(ui_sub, str) or not hmac.compare_digest(ui_sub, str(claims.get("sub", ""))):
        raise OidcError("failed", "Userinfo sub mismatch")
    merged = dict(claims)
    for k, v in ui.items():
        if not _has_claim(merged, k) and v is not None:
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
        have = {str(g).strip() for g in raw if isinstance(g, str) and g.strip()}
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
        if key == "email" and cfg.OIDC_USERNAME_CLAIM != "email" and claims.get("email_verified") is False:
            continue
        val = claims.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return "user"


def _find_existing_oidc_user(issuer: str, sub: str) -> str | None:
    con = db.connect()
    try:
        row = con.execute(
            "SELECT name FROM users WHERE oidc_issuer=? AND oidc_sub=?",
            (issuer, sub),
        ).fetchone()
        return str(row["name"]) if row else None
    finally:
        con.close()


def _resolve_or_create_user(
    issuer: str,
    sub: str,
    claims: dict[str, Any],
    cfg: config.Config,
) -> str:
    existing = _find_existing_oidc_user(issuer, sub)
    if existing:
        return existing

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
                if n > 100:
                    suffix = f"-{secrets.token_hex(3)}"
                    candidate = f"{base[:20 - len(suffix)].rstrip('_-') or 'user'}{suffix}"
                    continue
                suffix = f"-{n}"
                candidate = f"{base[:20 - len(suffix)].rstrip('_-') or 'user'}{suffix}"
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
    expected_cb = st_data.get("cb")
    if isinstance(expected_cb, str) and expected_cb and not hmac.compare_digest(expected_cb, cb):
        raise OidcError("state_invalid", "Mismatched OIDC redirect_uri")
    tok = await _exchange_code(disc, cfg, code, cb, st_data["cv"])
    claims = await _verify_id_token(tok["id_token"], disc, cfg, st_data["nonce"])
    claims = await _maybe_merge_userinfo(claims, tok, disc, cfg)
    _check_groups(claims, cfg)

    issuer = str(disc["issuer"])
    sub = str(claims["sub"])
    existing = await asyncio.to_thread(_find_existing_oidc_user, issuer, sub)
    if existing is None:
        if not cfg.OIDC_AUTO_REGISTER:
            raise OidcError("registration_disabled", "OIDC auto-registration disabled")
        try:
            deps.limited(req, "register")
        except HTTPException as exc:
            if exc.status_code == 429:
                raise OidcError("rate_limited", "OIDC registration rate limit exceeded") from exc
            raise
        username = await asyncio.to_thread(_resolve_or_create_user, issuer, sub, claims, cfg)
    else:
        username = existing
    join_tok = _sanitize_join(str(st_data.get("join") or ""))
    return username, join_tok
