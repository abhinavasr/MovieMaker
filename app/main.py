"""Story Studio -- a small front door onto the WIS video API.

Two modes, because the model behind it genuinely has two:

  storyboard  one image per shot, each animated for ~5 s and joined with
              ffmpeg. Any story length. Silent unless you upload a soundtrack.
  single      up to 9 reference images in ONE ~15 s clip with native stereo
              audio, addressed from the prompt as <Picture 1>..<Picture 9>.

Accounts are issued by an admin and expire. There is no self-registration.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app import (agent, agentfiles, auth, batiyao, editor, db, gpuqueue, images, mcp, music, oauth, podcast, render, service, speech, styles,
                 voices, wis)
from app.config import cfg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("studio")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["keep_a_copy"] = service.keep_a_copy_note


from app.main_assets import ASSET_V  # noqa: E402
templates.env.globals["asset_v"] = ASSET_V
templates.env.globals["batiyao_on"] = lambda: batiyao.enabled()



@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg.validate()
    db.init()
    auth.ensure_admin()
    auth.purge_expired()
    if not render.ffmpeg_available():
        log.error("ffmpeg is not on PATH -- storyboard mode cannot join clips")
    task = asyncio.create_task(render.worker())
    gpuqueue.register("speech", speech.run)
    gpuqueue.register("music", music.run)
    gpuqueue.register("voice_jobs", voices.run)
    gpu_task = asyncio.create_task(gpuqueue.worker())
    asyncio.create_task(_reap_loop())
    agent_tasks = [asyncio.create_task(editor.pending_loop())]
    if cfg.AGENT_WORKER:
        agent_tasks += [asyncio.create_task(agent.worker()), asyncio.create_task(agent.monitor()),
                        asyncio.create_task(podcast.worker())]
    else:
        log.info("agent worker off (STUDIO_AGENT_WORKER=0)")
    log.info("story studio up; upstream %s", cfg.WIS_BASE_URL)
    yield
    render.stop()
    task.cancel()
    gpu_task.cancel()
    for t in agent_tasks:
        t.cancel()
    # Hand back any GPU turn this process holds, so a stopped or redeployed studio never
    # leaves the card held for nobody until the lease times out.
    try:
        await asyncio.wait_for(asyncio.gather(agent.release_leftover_lease(),
                                              gpuqueue.release_leftover_lease(),
                                              return_exceptions=True), 8)
    except Exception:                                          # noqa: BLE001
        pass


app = FastAPI(title="Story Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
# --------------------------------------------------------------------------- CORS
# Browser apps (e.g. a sandboxed mini app, whose Origin is "null") call the PUBLIC API
# cross-origin, and the browser first sends an OPTIONS preflight -- which used to get a
# 405, so every call was blocked (seen 2026-10-25). These paths authenticate with a
# bearer token, never a cookie, so any origin may call them, but WITHOUT credentials:
# "*" is never combined with Allow-Credentials, so a website can't ride a signed-in
# browser session; it needs a token of its own. The pages' cookie API (/api/*) gets no
# CORS at all.
CORS_PREFIXES = ("/mcp", "/v1/", "/oauth/", "/.well-known/", "/dev", "/i/", "/s/", "/m/",
                 "/v/", "/a/")
CORS_ROOT = {"/", "/healthz", "/install"}
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id, Mcp-Protocol-Version, "
                                     "Retry-After, Content-Disposition",
}


def _cors_path(path: str) -> bool:
    return path in CORS_ROOT or path.startswith(CORS_PREFIXES)


@app.middleware("http")
async def _cors(request: Request, call_next):
    path = request.url.path
    if not _cors_path(path):
        return await call_next(request)
    if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        asked = request.headers.get("access-control-request-headers", "")
        return Response(status_code=204, headers={
            **CORS_HEADERS,
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": asked or "Authorization, Content-Type, Accept, "
                                            "Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-ID",
            "Access-Control-Max-Age": "86400"})
    response = await call_next(request)
    for k, v in CORS_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


app.include_router(oauth.router)
app.include_router(mcp.router)
app.include_router(voices.router)
app.include_router(styles.router)
app.include_router(agent.router)
app.include_router(editor.router)
app.include_router(batiyao.router)
app.include_router(podcast.router)


async def _reap_loop() -> None:
    """Delete aged projects and dead sessions once an hour."""
    while True:
        try:
            auth.purge_expired()
            cutoff = db.now() - cfg.RETENTION_DAYS * 86400
            for row in db.query("SELECT id FROM projects WHERE updated_at < ?", (cutoff,)):
                _delete_project_files(row["id"])
                db.run("DELETE FROM projects WHERE id=?", (row["id"],))
                db.run("DELETE FROM shots WHERE project_id=?", (row["id"],))
                db.run("DELETE FROM assets WHERE project_id=?", (row["id"],))
            images.purge_older_than(cutoff)
            speech.purge_older_than(cutoff)
            music.purge_older_than(cutoff)
            await voices.purge_older_than(cutoff)
            agent.purge_older_than(cutoff)     # trained voices + profiles are kept
            podcast.purge_older_than(cutoff)
        except Exception:                                      # noqa: BLE001
            log.exception("reaper survived an error")
        await asyncio.sleep(3600)


# --------------------------------------------------------------------------- helpers

def _user(request: Request) -> auth.User:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401)
    return user


def _admin(request: Request) -> auth.User:
    user = _user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    return user


def _project(request: Request, project_id: str):
    """A project the signed-in user is allowed to see, and that user."""
    user = _user(request)
    return service.owned_project(user, project_id), user


_delete_project_files = service.delete_project_files


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        # The public API: always JSON, with the machine codes docs/VOICE_API_SPEC.md §9.2 lists.
        code = {400: "invalid_request", 401: "unauthorized", 404: "not_found",
                409: "wrong_state", 413: "too_large", 422: "rejected_clip", 429: "rate_limited",
                502: "unavailable", 503: "unavailable"}.get(exc.status_code, "error")
        detail = exc.detail if exc.status_code != 401 else "not signed in"
        if exc.status_code == 400 and "not enough audio" in str(detail):
            code = "not_enough_audio"
        headers = {"Retry-After": "600"} if exc.status_code == 429 else None
        return JSONResponse({"error": detail, "code": code}, status_code=exc.status_code,
                            headers=headers)
    if exc.status_code == 401:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    # /u/ is the signed upload link: its caller is curl, which reads JSON.
    if request.headers.get("accept", "").startswith("application/json") \
            or request.url.path.startswith(("/api/", "/u/")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        "error.html", {"request": request, "code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code)


# --------------------------------------------------------------------------- auth

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/admin" if user.is_admin else "/assistant", status_code=303)


@app.get("/edit", response_class=HTMLResponse)
async def editor_list(request: Request):
    user = _user(request)
    prods = [{"id": p["id"], "title": p["title"], "status": p["status"],
              "updated": _iso(p["updated_at"]), "thumb": editor.first_picture(p["id"]),
              "shots": db.one("SELECT COUNT(*) AS n FROM shots WHERE project_id=?", (p["id"],))["n"]}
             for p in editor.productions_of(user)]
    lib = editor.library_project(user, create=False)
    others = [p for p in db.query("SELECT id, title, updated_at FROM projects WHERE user_id=? AND "
                                  "mode='storyboard' ORDER BY updated_at DESC LIMIT 30", (user.id,))
              if p["id"] not in {x["id"] for x in prods} and p["id"] != lib]
    return templates.TemplateResponse("editor_list.html", {
        "request": request, "user": user, "productions": prods, "others": others})


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    """The person's Library: their characters, places, props, styles and voices for every
    project, edited with the editor's own tools (library mode)."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login?next=/library", status_code=303)
    return RedirectResponse(f"/edit/{editor.library_project(user)}", status_code=303)


@app.get("/edit/{project_id}", response_class=HTMLResponse)
async def editor_page(project_id: str, request: Request):
    user = _user(request)
    project = editor.production(user, project_id)
    return templates.TemplateResponse("editor.html", {
        "library": editor.is_library(project),
        "request": request, "user": user, "project": project, "planner": cfg.PLANNER_MODEL,
        "executor": cfg.EXECUTOR_MODEL,
        "accept": ",".join(agentfiles.ACCEPT) + ",image/*,audio/*,video/*"})


@app.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request):
    user = _user(request)
    return templates.TemplateResponse("assistant.html", {
        "request": request, "user": user, "planner": cfg.PLANNER_MODEL,
        "executor": cfg.EXECUTOR_MODEL, "review_min": max(1, cfg.AGENT_REVIEW_S // 60),
        "accept": ",".join(agentfiles.ACCEPT) + ",image/*,audio/*,video/*"})


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = ""):
    if auth.current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    msg = {"cancelled": "Batiyao sign-in was cancelled.",
           "batiyao": "Batiyao sign-in did not complete; try again.",
           "disabled": "This account is disabled or has expired."}.get(error, error)
    return templates.TemplateResponse("login.html", {"request": request, "error": msg,
                                                     "next": request.query_params.get("next", "")})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user, reason = auth.authenticate(username.strip(), password)
    if user is None:
        log.info("login refused for %r: %s", username[:40], reason)
        # One message for every failure -- see auth.authenticate.
        return templates.TemplateResponse(
            "login.html",
            {"request": request,
             "error": "That username and password did not work, or the account has expired."},
            status_code=401)
    token = auth.start_session(user.id)
    target = "/admin" if user.is_admin else "/studio"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("studio_session", token, httponly=True, samesite="lax",
                    secure=cfg.COOKIE_SECURE, max_age=cfg.SESSION_TTL_HOURS * 3600)
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("studio_session")
    if token:
        auth.end_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("studio_session")
    return resp


# --------------------------------------------------------------------------- admin

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, created: str = "", password: str = ""):
    user = _admin(request)
    users = db.query(
        "SELECT u.*, (SELECT COUNT(*) FROM projects p WHERE p.user_id=u.id) AS projects "
        "FROM users u WHERE u.role!='admin' ORDER BY u.created_at DESC")
    rows = []
    for row in users:
        expired = row["expires_at"] is not None and row["expires_at"] < db.now()
        rows.append({
            "id": row["id"], "username": row["username"], "note": row["note"] or "",
            "created": _iso(row["created_at"]), "expires": _iso(row["expires_at"]) or "never",
            "projects": row["projects"], "unlimited": bool(row["unlimited"]),
            "state": "disabled" if row["disabled"] else ("expired" if expired else "active"),
        })
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "users": rows,
        "created": created, "password": password,
        "queue": render.queue_depth(),
        "renders_per_hour": cfg.USER_RENDERS_PER_HOUR,
        "images_per_hour": cfg.USER_IMAGES_PER_HOUR,
    })


@app.post("/admin/users")
async def admin_create(request: Request, username: str = Form(...),
                       password: str = Form(""), days: int = Form(7),
                       note: str = Form("")):
    _admin(request)
    username = username.strip()
    if not username or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "username must be letters, digits, dot, dash or underscore")
    if db.one("SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)):
        raise HTTPException(400, f"'{username}' already exists")
    password = password.strip()
    generated = ""
    if not password:
        # Readable rather than maximal: these get typed by hand or read down a
        # phone, and a password that is retyped wrong three times gets written
        # on a sticky note instead.
        import secrets
        words = ("amber", "basalt", "cobalt", "delta", "ember", "fjord", "gale",
                 "harbor", "indigo", "juniper", "kestrel", "lumen")
        password = "-".join(secrets.choice(words) for _ in range(3)) + \
                   f"-{secrets.randbelow(90) + 10}"
        generated = password
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    expires = db.now() + int(days) * 86400 if int(days) > 0 else None
    auth.create_user(username, password, expires_at=expires, note=note.strip()[:200])
    log.info("admin created user %s (expires %s)", username, _iso(expires) or "never")
    # The generated password is shown once, on the redirect, and never stored in
    # a readable form -- the hash is all that is kept.
    return RedirectResponse(f"/admin?created={username}&password={generated}",
                            status_code=303)


@app.post("/admin/users/{user_id}/{action}")
async def admin_action(request: Request, user_id: str, action: str,
                       password: str = Form(""), days: int = Form(0)):
    _admin(request)
    row = db.one("SELECT * FROM users WHERE id=? AND role!='admin'", (user_id,))
    if row is None:
        raise HTTPException(404, "no such user")
    if action == "disable":
        db.run("UPDATE users SET disabled=1 WHERE id=?", (user_id,))
        auth.revoke_sessions(user_id)
    elif action == "enable":
        db.run("UPDATE users SET disabled=0 WHERE id=?", (user_id,))
    elif action in ("unlimited", "limited"):
        db.run("UPDATE users SET unlimited=? WHERE id=?",
               (1 if action == "unlimited" else 0, user_id))
    elif action == "extend":
        base = max(row["expires_at"] or db.now(), db.now())
        db.run("UPDATE users SET expires_at=? WHERE id=?",
               (base + max(1, int(days)) * 86400, user_id))
    elif action == "password":
        if len(password.strip()) < 8:
            raise HTTPException(400, "password must be at least 8 characters")
        db.run("UPDATE users SET password_hash=? WHERE id=?",
               (auth.hash_password(password.strip()), user_id))
        auth.revoke_sessions(user_id)
    elif action == "delete":
        for proj in db.query("SELECT id FROM projects WHERE user_id=?", (user_id,)):
            _delete_project_files(proj["id"])
            db.run("DELETE FROM shots WHERE project_id=?", (proj["id"],))
            db.run("DELETE FROM assets WHERE project_id=?", (proj["id"],))
        db.run("DELETE FROM projects WHERE user_id=?", (user_id,))
        images.delete_for_user(user_id)
        speech.delete_for_user(user_id)
        music.delete_for_user(user_id)
        await voices.delete_for_user(user_id)
        for thr in db.query("SELECT id FROM agent_threads WHERE user_id=?", (user_id,)):
            agent.delete_thread(thr["id"])
        db.run("DELETE FROM agent_lessons WHERE user_id=?", (user_id,))
        db.run("DELETE FROM users WHERE id=?", (user_id,))
        auth.revoke_sessions(user_id)
    else:
        raise HTTPException(400, f"unknown action '{action}'")
    log.info("admin %s on user %s", action, row["username"])
    return RedirectResponse("/admin", status_code=303)


# --------------------------------------------------------------------------- studio

@app.get("/studio", response_class=HTMLResponse)
async def studio(request: Request):
    user = _user(request)
    projects = db.query(
        "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC LIMIT 60", (user.id,))
    return templates.TemplateResponse("studio.html", {
        "request": request, "user": user,
        "projects": [dict(p, created=_iso(p["created_at"]),
                          deletes=service.retention(p["updated_at"])) for p in
                     ({k: r[k] for k in r.keys()} for r in projects)],
        "queue": render.queue_depth(),
        "expires": _iso(user.expires_at) or "never",
    })


@app.post("/projects")
async def create_project(request: Request, title: str = Form(...), mode: str = Form("storyboard")):
    user = _user(request)
    project_id = service.create_project(user, title, mode)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    project, user = _project(request, project_id)
    assets = db.query("SELECT * FROM assets WHERE project_id=? ORDER BY kind, idx", (project_id,))
    shots = db.query("SELECT * FROM shots WHERE project_id=? ORDER BY idx", (project_id,))
    params = db.params_of(project)
    return templates.TemplateResponse("project.html", {
        "request": request, "user": user,
        "p": {k: project[k] for k in project.keys()},
        "deletes": service.retention(project["updated_at"]),
        "params": params,
        "images": [{k: a[k] for k in a.keys()} for a in assets if a["kind"] == "image"],
        "audios": [{k: a[k] for k in a.keys()} for a in assets if a["kind"] == "audio"],
        "shots": [{k: s[k] for k in s.keys()} for s in shots],
        "max_images": service.limits(project["mode"])[0],
        "max_audio": service.limits(project["mode"])[1],
        "queue": render.queue_depth(),
    })


@app.post("/projects/{project_id}/upload")
async def upload(request: Request, project_id: str, files: list[UploadFile] = []):
    project, _ = _project(request, project_id)
    service.add_files(project, [(f.filename or "", f.content_type or "", await f.read())
                                for f in files])
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/assets/{asset_id}/delete")
async def delete_asset(request: Request, project_id: str, asset_id: str):
    project, _ = _project(request, project_id)
    if project["status"] in ("queued", "running"):
        raise HTTPException(409, "this project is rendering")
    row = db.one("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, project_id))
    if row is None:
        raise HTTPException(404, "no such file")
    Path(row["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM assets WHERE id=?", (asset_id,))
    db.run("DELETE FROM shots WHERE asset_id=?", (asset_id,))
    # Re-index so the remaining files stay 0..n-1. Positions are what the
    # prompt's <Picture i> tags refer to, so a hole would silently renumber
    # every reference after it.
    for i, rest in enumerate(db.query(
            "SELECT id FROM assets WHERE project_id=? AND kind=? ORDER BY idx",
            (project_id, row["kind"]))):
        db.run("UPDATE assets SET idx=? WHERE id=?", (i, rest["id"]))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/settings")
async def save_settings(request: Request, project_id: str, title: str = Form(...),
                        story: str = Form(""), orientation: str = Form("portrait"),
                        seconds: int = Form(5), continuity: str = Form(""),
                        ref_image_size: str = Form("match"), upscale: str = Form("off")):
    project, _ = _project(request, project_id)
    service.save_settings(project, title=title, story=story, orientation=orientation,
                          seconds=seconds, continuity=continuity == "on",
                          ref_image_size=ref_image_size, upscale=upscale)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/plan")
async def plan_shots(request: Request, project_id: str):
    project, _ = _project(request, project_id)
    await service.plan_shots(project)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/shots/blank")
async def blank_shots(request: Request, project_id: str):
    project, _ = _project(request, project_id)
    service.blank_shots(project)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/shots")
async def save_shots(request: Request, project_id: str):
    project, _ = _project(request, project_id)
    form = await request.form()
    for row in db.query("SELECT id FROM shots WHERE project_id=?", (project_id,)):
        prompt = form.get(f"prompt_{row['id']}")
        seconds = form.get(f"seconds_{row['id']}")
        title = form.get(f"title_{row['id']}")
        if prompt is not None:
            db.run("UPDATE shots SET prompt=?, seconds=?, title=? WHERE id=?",
                   (str(prompt), service.clamp_seconds("storyboard", seconds or cfg.DEFAULT_SHOT_SECONDS),
                    str(title or "")[:80], row["id"]))
    db.touch_project(project_id, message="shots saved")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/render")
async def start_render(request: Request, project_id: str):
    project, user = _project(request, project_id)
    depth = await service.start_render(project, user)
    log.info("user %s queued %s (depth %d)", user.username, project_id, depth)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/cancel")
async def cancel(request: Request, project_id: str):
    project, _ = _project(request, project_id)
    if render.request_cancel(project_id):
        db.touch_project(project_id, message="cancelling…")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/delete")
async def delete_project(request: Request, project_id: str):
    project, user = _project(request, project_id)
    if project["mode"] == "storyboard":
        # Its pictures, voicing and assistant stop first (see editor.api_delete_production).
        from app import editor
        try:
            await editor.stop_all(project, user)
        except Exception:                                      # noqa: BLE001
            log.warning("stopping %s before deleting it failed", project_id, exc_info=True)
    if project["status"] in ("queued", "running"):
        render.request_cancel(project_id)
    _delete_project_files(project_id)
    db.run("DELETE FROM shots WHERE project_id=?", (project_id,))
    db.run("DELETE FROM assets WHERE project_id=?", (project_id,))
    db.run("DELETE FROM projects WHERE id=?", (project_id,))
    return RedirectResponse("/studio", status_code=303)


# --------------------------------------------------------------------------- reads

@app.get("/api/projects/{project_id}/status")
async def project_status(request: Request, project_id: str):
    project, _ = _project(request, project_id)
    shots = db.query("SELECT idx,status,error,notes FROM shots WHERE project_id=? ORDER BY idx",
                     (project_id,))
    return {
        "status": project["status"], "progress": project["progress"],
        "message": project["message"], "error": project["error"],
        "has_video": bool(project["output_path"]),
        "has_audio": bool(project["has_audio"]),
        "queue": render.queue_depth(),
        "running": render.current_project() == project_id,
        "shots": [{"idx": s["idx"], "status": s["status"], "error": s["error"]} for s in shots],
    }


@app.get("/projects/{project_id}/asset/{asset_id}")
async def get_asset(request: Request, project_id: str, asset_id: str):
    _project(request, project_id)
    row = db.one("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, project_id))
    if row is None or not Path(row["path"]).exists():
        raise HTTPException(404, "no such file")
    return FileResponse(row["path"], media_type=row["mime"] or "application/octet-stream")


@app.get("/projects/{project_id}/video")
async def get_video(request: Request, project_id: str, download: int = 0):
    project, _ = _project(request, project_id)
    path = project["output_path"]
    if not path or not Path(path).exists():
        raise HTTPException(404, "this project has no finished video")
    safe = "".join(c for c in (project["title"] or "video") if c.isalnum() or c in " -_").strip()
    return FileResponse(
        path, media_type="video/mp4",
        filename=f"{safe or 'video'}.mp4" if download else None)


# --------------------------------------------------------------------------- speech & music pages
# Same rules as the MCP tools: both front doors call app.speech / app.music.

def _speech_json(row) -> dict:
    out = {"id": row["id"], "status": row["status"], "text": row["text"],
           "voice": row["voice"], "language": row["language"], "instruct": row["instruct"],
           "error": row["error"], "message": row["message"], "duration_s": row["duration_s"],
           "created": _iso(row["created_at"]), "params": speech.params(row),
           "reference_text": row["reference_text"],
           "position": gpuqueue.position("speech", row["id"]),
           "deletes": service.retention(row["updated_at"])}
    if row["status"] == "succeeded":
        out["audio"] = f"/speech/{row['id']}/audio"
    return out


def _music_json(row) -> dict:
    out = {"id": row["id"], "status": row["status"], "task": row["task"],
           "task_label": music.TASKS.get(row["task"], row["task"]), "title": row["title"],
           "params": json.loads(row["params"]), "error": row["error"],
           "message": row["message"], "duration_s": row["duration_s"],
           "created": _iso(row["created_at"]), "result": music.meta(row),
           "position": gpuqueue.position("music", row["id"]),
           "deletes": service.retention(row["updated_at"])}
    if row["status"] == "succeeded":
        out["audio"] = f"/music/{row['id']}/audio"
    return out


def _projects_of(user) -> list[dict]:
    return [{"id": r["id"], "title": r["title"], "mode": r["mode"]} for r in db.query(
        "SELECT id, title, mode FROM projects WHERE user_id=? ORDER BY updated_at DESC LIMIT 30",
        (user.id,))]


@app.get("/speech", response_class=HTMLResponse)
async def speech_page(request: Request):
    user = _user(request)
    return templates.TemplateResponse("speech.html", {
        "request": request, "user": user, "speakers": speech.SPEAKERS,
        "default_speaker": speech.DEFAULT_SPEAKER, "languages": speech.LANGUAGES,
        "burmese_speakers": list(speech.BURMESE_SPEAKERS),
        "hindi_speakers": list(speech.HINDI_SPEAKERS),
        "meters": speech.METERS,
        "max_text": speech.MAX_TEXT, "per_hour": cfg.USER_SPEECH_PER_HOUR,
        "projects": _projects_of(user)})


@app.get("/api/speech")
async def speech_list(request: Request):
    user = _user(request)
    rows = db.query("SELECT * FROM speech WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                    (user.id,))
    return {"items": [_speech_json(r) for r in rows]}


@app.post("/api/speech")
async def speech_create(request: Request, text: str = Form(""), speaker: str = Form(""),
                        language: str = Form("Auto"), instruct: str = Form(""),
                        reference_text: str = Form(""), match: str = Form("style"),
                        temperature: str = Form(""), seed: str = Form(""),
                        takes: str = Form("1"), reference: UploadFile | None = None,
                        meter: str = Form("")):
    user = _user(request)
    ref = await reference.read() if reference is not None and reference.filename else None
    ids = await speech.create(
        user, text, speaker=None if ref else (speaker or None), language=language,
        instruct=None if ref else instruct, reference=ref, reference_text=reference_text,
        match=match if ref else None, temperature=temperature or None, seed=seed or None,
        takes=takes or 1, meter=(meter or None) if language == speech.SANSKRIT else None)
    return {"ids": ids}


@app.post("/api/speech/transcribe")
async def speech_transcribe(request: Request, reference: UploadFile):
    """Fill in the transcript box the moment a clip is chosen (Whisper, on the CPU)."""
    _user(request)
    return await speech.transcribe(await reference.read())


@app.get("/speech/{speech_id}/audio")
async def speech_audio(request: Request, speech_id: str, download: int = 0):
    row = speech.owned(_user(request), speech_id)
    if not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(404, "not finished")
    return FileResponse(row["path"], media_type="audio/mpeg",
                        filename=f"{speech_id}.mp3" if download else None)


@app.post("/api/speech/{speech_id}/delete")
async def speech_delete(request: Request, speech_id: str):
    speech.delete(_user(request), speech_id)
    return {"ok": True}


@app.post("/api/speech/{speech_id}/to-project")
async def speech_to_project(request: Request, speech_id: str, project_id: str = Form(...)):
    user = _user(request)
    project = service.owned_project(user, project_id)
    speech.add_to_project(user, speech_id, project)
    return {"ok": True, "project": f"/projects/{project_id}"}


# --------------------------------------------------------------------------- developer docs
# Public on purpose (the user's call, 2026-10-25): the API spec for whoever builds an app.
# Read from ./docs at request time (mounted read-only), so a rebuilt spec shows at once.
DOCS = Path("/srv/docs")
DEV_FILES = {"STUDIO_API_SPEC.md", "VOICE_API_SPEC.md"}


@app.get("/dev", response_class=HTMLResponse)
async def dev_docs(request: Request):
    spec = DOCS / "STUDIO_API_SPEC.md"
    if not spec.exists():
        raise HTTPException(404, "the developer docs are not published on this server")
    data = json.dumps({"full": spec.read_text()}).replace("</", "<\\/")
    built = datetime.fromtimestamp(spec.stat().st_mtime, timezone.utc).strftime("%d %b %Y %H:%M UTC")
    return templates.TemplateResponse("dev.html", {"request": request, "data": data, "built": built,
                                                   "base": service.base_url(request)})


@app.get("/dev/{name}")
async def dev_file(name: str, download: int = 0):
    if name not in DEV_FILES or not (DOCS / name).exists():
        raise HTTPException(404, "no such document")
    return FileResponse(DOCS / name, media_type="text/markdown; charset=utf-8",
                        filename=name if download else None)


@app.get("/voices", response_class=HTMLResponse)
async def voices_page(request: Request):
    user = _user(request)
    return templates.TemplateResponse("voices.html", {
        "request": request, "user": user, "consent_text": voices.CONSENT_TEXT,
        "consent_version": voices.CONSENT_VERSION, "max_voices": voices.MAX_VOICES})


@app.get("/voices/{voice_id}", response_class=HTMLResponse)
async def voice_page(request: Request, voice_id: str):
    user = _user(request)
    v = voices.owned(user, voice_id)
    songs = db.query("SELECT id, title, duration_s FROM music WHERE user_id=? AND "
                     "status='succeeded' ORDER BY created_at DESC LIMIT 40", (user.id,))
    return templates.TemplateResponse("voice.html", {
        "request": request, "user": user, "v": voices.voice_view(v),
        "songs": [dict(s) for s in songs], "min_minutes": voices.MIN_TRAIN_S // 60,
        "projects": _projects_of(user)})


@app.get("/a/{token}/{exp}/{sig}/audio.mp3")
async def signed_voice_audio(token: str, exp: int, sig: str):
    """A voice result (mix or stem), for a link handed out by an assistant."""
    if not service.signed_ok("voice", token, exp, sig):
        raise HTTPException(404, "this link is not valid, or it has expired")
    return voices.signed_file(token)


@app.get("/music", response_class=HTMLResponse)
async def music_page(request: Request):
    user = _user(request)
    return templates.TemplateResponse("music.html", {
        "request": request, "user": user, "tasks": music.TASKS, "tracks": music.TRACKS,
        "languages": music.LANGUAGES, "max_lyrics": music.MAX_LYRICS,
        "max_prompt": music.MAX_PROMPT, "min_s": music.MIN_S, "max_s": music.MAX_S,
        "per_hour": cfg.USER_MUSIC_PER_HOUR, "projects": _projects_of(user)})


@app.get("/api/music")
async def music_list(request: Request):
    user = _user(request)
    rows = db.query("SELECT * FROM music WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                    (user.id,))
    return {"items": [_music_json(r) for r in rows]}


@app.post("/api/music")
async def music_create(request: Request):
    user = _user(request)
    form = await request.form()

    async def audio(prefix: str) -> bytes | None:
        upload = form.get(f"{prefix}_file")
        if upload is not None and getattr(upload, "filename", ""):
            return await upload.read()
        if form.get(f"{prefix}_music_id"):
            return music.source_from_music(user, str(form[f"{prefix}_music_id"]))
        return None

    music_id = await music.create(
        user, task=str(form.get("task") or "custom"),
        description=str(form.get("description") or ""), style=str(form.get("style") or ""),
        lyrics=str(form.get("lyrics") or ""), instrumental=form.get("instrumental") == "on",
        language=str(form.get("language") or "") or None,
        duration=form.get("duration") or None, bpm=form.get("bpm") or None,
        key=str(form.get("key") or ""), time_signature=str(form.get("time_signature") or ""),
        seed=form.get("seed") or None, strength=form.get("strength") or None,
        start=form.get("start") or None, end=form.get("end") or None,
        track=str(form.get("track") or ""), instruments=form.getlist("instruments"),
        add_seconds=form.get("add_seconds") or None, title=str(form.get("title") or ""),
        source=await audio("source"),
        source_music_id=None if (getattr(form.get("source_file"), "filename", "")
                                 or not form.get("source_music_id"))
        else str(form["source_music_id"]),
        reference=await audio("reference"))
    return {"id": music_id}


@app.post("/api/music/lyrics")
async def music_lyrics(request: Request):
    """The words of a track: a studio song's stored lyrics, or an upload transcribed."""
    user = _user(request)
    form = await request.form()
    upload = form.get("source_file")
    if upload is not None and getattr(upload, "filename", ""):
        wav = await music._to_wav(await upload.read(), "track")
        return {"lyrics": await music.transcribe_source(wav), "transcribed": True}
    if form.get("source_music_id"):
        return {"lyrics": music.source_meta(user, str(form["source_music_id"])).get("lyrics")
                or "", "transcribed": False}
    raise HTTPException(400, "choose a track first")


@app.get("/music/{music_id}/audio")
async def music_audio(request: Request, music_id: str, download: int = 0):
    row = music.owned(_user(request), music_id)
    if not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(404, "not finished")
    safe = "".join(c for c in (row["title"] or "song") if c.isalnum() or c in " -_").strip()
    return FileResponse(row["path"], media_type="audio/mpeg",
                        filename=f"{safe or 'song'}.mp3" if download else None)


@app.post("/api/music/{music_id}/delete")
async def music_delete(request: Request, music_id: str):
    music.delete(_user(request), music_id)
    return {"ok": True}


@app.post("/api/music/{music_id}/to-project")
async def music_to_project(request: Request, music_id: str, project_id: str = Form(...)):
    user = _user(request)
    project = service.owned_project(user, project_id)
    music.add_to_project(user, music_id, project)
    return {"ok": True, "project": f"/projects/{project_id}"}


# --------------------------------------------------------------------------- assistants

def _install_context(request: Request) -> dict:
    base = service.base_url(request)
    return {"request": request, "base": base, "user": auth.current_user(request),
            "tools": [(t["spec"]["name"], t["spec"]["description"]) for t in mcp.TOOLS]}


@app.get("/install")
async def install(request: Request):
    """How to connect an assistant -- written to be read BY the assistant.

    "Install studio.example.com/install" is something a person says to Claude
    Code or ChatGPT, which then fetches this address. A program gets the
    instructions as plain markdown; a browser gets the same thing as a page.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "text/markdown" not in accept:
        return templates.TemplateResponse("install.html", _install_context(request))
    return await install_md(request)


@app.get("/install.md")
async def install_md(request: Request):
    text = templates.get_template("install.md").render(**_install_context(request))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@app.get("/v/{project_id}/{exp}/{sig}/video.mp4")
async def signed_video(project_id: str, exp: int, sig: str, download: int = 0):
    """The finished video, for a link handed out by an assistant."""
    project = service.check_signed("video", project_id, exp, sig)
    path = project["output_path"]
    if not path or not Path(path).exists():
        raise HTTPException(404, "this project has no finished video")
    safe = "".join(c for c in (project["title"] or "video") if c.isalnum() or c in " -_").strip()
    return FileResponse(path, media_type="video/mp4",
                        filename=f"{safe or 'video'}.mp4" if download else None)


@app.get("/i/{image_id}/{exp}/{sig}/image.png")
async def signed_image(image_id: str, exp: int, sig: str, download: int = 0):
    """A generated picture, for a link handed out by an assistant."""
    row = db.one("SELECT * FROM images WHERE id=?", (image_id,)) \
        if service.signed_ok("image", image_id, exp, sig) else None
    if row is None or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(404, "this link is not valid, or it has expired")
    return FileResponse(row["path"], media_type="image/png",
                        filename=f"{image_id}.png" if download else None)


@app.get("/s/{speech_id}/{exp}/{sig}/speech.mp3")
async def signed_speech(speech_id: str, exp: int, sig: str, download: int = 0):
    """A generated speech clip, for a link handed out by an assistant."""
    row = db.one("SELECT * FROM speech WHERE id=?", (speech_id,)) \
        if service.signed_ok("speech", speech_id, exp, sig) else None
    if row is None or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(404, "this link is not valid, or it has expired")
    return FileResponse(row["path"], media_type="audio/mpeg",
                        filename=f"{speech_id}.mp3" if download else None)


@app.get("/m/{music_id}/{exp}/{sig}/song.mp3")
async def signed_music(music_id: str, exp: int, sig: str, download: int = 0):
    """A generated song, for a link handed out by an assistant."""
    row = db.one("SELECT * FROM music WHERE id=?", (music_id,)) \
        if service.signed_ok("music", music_id, exp, sig) else None
    if row is None or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(404, "this link is not valid, or it has expired")
    return FileResponse(row["path"], media_type="audio/mpeg",
                        filename=f"{music_id}.mp3" if download else None)


@app.post("/u/{project_id}/{exp}/{sig}")
async def signed_upload(project_id: str, exp: int, sig: str, files: list[UploadFile] = []):
    """Pictures and audio, for an upload link handed out by an assistant."""
    project = service.check_signed("upload", project_id, exp, sig)
    try:
        added = service.add_files(project, [(f.filename or "", f.content_type or "",
                                             await f.read()) for f in files])
    except HTTPException as exc:
        # Always JSON here: the caller is curl, not a browser.
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    counts = {r["kind"]: r["n"] for r in db.query(
        "SELECT kind, COUNT(*) AS n FROM assets WHERE project_id=? GROUP BY kind", (project_id,))}
    return {"added": added, "pictures": counts.get("image", 0), "audio": counts.get("audio", 0)}


@app.get("/healthz")
async def healthz():
    out = {"ok": True, "queue": render.queue_depth(), "ffmpeg": render.ffmpeg_available()}
    try:
        health = await wis.health()
        models = {m.get("id"): m for m in health.get("models", [])}
        out["wis"] = health.get("status")
        # Named explicitly so a renamed or missing model is visible here rather
        # than as a 400 somebody hits after waiting in a queue.
        out["models"] = {
            name: (models.get(name, {}).get("state") or "absent")
            for name in (cfg.STORYBOARD_MODEL, cfg.SINGLE_MODEL, cfg.IMAGE_MODEL)}
    except Exception as exc:                                   # noqa: BLE001
        out["ok"] = False
        out["wis"] = f"unreachable: {exc}"
    # Reported, never fatal: speech is an extra, and a down tts container must
    # not turn this healthcheck (and the pages and videos with it) red.
    try:
        tts = await speech.health()
        out["tts"] = f"ok ({tts.get('loaded') or 'idle'})"
    except Exception as exc:                                   # noqa: BLE001
        out["tts"] = f"unreachable: {type(exc).__name__}"
    try:
        my = await speech.health(cfg.TTS_MY_URL)
        out["tts_burmese"] = ("ok (loaded)" if my.get("loaded") else "ok (idle)") \
            if my.get("weights_present") else "weights missing"
    except Exception as exc:                                   # noqa: BLE001
        out["tts_burmese"] = f"unreachable: {type(exc).__name__}"
    try:
        out["music"] = (await music.health()).get("status") or "ok"
    except Exception as exc:                                   # noqa: BLE001
        out["music"] = f"unreachable: {type(exc).__name__}"
    return JSONResponse(out, status_code=200 if out["ok"] else 503)
