"""What a project can be asked to do, whoever is asking.

The web pages and the MCP endpoint are two front doors onto the same rooms.
Everything that decides whether a request is allowed -- the reference-slot
limits, "write the story first", the hourly render budget -- lives here once,
so an assistant cannot be let through a check the browser form enforces, or the
other way round. Refusals are raised as HTTPException because both callers
already know what to do with one: the pages render it, the MCP layer returns
its `detail` as the tool's error text.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import shutil
from pathlib import Path

from fastapi import HTTPException, Request
from PIL import Image

from app import auth, db, render, storyboard
from app.config import cfg

MAX_SINGLE_IMAGES = 9
MAX_SINGLE_AUDIO = 3
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
MODES = ("storyboard", "single")
# The three sizes the model has native buckets for. Anything else is accepted
# by the service with a warning and then usually dies on VRAM part way through,
# so it is not offered.
SIZES = {"portrait": (480, 864), "landscape": (864, 480), "square": (640, 640)}


def base_url(request: Request) -> str:
    if cfg.PUBLIC_URL:
        return cfg.PUBLIC_URL
    return f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"


def owned_project(user: auth.User, project_id: str):
    """A project this user is allowed to see.

    An unknown project and someone else's project are the same 404 on purpose:
    distinguishing them tells a stranger which ids exist.
    """
    row = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="no such project")
    return row


def is_rendering(project) -> bool:
    return project["status"] in ("queued", "running")


def images_of(project_id: str) -> list:
    return db.query("SELECT * FROM assets WHERE project_id=? AND kind='image' ORDER BY idx",
                    (project_id,))


def limits(mode: str) -> tuple[int, int]:
    """(pictures, audio files) a project of this mode takes."""
    if mode == "single":
        return MAX_SINGLE_IMAGES, MAX_SINGLE_AUDIO
    return cfg.MAX_STORYBOARD_SHOTS, 1


def delete_project_files(project_id: str) -> None:
    from app import editor            # a production's scenes, references, audio, proposals
    editor.delete_production_rows(project_id)
    for base in (cfg.uploads_dir, cfg.outputs_dir):
        shutil.rmtree(base / project_id, ignore_errors=True)


def rate_limited(user_id: str) -> bool:
    if auth.is_unlimited(user_id):
        return False
    row = db.one("SELECT COUNT(*) AS n FROM renders WHERE user_id=? AND created_at > ?",
                 (user_id, db.now() - 3600))
    return bool(row and row["n"] >= cfg.USER_RENDERS_PER_HOUR)


# --------------------------------------------------------------------------- writes

# Opt-in 2x video upscale in WIS (added 2026-11-21 for testing; off by default).
# latent: ~2.4x the render time, sharper, can invent fine texture.
# refine: ~2.7x, upscale plus a short second pass; the cleanest detail.
UPSCALE = ("off", "latent", "refine")


def max_seconds(mode: str) -> int:
    """The longest clip this mode's video model accepts (config.py says why)."""
    return cfg.SINGLE_MAX_SECONDS if mode == "single" else cfg.STORYBOARD_MAX_SECONDS


def clamp_seconds(mode: str, seconds) -> int:
    return max(1, min(max_seconds(mode), int(seconds)))


def create_project(user: auth.User, title: str, mode: str, story: str = "") -> str:
    if mode not in MODES:
        raise HTTPException(400, "mode must be 'storyboard' or 'single'")
    project_id = db.new_id("prj")
    defaults = {"width": 480, "height": 864, "continuity": False,
                "seconds": 10 if mode == "single" else cfg.DEFAULT_SHOT_SECONDS,
                "ref_image_size": "match"}
    db.run("INSERT INTO projects (id,user_id,title,mode,story,params_json,created_at,updated_at) "
           "VALUES (?,?,?,?,?,?,?,?)",
           (project_id, user.id, title.strip()[:120] or "Untitled", mode, story,
            json.dumps(defaults), db.now(), db.now()))
    return project_id


def add_files(project, files: list[tuple[str, str, bytes]]) -> int:
    """Store (filename, content type, bytes) uploads. Returns how many were kept."""
    project_id = project["id"]
    if is_rendering(project):
        raise HTTPException(409, "this project is rendering; wait or cancel it first")
    limit = cfg.MAX_UPLOAD_MB * 1024 * 1024
    dest = cfg.uploads_dir / project_id
    dest.mkdir(parents=True, exist_ok=True)

    have = {k: 0 for k in ("image", "audio")}
    for row in db.query("SELECT kind, COUNT(*) AS n FROM assets WHERE project_id=? GROUP BY kind",
                        (project_id,)):
        have[row["kind"]] = row["n"]
    max_img, max_aud = limits(project["mode"])

    added = 0
    for filename, content_type, raw in files:
        if not raw:
            continue
        if len(raw) > limit:
            raise HTTPException(413, f"{filename} is over {cfg.MAX_UPLOAD_MB} MB")
        suffix = Path(filename or "").suffix.lower()
        is_audio = suffix in AUDIO_EXT or (content_type or "").startswith("audio/")
        kind = "audio" if is_audio else "image"
        ceiling = max_aud if is_audio else max_img
        if have[kind] >= ceiling:
            raise HTTPException(
                400,
                f"this project takes at most {ceiling} {kind} file(s)"
                + (" — the model has exactly that many reference slots"
                   if project["mode"] == "single" else ""))

        idx = have[kind]
        if is_audio:
            path = dest / f"audio_{idx:02d}{suffix or '.wav'}"
            path.write_bytes(raw)
            mime = content_type or "audio/wav"
        else:
            # Normalised to RGB PNG here rather than at render time: it is the
            # one place that sees the original bytes, and it means a stray EXIF
            # rotation or a palette GIF cannot surprise the model later.
            try:
                with Image.open(io.BytesIO(raw)) as probe:
                    probe = probe.convert("RGB")
                    probe.thumbnail((2048, 2048))
                    path = dest / f"image_{idx:02d}.png"
                    probe.save(path, format="PNG")
            except Exception:                                  # noqa: BLE001
                raise HTTPException(400, f"{filename} is not a readable image")
            mime = "image/png"
        db.run("INSERT INTO assets (id,project_id,kind,idx,path,orig_name,mime,created_at) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (db.new_id("ast"), project_id, kind, idx, str(path),
                (filename or "")[:120], mime, db.now()))
        have[kind] += 1
        added += 1
    db.touch_project(project_id, message=f"{added} file(s) added")
    return added


def save_settings(project, *, title: str | None = None, story: str | None = None,
                  orientation: str | None = None, seconds: int | None = None,
                  continuity: bool | None = None, ref_image_size: str | None = None,
                  mode: str | None = None, upscale: str | None = None) -> None:
    """Change what was passed and leave the rest. None means "not mentioned"."""
    project_id = project["id"]
    params = db.params_of(project)
    if mode is not None and mode != project["mode"]:
        _switch_mode(project, mode)
        if seconds is None:
            # 10 s is the single clip's default and five of them would be a
            # long storyboard shot, so the length follows the mode unless given.
            seconds = 10 if mode == "single" else cfg.DEFAULT_SHOT_SECONDS
    if orientation is not None:
        params["width"], params["height"] = SIZES.get(orientation, SIZES["portrait"])
        params["orientation"] = orientation if orientation in SIZES else "portrait"
    if seconds is not None:
        params["seconds"] = clamp_seconds(mode or project["mode"], seconds)
    if continuity is not None:
        params["continuity"] = bool(continuity)
    if ref_image_size is not None:
        params["ref_image_size"] = ref_image_size if ref_image_size in ("match", "max") else "match"
    if upscale is not None:
        if upscale not in UPSCALE:
            raise HTTPException(400, "upscale must be off, latent or refine")
        params["upscale"] = upscale
    db.set_params(project_id, params)
    fields = {}
    if title is not None:
        fields["title"] = title.strip()[:120] or "Untitled"
    if story is not None:
        fields["story"] = story
    db.touch_project(project_id, **fields)


def _switch_mode(project, mode: str) -> None:
    """Turn a storyboard into a single clip or back, keeping pictures and story.

    The same pictures and the same words can be made either way, which is the
    whole point of offering both -- but the single clip has exactly nine picture
    slots, so that direction can be refused.
    """
    if mode not in MODES:
        raise HTTPException(400, "mode must be 'storyboard' or 'single'")
    if is_rendering(project):
        raise HTTPException(409, "this project is rendering; wait or cancel it first")
    max_img, _ = limits(mode)
    count = len(images_of(project["id"]))
    if count > max_img:
        raise HTTPException(
            400, f"this project has {count} pictures and a {mode} project takes at most "
                 f"{max_img}. Remove some on the project page first.")
    db.run("UPDATE projects SET mode=?, updated_at=? WHERE id=?",
           (mode, db.now(), project["id"]))


async def plan_shots(project) -> str:
    """Split the story into one shot per picture. Returns 'planned' or 'split'."""
    project_id = project["id"]
    if project["mode"] != "storyboard":
        raise HTTPException(400, "shot planning is for storyboard projects")
    images = images_of(project_id)
    if not images:
        raise HTTPException(400, "upload at least one image first")
    if not (project["story"] or "").strip():
        # Refused rather than "planned" into empty prompts. Planning an empty
        # story used to produce one blank shot per picture, and the render then
        # refused those blanks several clicks later -- a dead end the person had
        # no way to read backwards.
        raise HTTPException(
            400, "Write the story first — it is what gets split into one prompt per "
                 "picture. If you would rather write each shot yourself, use "
                 "'Write the shots myself' instead.")
    params = db.params_of(project)
    seconds = clamp_seconds("storyboard", params.get("seconds", cfg.DEFAULT_SHOT_SECONDS))
    shots, how = await storyboard.plan(project["story"], len(images), seconds)
    db.run("DELETE FROM shots WHERE project_id=?", (project_id,))
    for i, (shot, image) in enumerate(zip(shots, images)):
        db.run("INSERT INTO shots (id,project_id,idx,title,prompt,seconds,asset_id) "
               "VALUES (?,?,?,?,?,?,?)",
               (db.new_id("sht"), project_id, i, shot.get("title", ""), shot["prompt"],
                seconds, image["id"]))
    db.touch_project(project_id, message=(
        "shots written by the planner — edit them before rendering" if how == "planned"
        else "the planner was busy, so the story was split evenly — edit the prompts, "
             "or press Plan again once the GPU is free"))
    return how


def blank_shots(project) -> int:
    """One empty shot per picture, for writing the prompts by hand.

    The explicit version of what an empty story used to do by accident. The
    difference that matters is that this path says the prompts are yours to
    fill in, so nobody reaches the render button holding blanks they did not
    know were blank.
    """
    project_id = project["id"]
    if project["mode"] != "storyboard":
        raise HTTPException(400, "shots are for storyboard projects")
    images = images_of(project_id)
    if not images:
        raise HTTPException(400, "upload at least one image first")
    params = db.params_of(project)
    db.run("DELETE FROM shots WHERE project_id=?", (project_id,))
    for i, image in enumerate(images):
        db.run("INSERT INTO shots (id,project_id,idx,title,prompt,seconds,asset_id) "
               "VALUES (?,?,?,?,?,?,?)",
               (db.new_id("sht"), project_id, i, f"Shot {i + 1}", "",
                clamp_seconds("storyboard", params.get("seconds", cfg.DEFAULT_SHOT_SECONDS)), image["id"]))
    db.touch_project(project_id, message=(
        f"{len(images)} empty shot(s) created — write a prompt for each one, describing "
        "what moves and how the camera moves"))
    return len(images)


def shots_of(project_id: str) -> list:
    return db.query("SELECT * FROM shots WHERE project_id=? ORDER BY idx", (project_id,))


def set_scenes(project, scenes: list[dict]) -> None:
    """Write prompts by scene number (1 = the first picture).

    A scene that has a picture but no shot row yet is created on the way, so an
    assistant can write a storyboard directly without planning it first.
    """
    project_id = project["id"]
    if project["mode"] != "storyboard":
        raise HTTPException(400, "scenes belong to storyboard projects; a single clip is "
                                 "made from the story alone")
    if is_rendering(project):
        raise HTTPException(409, "this project is rendering; wait or cancel it first")
    images = images_of(project_id)
    if not images:
        raise HTTPException(400, "add at least one picture first — each picture is a scene")
    shots = shots_of(project_id)
    have = {s["asset_id"] for s in shots}
    params = db.params_of(project)
    next_idx = max((s["idx"] for s in shots), default=-1) + 1
    for image in images:
        if image["id"] not in have:
            db.run("INSERT INTO shots (id,project_id,idx,title,prompt,seconds,asset_id) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (db.new_id("sht"), project_id, next_idx, f"Shot {next_idx + 1}", "",
                    clamp_seconds("storyboard", params.get("seconds", cfg.DEFAULT_SHOT_SECONDS)), image["id"]))
            next_idx += 1
    shots = shots_of(project_id)
    for entry in scenes:
        try:
            number = int(entry.get("scene"))
        except (TypeError, ValueError):
            raise HTTPException(400, "every entry needs a 'scene' number")
        if not 1 <= number <= len(shots):
            raise HTTPException(400, f"there is no scene {number}; this project has "
                                     f"{len(shots)} (one per picture)")
        shot = shots[number - 1]
        prompt = entry.get("prompt")
        title = entry.get("title")
        seconds = entry.get("seconds")
        db.run("UPDATE shots SET prompt=?, title=?, seconds=? WHERE id=?",
               (str(prompt) if prompt is not None else shot["prompt"],
                str(title)[:80] if title is not None else shot["title"],
                clamp_seconds("storyboard", seconds) if seconds is not None else shot["seconds"],
                shot["id"]))
    db.touch_project(project_id, message="shots saved")


async def start_render(project, user: auth.User, only: list[int] | None = None) -> int:
    """Queue a render and return the queue depth.

    `only` is a list of scene numbers to regenerate in a storyboard; every
    other scene keeps its existing clip and is joined in again.
    """
    project_id = project["id"]
    if is_rendering(project):
        raise HTTPException(409, "already rendering")
    if rate_limited(user.id):
        raise HTTPException(429, f"you have started {cfg.USER_RENDERS_PER_HOUR} renders in "
                                 "the last hour, which is the limit for this account")
    only_ids: set[str] | None = None
    if project["mode"] == "storyboard":
        shots = shots_of(project_id)
        if not shots:
            raise HTTPException(400, "plan the shots first")
        blank = [i + 1 for i, s in enumerate(shots) if not (s["prompt"] or "").strip()]
        if blank:
            raise HTTPException(
                400, f"shot(s) {', '.join(map(str, blank))} have no prompt. Write what "
                     "moves and how the camera moves in each, then Save shots.")
        # A shot saved before the cap existed (or with a raised one) would fail the
        # whole render at WIS after the earlier shots had already used the GPU.
        long = [f"{i + 1} ({s['title'] or 'untitled'}, {s['seconds']} s)"
                for i, s in enumerate(shots)
                if (only is None or i + 1 in only) and int(s["seconds"] or 0) > cfg.STORYBOARD_MAX_SECONDS]
        if long:
            raise HTTPException(
                400, f"scene(s) {', '.join(long)} are longer than the {cfg.STORYBOARD_MAX_SECONDS} s "
                     "a storyboard scene can be. Shorten them, or split a long take across two "
                     "pictures, then render again.")
        if not render.ffmpeg_available() and len(shots) > 1:
            raise HTTPException(503, "ffmpeg is missing, so clips cannot be joined")
        if only:
            bad = [n for n in only if not 1 <= n <= len(shots)]
            if bad:
                raise HTTPException(400, f"there is no scene {bad[0]}; this project has "
                                         f"{len(shots)}")
            only_ids = {shots[n - 1]["id"] for n in only}
            # A kept scene with no clip on disk would silently become a full
            # re-render of that scene too -- minutes of GPU nobody asked for.
            missing = [i + 1 for i, s in enumerate(shots)
                       if s["id"] not in only_ids
                       and not (s["clip_path"] and Path(s["clip_path"]).exists())]
            if missing:
                raise HTTPException(
                    400, f"scene(s) {', '.join(map(str, missing))} have no finished clip to "
                         "keep. Render the whole video once before regenerating single scenes.")
            marks = ",".join("?" * len(only_ids))
            db.run("UPDATE shots SET status='pending', clip_path=NULL, error=NULL, "
                   f"wis_job_id=NULL WHERE id IN ({marks})", tuple(only_ids))
        else:
            db.run("UPDATE shots SET status='pending', clip_path=NULL, error=NULL, "
                   "wis_job_id=NULL WHERE project_id=?", (project_id,))
    else:
        if only:
            raise HTTPException(400, "a single clip has no scenes to pick from")
        if not (project["story"] or "").strip():
            raise HTTPException(400, "write the story first — it is the prompt")
        if not images_of(project_id):
            raise HTTPException(400, "upload at least one reference image")
    db.run("INSERT INTO renders (id,user_id,project_id,created_at) VALUES (?,?,?,?)",
           (db.new_id("rnd"), user.id, project_id, db.now()))
    return await render.enqueue(project_id, only_ids)


# --------------------------------------------------------------------------- signed links

def _sig(kind: str, project_id: str, exp: int) -> str:
    msg = f"{kind}:{project_id}:{exp}".encode()
    return hmac.new(db.secret().encode(), msg, hashlib.sha256).hexdigest()[:40]


def stable_signed_path(kind: str, row_id: str, ttl_seconds: int = 86400) -> str:
    """A signed link whose expiry only moves once an hour, so pages that re-read state
    every few seconds get the same URL (no needless redraws, and browsers can cache it).
    Valid for between ttl and ttl + 1 h."""
    exp = (int(db.now()) // 3600 + 1) * 3600 + ttl_seconds
    tail = {"video": "/video.mp4", "image": "/image.png", "speech": "/speech.mp3",
            "music": "/song.mp3", "voice": "/audio.mp3"}.get(kind, "")
    prefix = {"video": "v", "image": "i", "speech": "s", "music": "m", "voice": "a"}.get(kind, "u")
    return f"/{prefix}/{row_id}/{exp}/{_sig(kind, row_id, exp)}{tail}"


def signed_path(kind: str, project_id: str, ttl_seconds: int) -> str:
    """A path that works without a cookie until it expires.

    An assistant can hand a person a link or run curl, but it cannot carry the
    browser's session -- so the finished video and the upload slot are reachable
    through a link that names one project, does one thing and stops working.
    """
    exp = int(db.now()) + ttl_seconds
    tail = {"video": "/video.mp4", "image": "/image.png", "speech": "/speech.mp3",
            "music": "/song.mp3", "voice": "/audio.mp3"}.get(kind, "")
    prefix = {"video": "v", "image": "i", "speech": "s", "music": "m", "voice": "a"}.get(kind, "u")
    return f"/{prefix}/{project_id}/{exp}/{_sig(kind, project_id, exp)}{tail}"


def signed_ok(kind: str, row_id: str, exp: int, sig: str) -> bool:
    return hmac.compare_digest(_sig(kind, row_id, exp), sig) and exp >= db.now()


def check_signed(kind: str, project_id: str, exp: int, sig: str):
    """The project a signed link points at, or a 404 that explains nothing."""
    good = hmac.compare_digest(_sig(kind, project_id, exp), sig)
    row = db.one("SELECT * FROM projects WHERE id=?", (project_id,)) if good else None
    if row is None or exp < db.now():
        raise HTTPException(404, "this link is not valid, or it has expired")
    return row


# --------------------------------------------------------------------------- retention
# Everything a person makes or uploads is deleted RETENTION_DAYS after it last changed
# (main._reap_loop does it hourly). Trained voices and their profiles are the one
# exception: they are kept until the owner deletes them. Both front doors show the
# date from here, so the web pages and the MCP tools can never disagree.

def keep_a_copy_note() -> str:
    return (f"Files are deleted {cfg.RETENTION_DAYS} days after they were last changed. "
            "Download anything you want to keep and save your own copy. Trained voices and "
            "their voice profiles are kept until you delete them.")


def retention(updated_at: float | None) -> dict:
    """When this item will be deleted, for display: {'deleted_on', 'days_left'}."""
    import datetime as _dt
    at = (updated_at or db.now()) + cfg.RETENTION_DAYS * 86400
    return {"deleted_on": _dt.datetime.fromtimestamp(at, _dt.timezone.utc).strftime("%Y-%m-%d"),
            "days_left": max(0, int((at - db.now()) // 86400))}
