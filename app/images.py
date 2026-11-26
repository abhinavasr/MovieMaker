"""Pictures made on the GPU: text-to-image, and image-to-image from source pictures.

The model is FLUX.2 klein through WIS `/v1/images`. Image-to-image there is
NOT a denoise-from-the-input pass: source pictures go in as `reference_images`
and condition the model at full weight, while the output is drawn fresh at the
requested size. So "make the sky purple" on a photo works as an edit, and two
sources can be combined ("this person in that room") -- but the output only
keeps the source's framing if it is drawn at the source's aspect, which is why
an edit with no size given is drawn at the first source's size (verified: an
edit of a fox at dawn kept the fox, its pose and the framing).

A picture belongs to a person, not a project. It is made first and then put
into as many projects as wanted with `add_file(image_id=...)`.

The GPU work is asynchronous at WIS, and nothing here runs in the background:
every read of a picture that is still generating asks WIS once and records the
answer (`refresh`). That keeps a restart from orphaning anything -- the next
read picks the job up where it is.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
from pathlib import Path

from fastapi import HTTPException
from PIL import Image

from app import auth, db, service, wis
from app.config import cfg

TERMINAL = {"succeeded", "failed", "cancelled"}
# klein chains reference latents with no fixed ceiling, but each one is a full
# copy in the text/latent context; four is what WIS's own contract allows.
MAX_SOURCES = 4
# Picture models a caller may choose (added 2026-11-22). The default stays
# cfg.IMAGE_MODEL (FLUX.2 klein). Qwen-Image-2.1 is one checkpoint that both draws
# and edits; WIS measured ~10 s (turbo) and ~15-46 s (base) per picture.
MODELS = {
    "flux-2-klein-9b": "FLUX.2 klein — the default; fast, strong photographic look",
    "qwen-image-2.1-turbo": "Qwen-Image-2.1 turbo — ~10 s; strong lettering and editing; "
                            "no negative prompt",
    "qwen-image-2.1": "Qwen-Image-2.1 base — slower (25 steps), the most careful Qwen result",
    # Added 2026-11-24 so the person can switch when one model draws a view wrong.
    "qwen-image-edit-2511": "Qwen-Image-Edit 2511 — made for edits; keeps a person's identity "
                            "well; up to 3 source pictures",
    # Not flux-2-dev: with source pictures it ran out of memory on this 24 GB card every
    # time (2026-11-24, twice, including on a clear card).
}
# Models that take fewer source pictures than MAX_SOURCES (WIS refuses more).
MODEL_MAX_SOURCES = {"qwen-image-edit-2511": 3}
MAX_PROMPT = 4000
# Close to klein's ~1 MP native buckets. WIS diffuses at its own bucket and
# upscales to the exact size asked, so a bigger number here buys upscaler
# pixels, not model detail.
SIZES = {"square": (1024, 1024), "portrait": (864, 1536), "landscape": (1536, 864)}
MATCH_PIXELS = 1024 * 1024
MIN_SIDE, MAX_SIDE = 256, 2048

_locks: dict[str, asyncio.Lock] = {}


# --------------------------------------------------------------------------- reads

def owned_image(user: auth.User, image_id: str):
    """Same rule as projects: unknown and someone else's are the same 404."""
    row = db.one("SELECT * FROM images WHERE id=?", (str(image_id or ""),))
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "no such image")
    return row


def rate_limited(user_id: str) -> bool:
    if auth.is_unlimited(user_id):
        return False
    row = db.one("SELECT COUNT(*) AS n FROM images WHERE user_id=? AND created_at > ?",
                 (user_id, db.now() - 3600))
    return bool(row and row["n"] >= cfg.USER_IMAGES_PER_HOUR)


def source_from_image(user: auth.User, image_id: str) -> tuple[str, bytes]:
    row = owned_image(user, image_id)
    if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(400, f"image {row['id']} is not finished ({row['status']}), so it "
                                 "cannot be used as a source yet")
    return f"{row['id']}.png", Path(row["path"]).read_bytes()


def source_from_picture(user: auth.User, project_id: str, number) -> tuple[str, bytes]:
    project = service.owned_project(user, project_id)
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise HTTPException(400, "pictures must be picture numbers, e.g. [1, 3]")
    images = service.images_of(project["id"])
    if not 1 <= number <= len(images):
        raise HTTPException(400, f"there is no picture {number} in that project; it has "
                                 f"{len(images)}")
    asset = images[number - 1]
    return asset["orig_name"] or f"picture-{number}.png", Path(asset["path"]).read_bytes()


# --------------------------------------------------------------------------- sizing

def _round16(v: float) -> int:
    return max(MIN_SIDE, min(MAX_SIDE, int(round(v / 16.0)) * 16))


def choose_size(orientation: str | None, width, height,
                first_source: tuple[int, int] | None) -> tuple[int, int]:
    if width is not None or height is not None:
        if width is None or height is None:
            raise HTTPException(400, "give both width and height, or neither")
        try:
            return _round16(float(width)), _round16(float(height))
        except (TypeError, ValueError):
            raise HTTPException(400, "width and height must be numbers")
    if orientation is not None:
        if orientation not in SIZES:
            raise HTTPException(400, "orientation must be square, portrait or landscape")
        return SIZES[orientation]
    if first_source:
        sw, sh = first_source
        # An edit comes back the size it went in, when that size is a sensible
        # one to draw at; a thumbnail is drawn at ~1 MP in the same shape.
        if sw * sh >= 0.7 * MATCH_PIXELS:
            return _round16(sw), _round16(sh)
        aspect = sw / sh
        return _round16(math.sqrt(MATCH_PIXELS * aspect)), _round16(math.sqrt(MATCH_PIXELS / aspect))
    return SIZES["square"]


def _normalise(name: str, raw: bytes) -> tuple[bytes, tuple[int, int]]:
    """Any readable picture -> an RGB JPEG of at most 1536 px, and its size.

    JPEG at q95 rather than PNG because four 1536 px PNGs can pass WIS's 32 MB
    body limit once base64'd, and a reference is conditioning, not output.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            img.thumbnail((1536, 1536))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=95)
            return out.getvalue(), img.size
    except Exception:                                          # noqa: BLE001
        raise HTTPException(400, f"{name or 'a source'} is not a readable image")


# --------------------------------------------------------------------------- writes

async def create(user: auth.User, prompt: str, sources: list[tuple[str, bytes]], *,
                 orientation: str | None = None, width=None, height=None,
                 seed=None, model: str | None = None, negative: str | None = None) -> str:
    """Submit one picture. `sources` empty = text-to-image; otherwise image-to-image.
    `negative`: what must not appear (models that ignore it say so in WIS's notes)."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "a prompt is required — describe the picture, or for an "
                                 "edit, what to change")
    if len(prompt) > MAX_PROMPT:
        raise HTTPException(400, f"the prompt is over {MAX_PROMPT} characters")
    model = (model or "").strip() or cfg.IMAGE_MODEL
    if model not in MODELS and model != cfg.IMAGE_MODEL:
        raise HTTPException(400, f"model must be one of {', '.join(MODELS)}")
    if len(sources) > MAX_SOURCES:
        raise HTTPException(400, f"at most {MAX_SOURCES} source pictures per image")
    # The first sources are the main ones (callers order them so); a model that takes
    # fewer keeps those.
    sources = sources[:MODEL_MAX_SOURCES.get(model, MAX_SOURCES)]
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise HTTPException(400, "seed must be a whole number")
        if not 0 <= seed < 2 ** 53:
            raise HTTPException(400, "seed must be between 0 and 2^53-1")
    if rate_limited(user.id):
        raise HTTPException(429, f"you have made {cfg.USER_IMAGES_PER_HOUR} images in the last "
                                 "hour, which is the limit for this account")

    refs, first = [], None
    for i, (name, raw) in enumerate(sources):
        data, size = _normalise(name, raw)
        first = first or size
        refs.append({"b64": wis.b64(data), "mime": "image/jpeg",
                     "role": f"source picture {i + 1}"})
    w, h = choose_size(orientation, width, height, first)

    image_id = db.new_id("img")
    # Field names exactly as spec section 3 spells them -- an unknown key is
    # accepted by WIS and does nothing, which is how a reference goes missing.
    # No `layer_role`: a role applies the pack reference policy and withholds
    # references on three of the four roles. Without one, they are kept.
    payload = {"model": model, "prompt": prompt, "width": w, "height": h,
               "background": "opaque", "format": "png", "client_request_id": image_id}
    if refs:
        payload["reference_images"] = refs
        payload["use_references"] = True
    if seed is not None:
        payload["seed"] = seed
    if negative:
        payload["negative_prompt"] = negative[:500]
    try:
        job = await wis.submit_image(payload)
    except wis.WisError as exc:
        if exc.code == "rate_limited":
            raise HTTPException(429, "the studio's hourly allowance on the GPU service is used "
                                     "up (it is shared by everyone's pictures and videos); "
                                     "try again later")
        raise HTTPException(502, f"the GPU service refused the image: {exc}")
    db.run("INSERT INTO images (id,user_id,prompt,sources,status,progress,notes,wis_job_id,"
           "width,height,created_at,updated_at,model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (image_id, user.id, prompt, len(refs), job.status, job.progress,
            json.dumps(job.notes) if job.notes else None, job.id, w, h, db.now(), db.now(),
            model))
    return image_id


async def refresh(image_id: str):
    """Ask WIS once about a picture that is not finished, and record the answer."""
    row = db.one("SELECT * FROM images WHERE id=?", (image_id,))
    if row is None or row["status"] in TERMINAL or not row["wis_job_id"]:
        return row
    lock = _locks.setdefault(image_id, asyncio.Lock())
    async with lock:
        row = db.one("SELECT * FROM images WHERE id=?", (image_id,))
        if row["status"] in TERMINAL:
            return row
        try:
            job = await wis.get_job(row["wis_job_id"], route="images")
        except wis.WisError as exc:
            if exc.code == "not_found":
                db.run("UPDATE images SET status='failed', error=?, updated_at=? WHERE id=?",
                       ("the GPU service no longer has this job", db.now(), image_id))
            return db.one("SELECT * FROM images WHERE id=?", (image_id,))

        notes = list(job.notes)
        applied = job.raw.get("references_applied")
        # Counted, not read from `message`: WIS states it as a field so that a
        # dropped source is a number that can be checked, not prose.
        if row["sources"] and applied is not None and int(applied) < row["sources"]:
            notes.append(f"only {applied} of {row['sources']} source picture(s) were used")
        fields = {"status": job.status, "progress": job.progress,
                  "notes": json.dumps(notes) if notes else None,
                  "references_applied": applied}
        if job.raw.get("seed") is not None:
            fields["seed"] = int(job.raw["seed"])
        if job.status == "succeeded":
            data = await wis.download_artifact(job)
            dest = cfg.images_dir / row["user_id"]
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / f"{image_id}.png"
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            art = job.artifacts[0]
            fields.update(path=str(path), width=art.get("width"), height=art.get("height"))
        elif job.status in ("failed", "cancelled"):
            err = job.raw.get("error") or {}
            fields["error"] = err.get("message") or job.message or job.status
        sets = ", ".join(f"{k}=?" for k in fields)
        db.run(f"UPDATE images SET {sets}, updated_at=? WHERE id=?",
               (*fields.values(), db.now(), image_id))
    _locks.pop(image_id, None)
    return db.one("SELECT * FROM images WHERE id=?", (image_id,))


async def wait(image_id: str, seconds: float, poll: float = 2.0):
    """Refresh until finished or `seconds` pass. The caller's own deadline wins."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        row = await refresh(image_id)
        if row["status"] in TERMINAL or loop.time() + poll > deadline:
            return row
        await asyncio.sleep(poll)


def preview_jpeg(path: str, longest: int = 640) -> bytes:
    """A small copy for an assistant to look at; the full PNG goes by link."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((longest, longest))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()


def add_to_project(user: auth.User, image_id: str, project) -> int:
    """Copy a finished picture into a project, through the same door as an upload."""
    row = owned_image(user, image_id)
    if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(400, f"image {row['id']} is not finished ({row['status']}); call "
                                 "get_image until it has succeeded")
    return service.add_files(project, [(f"{row['id']}.png", "image/png",
                                        Path(row["path"]).read_bytes())])


def delete_for_user(user_id: str) -> None:
    for row in db.query("SELECT path FROM images WHERE user_id=?", (user_id,)):
        if row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM images WHERE user_id=?", (user_id,))


def purge_older_than(cutoff: float) -> None:
    for row in db.query("SELECT id, path FROM images WHERE updated_at < ?", (cutoff,)):
        if row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
        db.run("DELETE FROM images WHERE id=?", (row["id"],))
