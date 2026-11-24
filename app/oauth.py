"""OAuth 2.1 for the MCP endpoint -- the part that lets an assistant act as a user.

ChatGPT and Claude will not take a pasted password or a static key for a remote
MCP server; they discover an authorization server, register themselves, and
send the person to a sign-in page. So this is the smallest thing that satisfies
that dance: metadata (RFC 8414 / 9728), dynamic client registration (RFC 7591),
the authorization-code grant with PKCE, and refresh tokens.

Plus the device authorization grant (RFC 8628) for apps that cannot redirect or
open a pop-up -- a sandboxed mini app, a TV, a script: the app shows a short code,
the person approves it at /activate in any browser where they can sign in, and the
app polls the token endpoint. The password never passes through the app.

What it deliberately is not: a second kind of account. The only thing anyone can
sign in with is a studio username and password issued by the admin, and a token
is re-checked against that account on every call -- expiry, disabling and
deletion cut an assistant off exactly as they cut a browser off.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app import auth, db
from app.service import base_url

log = logging.getLogger("studio.oauth")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# base.html calls keep_a_copy() for signed-in people; without it here, the sign-in
# pages crashed for anyone already signed in (found 2026-10-25).
from app.service import keep_a_copy_note  # noqa: E402
templates.env.globals["keep_a_copy"] = keep_a_copy_note
from app.main_assets import ASSET_V  # noqa: E402  (the same ?v= stamp as the other pages)
templates.env.globals["asset_v"] = ASSET_V

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
# A rotated refresh token keeps working this long. Two sessions of the same
# assistant share one stored token; without the overlap the slower of two
# simultaneous refreshes is logged out for having been honest.
REFRESH_GRACE = 60
MAX_CLIENTS = 5000
SCOPE = "studio"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --------------------------------------------------------------------------- metadata

def resource_metadata_url(request: Request) -> str:
    return f"{base_url(request)}/.well-known/oauth-protected-resource/mcp"


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request):
    base = base_url(request)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [SCOPE],
        "resource_name": "Story Studio",
        "resource_documentation": f"{base}/install",
    }


# The same document under every name a client is known to look for it by.
@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/openid-configuration")
@router.get("/.well-known/openid-configuration/mcp")
async def authorization_server(request: Request):
    base = base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:device_code"],
        "device_authorization_endpoint": f"{base}/oauth/device_authorization",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": [SCOPE],
        "service_documentation": f"{base}/install",
    }


# --------------------------------------------------------------------------- registration

def _redirect_ok(uri: str) -> bool:
    """https anywhere, http only to this machine, or an app's own scheme.

    The loopback and custom-scheme cases are how desktop and CLI clients catch
    the redirect. What is refused is the set that turns a redirect into script
    execution or a plain-http hop across the network.
    """
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.scheme:
        return False
    scheme = parts.scheme.lower()
    if scheme == "https":
        return bool(parts.hostname)
    if scheme == "http":
        return (parts.hostname or "") in _LOOPBACK
    return scheme not in ("javascript", "data", "file", "vbscript", "blob", "about")


def _same_redirect(registered: str, given: str) -> bool:
    if registered == given:
        return True
    # RFC 8252 7.3: a native client picks its loopback port at run time, so the
    # port is the one part of a registered redirect that may differ.
    a, b = urlsplit(registered), urlsplit(given)
    return (a.scheme == b.scheme == "http" and (a.hostname or "") in _LOOPBACK
            and a.hostname == b.hostname and a.path == b.path and a.query == b.query)


def _oauth_error(code: str, description: str, status: int = 400, headers: dict | None = None):
    return JSONResponse({"error": code, "error_description": description},
                        status_code=status, headers={**_NO_STORE, **(headers or {})})


@router.post("/oauth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:                                          # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return _oauth_error("invalid_client_metadata", "send a JSON object")
    uris = body.get("redirect_uris")
    grants = body.get("grant_types") if isinstance(body.get("grant_types"), list) else []
    device_only = "urn:ietf:params:oauth:grant-type:device_code" in grants and not uris
    if device_only:
        uris = []          # a device-sign-in app has no address to return to
    elif not isinstance(uris, list) or not 1 <= len(uris) <= 10 \
            or not all(_redirect_ok(u) for u in uris):
        return _oauth_error(
            "invalid_redirect_uri",
            "redirect_uris must be 1-10 URIs: https, http on localhost, or an app scheme")

    # Registration is open by necessity, so it is also where junk accumulates.
    # A client nobody ever completed a sign-in with is safe to forget.
    db.run("DELETE FROM oauth_clients WHERE last_used_at IS NULL AND created_at < ?",
           (db.now() - 86400,))
    if db.one("SELECT COUNT(*) AS n FROM oauth_clients")["n"] >= MAX_CLIENTS:
        return _oauth_error("temporarily_unavailable", "too many registered clients", 429)

    client_id = "mcp_" + secrets.token_urlsafe(18)
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        method = "none"
    secret = secrets.token_urlsafe(32) if method != "none" else None
    name = str(body.get("client_name") or "")[:80]
    db.run("INSERT INTO oauth_clients (id,name,redirect_uris,secret_hash,created_at) "
           "VALUES (?,?,?,?,?)",
           (client_id, name, json.dumps(uris), _h(secret) if secret else None, db.now()))
    log.info("registered oauth client %s (%r)", client_id, name)
    out = {
        "client_id": client_id,
        "client_id_issued_at": int(db.now()),
        "client_name": name,
        "redirect_uris": uris,
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"] if device_only else
                       ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:device_code"],
        "response_types": [] if device_only else ["code"],
        "token_endpoint_auth_method": method,
        "scope": SCOPE,
    }
    if secret:
        out["client_secret"] = secret
        out["client_secret_expires_at"] = 0
    return JSONResponse(out, status_code=201, headers=_NO_STORE)


# --------------------------------------------------------------------------- authorize

_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge",
           "code_challenge_method", "scope", "resource")


def _with_params(uri: str, params: dict) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _page(request: Request, status: int = 200, **ctx):
    resp = templates.TemplateResponse("authorize.html", {"request": request, **ctx},
                                      status_code=status)
    # This page turns one click into a grant, so it must never render inside
    # somebody else's frame.
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _check_request(request: Request, p: dict):
    """(client, redirect_uri, problem). A problem with the client or the
    redirect is shown on the page; anything after that goes back to the client,
    because only then is the redirect known to be the client's own."""
    client = db.one("SELECT * FROM oauth_clients WHERE id=?", (p.get("client_id") or "",))
    if client is None:
        return None, None, ("This sign-in link names an application this server does not "
                            "know. Remove the connector in your assistant and add it again.")
    registered = json.loads(client["redirect_uris"])
    redirect_uri = p.get("redirect_uri") or (registered[0] if len(registered) == 1 else "")
    if not any(_same_redirect(r, redirect_uri) for r in registered):
        return None, None, "This sign-in link has a return address the application never registered."
    return client, redirect_uri, None


def _protocol_error(p: dict) -> tuple[str, str] | None:
    if p.get("response_type") != "code":
        return "unsupported_response_type", "only response_type=code is supported"
    if not p.get("code_challenge") or (p.get("code_challenge_method") or "plain") != "S256":
        return "invalid_request", "PKCE with code_challenge_method=S256 is required"
    return None


def _back(redirect_uri: str, p: dict, **params) -> RedirectResponse:
    if p.get("state"):
        params["state"] = p["state"]
    return RedirectResponse(_with_params(redirect_uri, params), status_code=302)


@router.get("/oauth/authorize")
async def authorize_form(request: Request):
    p = {k: request.query_params.get(k, "") for k in _FIELDS}
    client, redirect_uri, problem = _check_request(request, p)
    if problem:
        return _page(request, 400, problem=problem)
    bad = _protocol_error(p)
    if bad:
        return _back(redirect_uri, p, error=bad[0], error_description=bad[1])
    p["redirect_uri"] = redirect_uri
    return _page(request, fields=p, client_name=client["name"],
                 return_to=urlsplit(redirect_uri).netloc or urlsplit(redirect_uri).scheme,
                 user=auth.current_user(request))


@router.post("/oauth/authorize")
async def authorize(request: Request):
    form = await request.form()
    p = {k: str(form.get(k) or "") for k in _FIELDS}
    client, redirect_uri, problem = _check_request(request, p)
    if problem:
        return _page(request, 400, problem=problem)
    bad = _protocol_error(p)
    if bad:
        return _back(redirect_uri, p, error=bad[0], error_description=bad[1])
    if form.get("action") == "deny":
        return _back(redirect_uri, p, error="access_denied",
                     error_description="the person declined")

    # Either the browser is already signed in to the studio, or the form
    # carries a username and password. The cookie is SameSite=Lax, so another
    # site cannot make this POST ride on somebody's existing session.
    user = None
    username = str(form.get("username") or "").strip()
    if username:
        user, reason = auth.authenticate(username, str(form.get("password") or ""))
        if user is None:
            log.info("oauth sign-in refused for %r: %s", username[:40], reason)
    else:
        user = auth.current_user(request)
    if user is None:
        p["redirect_uri"] = redirect_uri
        return _page(
            request, 401, fields=p, client_name=client["name"],
            return_to=urlsplit(redirect_uri).netloc or urlsplit(redirect_uri).scheme,
            user=None,
            error="That username and password did not work, or the account has expired.")

    code = secrets.token_urlsafe(32)
    db.run("INSERT INTO oauth_codes (code_hash,client_id,user_id,redirect_uri,challenge,"
           "expires_at) VALUES (?,?,?,?,?,?)",
           (_h(code), client["id"], user.id, redirect_uri, p["code_challenge"],
            db.now() + CODE_TTL))
    log.info("user %s authorised oauth client %s (%r)", user.username, client["id"],
             client["name"])
    return _back(redirect_uri, p, code=code)


# --------------------------------------------------------------------------- token

def _issue(client_id: str, user_id: str) -> dict:
    access = "sst_" + secrets.token_urlsafe(32)
    refresh = "ssr_" + secrets.token_urlsafe(32)
    now = db.now()
    for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
        db.run("INSERT INTO oauth_tokens (token_hash,kind,client_id,user_id,created_at,"
               "expires_at) VALUES (?,?,?,?,?,?)",
               (_h(token), kind, client_id, user_id, now, now + ttl))
    db.run("UPDATE oauth_clients SET last_used_at=? WHERE id=?", (now, client_id))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": refresh, "scope": SCOPE}


def _account_ok(user_id: str) -> bool:
    row = db.one("SELECT disabled, expires_at FROM users WHERE id=?", (user_id,))
    return bool(row) and not row["disabled"] and \
        (row["expires_at"] is None or row["expires_at"] >= db.now())


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    client_id = str(form.get("client_id") or "")
    client_secret = str(form.get("client_secret") or "")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            client_id, _, client_secret = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:                                      # noqa: BLE001
            return _oauth_error("invalid_client", "unreadable Basic credentials", 401)
    client = db.one("SELECT * FROM oauth_clients WHERE id=?", (client_id,))
    if client is None:
        return _oauth_error("invalid_client", "unknown client_id", 401)
    if client["secret_hash"] and not hmac.compare_digest(client["secret_hash"],
                                                         _h(client_secret)):
        return _oauth_error("invalid_client", "wrong client_secret", 401)

    grant = form.get("grant_type")
    if grant == "authorization_code":
        code_hash = _h(str(form.get("code") or ""))
        row = db.one("SELECT * FROM oauth_codes WHERE code_hash=?", (code_hash,))
        # Spent on sight, whatever happens next: a code is good for one try.
        db.run("DELETE FROM oauth_codes WHERE code_hash=?", (code_hash,))
        if row is None or row["expires_at"] < db.now() or row["client_id"] != client_id:
            return _oauth_error("invalid_grant", "the authorization code is unknown or expired")
        given = str(form.get("redirect_uri") or "")
        if given and given != row["redirect_uri"]:
            return _oauth_error("invalid_grant", "redirect_uri does not match")
        verifier = str(form.get("code_verifier") or "")
        want = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if not verifier or not hmac.compare_digest(want, row["challenge"]):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        if not _account_ok(row["user_id"]):
            return _oauth_error("invalid_grant", "the account is no longer active")
        return JSONResponse(_issue(client_id, row["user_id"]), headers=_NO_STORE)

    if grant == "refresh_token":
        token_hash = _h(str(form.get("refresh_token") or ""))
        row = db.one("SELECT * FROM oauth_tokens WHERE token_hash=? AND kind='refresh'",
                     (token_hash,))
        if row is None or row["expires_at"] < db.now() or row["client_id"] != client_id \
                or not _account_ok(row["user_id"]):
            return _oauth_error("invalid_grant", "the refresh token is unknown or expired")
        db.run("UPDATE oauth_tokens SET expires_at=? WHERE token_hash=?",
               (min(row["expires_at"], db.now() + REFRESH_GRACE), token_hash))
        return JSONResponse(_issue(client_id, row["user_id"]), headers=_NO_STORE)

    if grant == "urn:ietf:params:oauth:grant-type:device_code":
        return _device_token(client_id, str(form.get("device_code") or ""))

    return _oauth_error("unsupported_grant_type",
                        "use authorization_code, refresh_token or urn:ietf:params:oauth:grant-type:device_code")


# --------------------------------------------------------------------------- device sign-in (RFC 8628)

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_TTL = 15 * 60
DEVICE_INTERVAL = 5
# No vowels or look-alikes: codes are read off one screen and typed into another,
# and cannot spell words. 20^8 combinations, alive 15 minutes, approvable only by a
# signed-in account.
_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"


def _user_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_code(value: str) -> str:
    v = "".join(c for c in (value or "").upper() if c in _CODE_ALPHABET)
    return f"{v[:4]}-{v[4:8]}" if len(v) == 8 else ""


@router.post("/oauth/device_authorization")
async def device_authorization(request: Request):
    form = await request.form()
    client_id = str(form.get("client_id") or "")
    client = db.one("SELECT * FROM oauth_clients WHERE id=?", (client_id,))
    if client is None:
        return _oauth_error("invalid_client", "unknown client_id; register first", 401)
    db.run("DELETE FROM oauth_device WHERE expires_at < ?", (db.now(),))
    device_code = "ssd_" + secrets.token_urlsafe(32)
    for _ in range(5):
        code = _user_code()
        if not db.one("SELECT 1 FROM oauth_device WHERE user_code=?", (code,)):
            break
    db.run("INSERT INTO oauth_device (device_hash,user_code,client_id,status,interval,"
           "created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
           (_h(device_code), code, client_id, "pending", DEVICE_INTERVAL, db.now(),
            db.now() + DEVICE_TTL))
    base = base_url(request)
    log.info("device sign-in started for client %s (%r)", client_id, client["name"])
    return JSONResponse({
        "device_code": device_code, "user_code": code,
        "verification_uri": f"{base}/activate",
        "verification_uri_complete": f"{base}/activate?code={code}",
        "expires_in": DEVICE_TTL, "interval": DEVICE_INTERVAL}, headers=_NO_STORE)


def _device_token(client_id: str, device_code: str):
    h = _h(device_code)
    row = db.one("SELECT * FROM oauth_device WHERE device_hash=?", (h,))
    if row is None or row["client_id"] != client_id:
        return _oauth_error("invalid_grant", "unknown device_code")
    if row["expires_at"] < db.now():
        db.run("DELETE FROM oauth_device WHERE device_hash=?", (h,))
        return _oauth_error("expired_token", "the code expired; start sign-in again")
    if row["status"] == "denied":
        db.run("DELETE FROM oauth_device WHERE device_hash=?", (h,))
        return _oauth_error("access_denied", "the person declined")
    if row["status"] == "pending":
        if row["last_poll"] and db.now() - row["last_poll"] < row["interval"] - 0.5:
            db.run("UPDATE oauth_device SET interval=interval+5, last_poll=? WHERE device_hash=?",
                   (db.now(), h))
            return _oauth_error("slow_down", "poll no faster than every `interval` seconds")
        db.run("UPDATE oauth_device SET last_poll=? WHERE device_hash=?", (db.now(), h))
        return _oauth_error("authorization_pending", "waiting for the person to approve")
    # approved: spent on use, like an authorization code
    db.run("DELETE FROM oauth_device WHERE device_hash=?", (h,))
    if not _account_ok(row["user_id"]):
        return _oauth_error("invalid_grant", "the account is no longer active")
    return JSONResponse(_issue(client_id, row["user_id"]), headers=_NO_STORE)


def _activate_page(request: Request, status: int = 200, **ctx):
    resp = templates.TemplateResponse("activate.html", {"request": request, **ctx},
                                      status_code=status)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _pending(code: str):
    return db.one("SELECT d.*, c.name AS client_name FROM oauth_device d JOIN oauth_clients c "
                  "ON c.id=d.client_id WHERE d.user_code=? AND d.status='pending' AND "
                  "d.expires_at > ?", (code, db.now()))


@router.get("/activate")
async def activate_form(request: Request):
    code = normalize_user_code(request.query_params.get("code", ""))
    row = _pending(code) if code else None
    return _activate_page(request, code=code, client_name=row["client_name"] if row else None,
                          user=auth.current_user(request),
                          error=None if (row or not code) else
                          "That code is not valid or has expired. Check it, or start sign-in "
                          "again in the app.")


@router.post("/activate")
async def activate(request: Request):
    form = await request.form()
    code = normalize_user_code(str(form.get("code") or ""))
    row = _pending(code) if code else None
    if row is None:
        return _activate_page(request, 400, code=code, user=auth.current_user(request),
                              error="That code is not valid or has expired. Check it, or start "
                                    "sign-in again in the app.")
    if form.get("action") == "deny":
        db.run("UPDATE oauth_device SET status='denied' WHERE user_code=?", (code,))
        return _activate_page(request, done="denied", user=auth.current_user(request))
    # Same rule as /oauth/authorize: an existing session (SameSite=Lax cookie, so another
    # site cannot post this on someone's behalf) or a username and password.
    user = None
    username = str(form.get("username") or "").strip()
    if username:
        user, reason = auth.authenticate(username, str(form.get("password") or ""))
        if user is None:
            log.info("device sign-in refused for %r: %s", username[:40], reason)
    else:
        user = auth.current_user(request)
    if user is None:
        return _activate_page(request, 401, code=code, client_name=row["client_name"], user=None,
                              error="That username and password did not work, or the account "
                                    "has expired.")
    db.run("UPDATE oauth_device SET status='approved', user_id=? WHERE user_code=? AND "
           "status='pending'", (user.id, code))
    log.info("user %s approved device sign-in for client %s", user.username, row["client_id"])
    return _activate_page(request, done="approved", client_name=row["client_name"], user=user)


# --------------------------------------------------------------------------- bearer

def user_for_request(request: Request) -> auth.User | None:
    """The account behind a request's bearer token, re-checked every time."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    row = db.one(
        "SELECT t.expires_at AS t_exp, u.id, u.username, u.role, "
        "       u.expires_at AS u_exp, u.disabled "
        "FROM oauth_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash=? AND t.kind='access'", (_h(header[7:].strip()),))
    if row is None or row["t_exp"] < db.now():
        return None
    if row["disabled"] or (row["u_exp"] is not None and row["u_exp"] < db.now()):
        return None
    return auth.User(row["id"], row["username"], row["role"], row["u_exp"])
