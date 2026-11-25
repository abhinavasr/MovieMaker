"""Batiyao (batiyao.com): sign in with Batiyao, and share studio work to the person's Batiyao.

The person's choices (2026-11-25):
  * "Use are able to login with Batiyao account" -- a first Batiyao sign-in creates a studio
    account automatically (normal limits; cfg.BATIYAO_AUTO_ACCOUNTS).
  * "allow people share their items on the batiyao account directly from here" -- the Share
    sheet offers a Post (text <= 500, costs the person 1.0 BuckBuck, confirmed first) or a
    Story (free: one picture or a video up to 30 s, followers only, 48 hours, must be sfw).

Batiyao's API (https://api.batiyao.com, OpenAPI at /openapi.json) is OAuth 2.1 with PKCE and a
public client: no client secret. Sign-in asks only for `read:feed` (enough to read the
profile that identifies the person); the first share asks again for `write:media write:post`.
Tokens are encrypted at rest with cfg.BATIYAO_TOKEN_KEY (Fernet) and never reach the browser.
Access tokens last an hour; refresh tokens ROTATE -- the new one is always stored.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import auth, db, images, service
from app.config import cfg

log = logging.getLogger("studio.batiyao")
router = APIRouter()

LOGIN_SCOPES = ["read:feed"]
SHARE_SCOPES = ["read:feed", "write:media", "write:post"]
PENDING_TTL = 15 * 60
POST_MAX_CHARS = 500
STORY_MAX_S = 30.0
MEDIA_MAX_MB = 60          # the upload is base64 JSON; keep the request well under 100 MB
STORY_IMAGE_MAX_MB = 30


# --------------------------------------------------------------------------- basics

def enabled() -> bool:
    return bool(cfg.BATIYAO_CLIENT_ID and cfg.BATIYAO_TOKEN_KEY and _fernet())


_F = None


def _fernet():
    global _F
    if _F is None and cfg.BATIYAO_TOKEN_KEY:
        try:
            from cryptography.fernet import Fernet
            _F = Fernet(cfg.BATIYAO_TOKEN_KEY.encode())
        except Exception as exc:                               # noqa: BLE001
            log.error("Batiyao is off: STUDIO_BATIYAO_TOKEN_KEY is not a valid key (%s)", exc)
            _F = False
    return _F or None


def _enc(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode() if text else ""


def _dec(text: str) -> str:
    return _fernet().decrypt(text.encode()).decode() if text else ""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def redirect_uri(request: Request) -> str:
    return service.base_url(request) + "/auth/batiyao/callback"


def _safe_next(nxt: str | None) -> str:
    """Only a path on this site (no open redirects)."""
    nxt = str(nxt or "/")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


def _unwrap(data):
    """Batiyao answers {"result": "<label line>\\n<json>"} for reads, a sentence for writes."""
    res = data.get("result") if isinstance(data, dict) else None
    if not isinstance(res, str):
        return data
    nl = res.find("\n")
    if res.startswith("UNTRUSTED PLATFORM DATA") and nl > 0:
        try:
            return json.loads(res[nl + 1:])
        except ValueError:
            return res[nl + 1:]
    return res


def link_of(user_id: str):
    return db.one("SELECT * FROM batiyao_links WHERE user_id=?", (user_id,))


# --------------------------------------------------------------------------- OAuth

def _authorize_url(request: Request, purpose: str, user_id: str | None, nxt: str,
                   scopes: list[str]) -> str:
    if not enabled():
        raise HTTPException(503, "Batiyao sign-in is not set up on this studio yet")
    db.run("DELETE FROM batiyao_pending WHERE created_at < ?", (time.time() - PENDING_TTL,))
    verifier = _b64url(secrets.token_bytes(48))
    state = _b64url(secrets.token_bytes(24))
    db.run("INSERT INTO batiyao_pending (state, verifier, purpose, user_id, next, created_at) "
           "VALUES (?,?,?,?,?,?)", (state, verifier, purpose, user_id, _safe_next(nxt), time.time()))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return f"{cfg.BATIYAO_API}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": cfg.BATIYAO_CLIENT_ID,
        "redirect_uri": redirect_uri(request), "scope": " ".join(scopes), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})


async def _token_request(fields: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{cfg.BATIYAO_API}/oauth/token", data=fields,
                         headers={"Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    if r.status_code != 200 or not body.get("access_token"):
        raise HTTPException(502 if r.status_code < 500 else 503,
                            body.get("error_description") or body.get("error") or
                            f"Batiyao refused the sign-in ({r.status_code})")
    return body


def _store_tokens(user_id: str, body: dict, profile: dict | None = None) -> None:
    now = time.time()
    scopes = body.get("scope") if isinstance(body.get("scope"), str) else ""
    fields = {"access_token": _enc(body["access_token"]),
              "refresh_token": _enc(body.get("refresh_token") or ""),
              "expires_at": now + max(0, int(body.get("expires_in") or 3600) - 60),
              "updated_at": now}
    if scopes:
        fields["scopes"] = scopes
    if profile is not None:
        fields.update(bt_user_id=str(profile["id"]), username=str(profile.get("username") or ""),
                      display_name=str(profile.get("display_name") or ""))
    if link_of(user_id):
        cols = ", ".join(f"{k}=?" for k in fields)
        db.run(f"UPDATE batiyao_links SET {cols} WHERE user_id=?", (*fields.values(), user_id))
    else:
        db.run("INSERT INTO batiyao_links (user_id, bt_user_id, username, display_name, "
               "access_token, refresh_token, scopes, expires_at, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (user_id, fields["bt_user_id"], fields["username"], fields["display_name"],
                fields["access_token"], fields["refresh_token"], scopes, fields["expires_at"],
                now, now))


async def _access_token(user_id: str, force_refresh: bool = False) -> str:
    row = link_of(user_id)
    if row is None or not row["access_token"]:
        raise HTTPException(409, "connect your Batiyao account first")
    if not force_refresh and row["expires_at"] > time.time():
        return _dec(row["access_token"])
    rt = _dec(row["refresh_token"])
    if not rt:
        raise HTTPException(409, "your Batiyao connection has ended; connect it again")
    try:
        body = await _token_request({"grant_type": "refresh_token", "refresh_token": rt,
                                     "client_id": cfg.BATIYAO_CLIENT_ID})
    except HTTPException:
        # invalid_grant / revoked: the person disconnected the studio on Batiyao.
        db.run("UPDATE batiyao_links SET access_token='', refresh_token='' WHERE user_id=?",
               (user_id,))
        raise HTTPException(409, "your Batiyao connection has ended; connect it again")
    _store_tokens(user_id, body)
    return body["access_token"]


async def _api(user_id: str | None, method: str, path: str, payload: dict | None = None,
               token: str | None = None, timeout: float = 60):
    """One Batiyao API call; on 403 it refreshes once (expired token vs missing permission)."""
    for attempt in (0, 1):
        tok = token or await _access_token(user_id, force_refresh=attempt == 1)
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, f"{cfg.BATIYAO_API}{path}", json=payload,
                                headers={"Authorization": f"Bearer {tok}",
                                         "Accept": "application/json"})
        if r.status_code == 403 and attempt == 0 and token is None:
            continue
        break
    try:
        body = r.json()
    except ValueError:
        body = {"result": r.text[:500]}
    if r.status_code >= 400:
        msg = body.get("result") or body.get("error") or body.get("detail") or r.text[:300]
        if r.status_code == 403:
            msg = (f"Batiyao said no: {msg}. If you switched off a permission for Story Studio "
                   "in Batiyao → Settings → Connected apps, connect again to allow sharing.")
        elif r.status_code == 429:
            msg = "Batiyao is busy; try again in a minute."
        elif "402" in str(msg):
            msg = "Not enough BuckBuck for a post; nothing was spent."
        raise HTTPException(400 if r.status_code < 500 else 502, str(msg)[:600])
    return body


# --------------------------------------------------------------------------- accounts

def _new_username(bt_username: str) -> str:
    base = re.sub(r"[^a-z0-9_.-]+", "", (bt_username or "").lower())[:24] or "batiyao"
    name, n = base, 1
    while db.one("SELECT 1 FROM users WHERE username=?", (name,)):
        n += 1
        name = f"{base}-{n}"
    return name


def _user_for_login(profile: dict) -> str:
    """The studio user for this Batiyao person; made on first sign-in when allowed."""
    row = db.one("SELECT user_id FROM batiyao_links WHERE bt_user_id=?", (str(profile["id"]),))
    if row:
        return row["user_id"]
    if not cfg.BATIYAO_AUTO_ACCOUNTS:
        raise HTTPException(403, "This Batiyao account is not connected to a studio account. "
                                 "Sign in with your studio password and connect Batiyao there.")
    uname = _new_username(profile.get("username") or "")
    uid = auth.create_user(uname, secrets.token_urlsafe(32), expires_at=None,
                           note=f"Batiyao @{profile.get('username') or '?'} "
                                f"({profile.get('display_name') or ''})".strip())
    log.info("new studio account %s for Batiyao @%s", uname, profile.get("username"))
    return uid


# --------------------------------------------------------------------------- routes: sign-in

@router.get("/auth/batiyao/start")
async def start_login(request: Request, next: str = "/"):
    """"Continue with Batiyao" on the login page."""
    return RedirectResponse(_authorize_url(request, "login", None, next, LOGIN_SCOPES), 303)


@router.get("/auth/batiyao/connect")
async def start_connect(request: Request, next: str = "/", share: int = 1):
    """Connect (or widen) Batiyao for the signed-in person; by default with posting rights."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login", 303)
    return RedirectResponse(_authorize_url(request, "connect", user.id, next,
                                           SHARE_SCOPES if share else LOGIN_SCOPES), 303)


def _session_redirect(user_id: str, target: str) -> RedirectResponse:
    token = auth.start_session(user_id)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("studio_session", token, httponly=True, samesite="lax",
                    secure=cfg.COOKIE_SECURE, max_age=cfg.SESSION_TTL_HOURS * 3600)
    return resp


@router.get("/auth/batiyao/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = "",
                   error_description: str = ""):
    pend = db.one("SELECT * FROM batiyao_pending WHERE state=?", (state,)) if state else None
    if pend is not None:
        db.run("DELETE FROM batiyao_pending WHERE state=?", (state,))
    if error:
        dest = "/login?error=" if pend is None or pend["purpose"] == "login" else (pend["next"] + "?batiyao=")
        return RedirectResponse(dest + ("cancelled" if error == "access_denied" else "batiyao"), 303)
    if pend is None or time.time() - pend["created_at"] > PENDING_TTL or not code:
        raise HTTPException(400, "This Batiyao sign-in has expired or was not started here. "
                                 "Start it again.")
    body = await _token_request({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": redirect_uri(request),
                                 "client_id": cfg.BATIYAO_CLIENT_ID,
                                 "code_verifier": pend["verifier"]})
    profile = _unwrap(await _api(None, "GET", "/v1/profile", token=body["access_token"]))
    if not isinstance(profile, dict) or not profile.get("id"):
        raise HTTPException(502, "Batiyao did not say who you are; try again")
    bt_id = str(profile["id"])
    if pend["purpose"] == "login":
        uid = _user_for_login(profile)
        user_row = db.one("SELECT disabled, expires_at FROM users WHERE id=?", (uid,))
        if not user_row or user_row["disabled"] or (user_row["expires_at"] and
                                                    user_row["expires_at"] < time.time()):
            return RedirectResponse("/login?error=disabled", 303)
        _store_tokens(uid, body, profile)
        return _session_redirect(uid, _safe_next(pend["next"]))
    # connect: link to the signed-in person (who started it)
    other = db.one("SELECT user_id FROM batiyao_links WHERE bt_user_id=?", (bt_id,))
    if other and other["user_id"] != pend["user_id"]:
        raise HTTPException(409, "That Batiyao account is already connected to another studio "
                                 "account.")
    _store_tokens(pend["user_id"], body, profile)
    nxt = _safe_next(pend["next"])
    return RedirectResponse(nxt + ("&" if "?" in nxt else "?") + "batiyao=connected", 303)


@router.get("/api/batiyao/status")
async def status(request: Request):
    user = auth.current_user(request)
    row = link_of(user.id) if user else None
    scopes = set((row["scopes"] or "").split()) if row else set()
    return {"enabled": enabled(),
            "connected": bool(row and row["access_token"]),
            "username": row["username"] if row else None,
            "display_name": row["display_name"] if row else None,
            "can_share": bool(row and row["access_token"]) and
                         {"write:media", "write:post"} <= scopes}


@router.post("/api/batiyao/disconnect")
async def disconnect(request: Request):
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401)
    row = link_of(user.id)
    if row and row["access_token"]:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{cfg.BATIYAO_API}/oauth/revoke",
                             data={"token": _dec(row["access_token"]),
                                   "client_id": cfg.BATIYAO_CLIENT_ID})
        except Exception as exc:                               # noqa: BLE001
            log.warning("revoke failed: %s", exc)
    # The link itself stays (it is how this person signs in); only the tokens go.
    db.run("UPDATE batiyao_links SET access_token='', refresh_token='', scopes='' "
           "WHERE user_id=?", (user.id,))
    return {"ok": True}


# --------------------------------------------------------------------------- items to share

def _probe(path: Path) -> tuple[float, bool]:
    """(seconds, has video stream)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:stream=codec_type", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=30).stdout
        d = json.loads(out or "{}")
        secs = float((d.get("format") or {}).get("duration") or 0)
        return secs, any(s.get("codec_type") == "video" for s in d.get("streams") or [])
    except Exception:                                          # noqa: BLE001
        return 0.0, False


def resolve(user: auth.User, kind: str, item_id: str) -> dict:
    """{path, title, media: image|video|audio} for something this person may share."""
    from app import editor
    kind = str(kind)
    if kind == "image":
        row = images.owned_image(user, item_id)
        if row["status"] != "succeeded" or not row["path"]:
            raise HTTPException(409, "that picture is not finished")
        return {"path": Path(row["path"]), "title": (row["prompt"] or "Picture")[:80], "media": "image"}
    if kind in ("speech", "music"):
        row = db.one(f"SELECT * FROM {kind} WHERE id=? AND user_id=?", (item_id, user.id))
        if row is None or row["status"] != "succeeded" or not row["path"]:
            raise HTTPException(404, f"no finished {kind} {item_id}")
        title = (row["title"] if kind == "music" else row["text"]) or kind.title()
        return {"path": Path(row["path"]), "title": title[:80], "media": "audio"}
    if kind == "project":
        p = service.owned_project(user, item_id)
        if not p["output_path"]:
            raise HTTPException(409, "this project has no finished video")
        return {"path": Path(p["output_path"]), "title": p["title"] or "Video", "media": "video"}
    if kind == "film":                                          # the editor's mixed export
        p = editor.production(user, item_id)
        return {"path": editor.export_path(p["id"]), "title": p["title"] or "Film", "media": "video"}
    if kind in ("shot", "shotpic"):                             # item_id = "<project>:<shot>"
        pid, _, sid = item_id.partition(":")
        p = editor.production(user, pid)
        s = editor._shot(p["id"], sid)
        if kind == "shotpic":
            a = db.one("SELECT path FROM assets WHERE id=? AND project_id=?", (s["asset_id"], p["id"])) \
                if s["asset_id"] else None
            if not a:
                raise HTTPException(409, "this shot has no picture yet")
            return {"path": Path(a["path"]), "title": s["title"] or p["title"], "media": "image"}
        if not s["clip_path"]:
            raise HTTPException(409, "this shot has no clip yet")
        return {"path": Path(s["clip_path"]), "title": s["title"] or p["title"], "media": "video"}
    raise HTTPException(400, "share one of: image, speech, music, project, film, shot, shotpic")


def _cover(title: str, out: Path) -> None:
    """A title card for sound-only items (Batiyao takes pictures and videos)."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = 1080, 1080
    img = Image.new("RGB", (w, h), (24, 20, 32))
    d = ImageDraw.Draw(img)
    for y in range(h):                                        # soft vertical gradient
        c = int(24 + 40 * y / h)
        d.line([(0, y), (w, y)], fill=(c, 20 + c // 3, 40 + c // 2))
    try:
        font = ImageFont.load_default(size=64)
        small = ImageFont.load_default(size=34)
    except TypeError:
        font = small = ImageFont.load_default()
    words, lines, cur = title.split(), [], ""
    for word in words:
        if len(cur) + len(word) > 22 and cur:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    lines = (lines + [cur])[:6]
    y = h // 2 - 40 * len(lines)
    for ln in lines:
        tw = d.textlength(ln, font=font)
        d.text(((w - tw) / 2, y), ln, fill=(245, 230, 200), font=font)
        y += 80
    tag = "♪ made in Story Studio"
    d.text(((w - d.textlength(tag, font=small)) / 2, h - 120), tag, fill=(200, 170, 110), font=small)
    img.save(out)


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise HTTPException(500, "could not prepare the file: " + r.stderr[-300:])


def prepare(item: dict, mode: str, workdir: Path) -> tuple[Path, str]:
    """The file to upload (path, mime), shaped for Batiyao's limits."""
    src = item["path"]
    if not src.exists():
        raise HTTPException(404, "the file is no longer here (files are kept 90 days)")
    if item["media"] == "image":
        if mode == "story" and src.stat().st_size > STORY_IMAGE_MAX_MB * 2**20:
            raise HTTPException(413, "that picture is too large for a story")
        return src, "image/png" if src.suffix.lower() == ".png" else "image/jpeg"
    secs, has_video = _probe(src)
    if mode == "story" and secs > STORY_MAX_S + 0.5:
        raise HTTPException(400, f"a story can be at most 30 s; this is {secs:.0f} s — share it "
                                 "as a post instead")
    out = workdir / "share.mp4"
    budget_bits = MEDIA_MAX_MB * 8 * 1e6 * 0.92
    if item["media"] == "audio" or not has_video:
        cover = workdir / "cover.png"
        _cover(item["title"], cover)
        _run(["ffmpeg", "-y", "-loop", "1", "-framerate", "2", "-i", str(cover), "-i", str(src),
              "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "2",
              "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)])
        return out, "video/mp4"
    size = src.stat().st_size
    if size <= MEDIA_MAX_MB * 1e6 * 0.95 and src.suffix.lower() == ".mp4":
        return src, "video/mp4"
    vbr = max(300_000, int(budget_bits / max(secs, 1) - 128_000))
    _run(["ffmpeg", "-y", "-i", str(src), "-vf", "scale='min(1280,iw)':-2",
          "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vbr), "-maxrate", str(vbr),
          "-bufsize", str(vbr * 2), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
          "-movflags", "+faststart", str(out)])
    return out, "video/mp4"


# --------------------------------------------------------------------------- share

async def share(user: auth.User, kind: str, item_id: str, mode: str, text: str = "",
                rating: str = "sfw", reasoning: str = "", confirm_spend: bool = False) -> dict:
    if not enabled():
        raise HTTPException(503, "Batiyao sharing is not set up on this studio yet")
    st = link_of(user.id)
    scopes = set((st["scopes"] or "").split()) if st else set()
    if not st or not st["access_token"] or not {"write:media", "write:post"} <= scopes:
        raise HTTPException(409, "connect Batiyao for sharing first")
    mode = "story" if mode == "story" else "post"
    text = (text or "").strip()
    if mode == "post":
        if len(text) > POST_MAX_CHARS:
            raise HTTPException(400, f"a post is at most {POST_MAX_CHARS} characters")
        if re.search(r"(^|\s)#\w", text):
            raise HTTPException(400, "Batiyao adds hashtags itself and refuses typed ones — "
                                     "remove the #")
        if not confirm_spend:
            raise HTTPException(400, "a post costs 1.0 BuckBuck — confirm to publish")
        if rating not in ("sfw", "suggestive", "nsfw"):
            raise HTTPException(400, "pick a content rating")
    elif rating != "sfw":
        raise HTTPException(400, "stories reach everyone, so only safe-for-everyone items can "
                                 "be a story; share it as a post")
    item = resolve(user, kind, item_id)
    share_id = db.new_id("bsh")
    db.run("INSERT INTO batiyao_shares (id, user_id, kind, item_id, mode, status, created_at) "
           "VALUES (?,?,?,?,?,?,?)", (share_id, user.id, kind, item_id, mode, "running", time.time()))
    why = (reasoning or "").strip() or (f"The person chose '{rating}' for this {item['media']} "
                                         f"they made in Story Studio.")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, mime = await asyncio.to_thread(prepare, item, mode, Path(tmp))
            data = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
        fname = re.sub(r"[^\w.-]+", "_", item["title"])[:60] + Path(path.name).suffix
        if mode == "story":
            res = await _api(user.id, "POST", "/v1/stories", {
                "filename": fname, "content_type": mime, "data_base64": data,
                "content_rating": "sfw", "rating_reasoning": why}, timeout=600)
            result = str(_unwrap(res))
        else:
            up = _unwrap(await _api(user.id, "POST", "/v1/media", {
                "filename": fname, "content_type": mime, "data_base64": data}, timeout=600))
            ref = [ln.strip() for ln in str(up).splitlines() if ln.strip()][-1]
            res = await _api(user.id, "POST", "/v1/posts", {
                "content": text, "content_rating": rating, "rating_reasoning": why,
                "media_urls": [ref]})
            result = str(_unwrap(res))
        db.run("UPDATE batiyao_shares SET status='done', result=? WHERE id=?", (result[:2000], share_id))
        log.info("shared %s %s as a %s for %s", kind, item_id, mode, user.username)
        return {"ok": True, "mode": mode, "result": result[:1000]}
    except HTTPException as exc:
        db.run("UPDATE batiyao_shares SET status='failed', result=? WHERE id=?",
               (str(exc.detail)[:2000], share_id))
        raise


@router.post("/api/batiyao/share")
async def api_share(request: Request):
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401)
    try:
        b = await request.json()
    except ValueError:
        b = {}
    return await share(user, str(b.get("kind") or ""), str(b.get("id") or ""),
                       str(b.get("mode") or "post"), str(b.get("text") or ""),
                       str(b.get("rating") or "sfw"), str(b.get("reasoning") or ""),
                       bool(b.get("confirm_spend")))


@router.get("/api/batiyao/item")
async def api_item(request: Request, kind: str, id: str):
    """What the share sheet needs to know before it offers Post/Story."""
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401)
    item = resolve(user, kind, id)
    secs = 0.0
    if item["media"] != "image" and item["path"].exists():
        secs = (await asyncio.to_thread(_probe, item["path"]))[0]
    return {"title": item["title"], "media": item["media"], "seconds": round(secs, 1),
            "story_ok": item["media"] == "image" or secs <= STORY_MAX_S + 0.5}
