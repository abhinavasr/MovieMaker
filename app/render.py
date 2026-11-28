"""The render queue.

One worker, one project at a time. That is not a simplification -- there is a
single 24 GB card behind WIS and a clip occupies it for roughly two minutes, so
a second concurrent project would not render faster, it would just interleave
two people's waits. Queue depth is visible in the UI instead.

Storyboard mode renders N clips and joins them with ffmpeg. Single-clip mode
sends one reference-conditioned request and keeps what comes back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

from app import db, storyboard, wis
from app.config import cfg

log = logging.getLogger("studio.render")

_queue: asyncio.Queue[str] = asyncio.Queue()
_cancelled: set[str] = set()
_current: dict = {"project_id": None, "wis_job_id": None}
# Set while the app shuts down, so a render cut off by the restart is told apart
# from one the person cancelled (both arrive as CancelledError).
_stopping = False
# project id -> the shot ids to regenerate, for a render that keeps the rest.
_only: dict[str, set[str]] = {}


def queue_depth() -> int:
    return _queue.qsize()


def current_project() -> str | None:
    return _current["project_id"]


async def enqueue(project_id: str, only: set[str] | None = None, partial: bool = False) -> int:
    """Queue a render. `only` limits a storyboard to those shots; the others
    keep the clip they already have and are simply joined in again."""
    db.touch_project(project_id, status="queued", progress=0,
                     message="waiting for the GPU", error=None)
    # Persisted so a restart can pick the render up exactly (see recover()).
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if project is not None:
        params = db.params_of(project)
        params["_render_only"] = sorted(only) if only else None
        # Editor productions render shots ahead of time: the others may have no
        # clip yet, and the film is joined only once every shot has one.
        params["_partial"] = bool(partial)
        params["_wis_job"] = None
        params.pop("_render_total", None)          # a new render counts from zero
        params.pop("_render_failed", None)
        params.pop("_cancel", None)
        db.set_params(project_id, params)
    _cancelled.discard(project_id)
    if only:
        _only[project_id] = set(only)
    else:
        _only.pop(project_id, None)
    await _queue.put(project_id)
    return _queue.qsize()


async def _attach(job_id: str | None):
    """A WIS job this render already submitted before a restart, if still usable."""
    if not job_id:
        return None
    try:
        job = await wis.get_job(job_id)
    except Exception:                                          # noqa: BLE001
        return None
    return job if job.status in ("queued", "running", "succeeded") else None


def _remember_job(project_id: str, job_id: str | None) -> None:
    row = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if row is not None:
        params = db.params_of(row)
        params["_wis_job"] = job_id
        db.set_params(project_id, params)


def recover() -> int:
    """Put renders that a restart interrupted back in the queue (added 2026-11-21).

    The queue lives in memory, so before this a web restart stranded every queued
    and running project at 'queued'/'running' forever. The interrupted one goes
    first and REATTACHES to the WIS job it already submitted (single clip:
    params._wis_job; storyboard: the shot's wis_job_id) rather than paying for the
    clip again; a scene subset is restored from params._render_only.
    """
    rows = db.query("SELECT * FROM projects WHERE status IN ('running','queued') "
                    "ORDER BY status='running' DESC, updated_at")
    picked = 0
    for row in rows:
        if db.params_of(row).get("_cancel"):
            # Cancelled while running and the restart came before the loop noticed:
            # honour it rather than paying for the clip again.
            db.touch_project(row["id"], status="cancelled", message="cancelled by the user")
            continue
        picked += 1
        only = db.params_of(row).get("_render_only")
        if only:
            # Shots this render already finished before the restart keep their clip:
            # enqueue cleared every listed shot, so a listed shot that is now done with
            # its clip on disk was made by this very render (2026-11-27: a reboot made
            # a finished 41 s InfiniteTalk shot -- 16 GPU minutes -- again).
            finished = {s["id"] for s in db.query(
                "SELECT id, clip_path FROM shots WHERE project_id=? AND status='done'",
                (row["id"],)) if s["clip_path"] and Path(s["clip_path"]).exists()}
            left = [sid for sid in only if sid not in finished]
            if finished & set(only):
                params = db.params_of(row)
                params["_render_only"] = left
                db.set_params(row["id"], params)
            _only[row["id"]] = set(left)
        db.touch_project(row["id"], status="queued",
                         message="picked up again after a restart")
        _queue.put_nowait(row["id"])
    return picked


def stop() -> None:
    """Called at shutdown, before the worker task is cancelled."""
    global _stopping
    _stopping = True


def request_cancel(project_id: str) -> bool:
    """Cancel a render. True if it is running and stops at its next checkpoint;
    False if it had not started and is now cancelled.

    Recorded in the database as well as in memory: before 2026-11-22 a cancel lived
    only in `_cancelled`, so a restart re-queued every cancelled render. If the
    project is the one running, its in-flight WIS job is cancelled too -- a clip is
    the most expensive thing this box produces.
    """
    _cancelled.add(project_id)
    if _current["project_id"] != project_id:
        db.touch_project(project_id, status="cancelled", message="cancelled before it started")
        return False
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if project is not None:
        params = db.params_of(project)
        params["_cancel"] = True
        db.set_params(project_id, params)
    job_id = _current["wis_job_id"]
    ids = {job_id} if job_id else set()
    # A render picked up again after a restart may not know its in-flight job: the shots'
    # recorded WIS jobs are cancelled too (2026-11-27: a deleted production's lip-sync shot
    # kept the GPU after its render was cancelled).
    ids |= {r["wis_job_id"] for r in db.query(
        "SELECT wis_job_id FROM shots WHERE project_id=? AND wis_job_id IS NOT NULL AND "
        "status NOT IN ('done','failed','cancelled')", (project_id,))}
    for j in ids:
        asyncio.create_task(wis.cancel_job(j))
    task = _polish_tasks.get(project_id)
    if task is not None and not task.done():
        task.cancel()                       # its finally returns the GPU lease
    return True


_polish_tasks: dict[str, asyncio.Task] = {}


class _SomeFailed(Exception):
    """The render finished, but these shots failed: [(title, why)]."""

    def __init__(self, failed: list[tuple[str, str]], made: int):
        super().__init__("some shots failed")
        self.failed, self.made = failed, made


class _Turn(Exception):
    """A production steps back so a waiting one gets the GPU for a shot (2026-11-27, the
    person: "have the render worker take turns between productions, one shot each, instead
    of finishing one production first" -- a 3-hour podcast render held up everyone's
    20-second clip). Carries the shots this render still has to make."""

    def __init__(self, remaining: list[str], done: int, total: int):
        super().__init__("taking turns")
        self.remaining, self.done, self.total = remaining, done, total


def _check_cancelled(project_id: str) -> None:
    if project_id in _cancelled:
        raise asyncio.CancelledError()


# --------------------------------------------------------------------------- ffmpeg

def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError("ffmpeg failed: " + " / ".join(tail))


def _has_audio(path: Path) -> bool:
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(path)], capture_output=True)
    return bool(res.stdout.strip())


def _concat_with_sound(clips: list[Path], out: Path, size: tuple[int, int] | None) -> None:
    w, h = size or (480, 864)
    cmd = ["ffmpeg", "-y"]
    parts = []
    for i, c in enumerate(clips):
        cmd += ["-i", str(c)]
    for i, c in enumerate(clips):
        parts.append(f"[{i}:v]scale={w}:{h}:flags=lanczos,setsar=1,fps=24[v{i}]")
        if _has_audio(c):
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration="
                         f"{_duration(c):.3f}[a{i}]")
    parts.append("".join(f"[v{i}][a{i}]" for i in range(len(clips)))
                 + f"concat=n={len(clips)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    _run(cmd)


def _duration(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 5.0


def _concat(clips: list[Path], out: Path, audio: Path | None,
            size: tuple[int, int] | None = None, keep_sound: bool = False) -> None:
    """Join clips, optionally laying an uploaded soundtrack over the result.

    Re-encoded rather than stream-copied. The clips all come from one model at
    one size, so `-c copy` would usually work -- but "usually" here means a
    silent container-level failure at the very last step of a render that has
    already cost the user ten minutes, and the re-encode of a handful of short
    clips costs seconds.
    """
    listing = out.parent / "concat.txt"
    listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
    if audio is None and keep_sound and any(_has_audio(c) for c in clips):
        # Talking, singing and remade scenes carry their sound (2026-11-24): join with it,
        # giving silent clips a silent track so every piece has one.
        _concat_with_sound(clips, out, size)
        return
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
    if audio is None:
        # Performance shots (ref2v) carry sound and animated ones don't; mixed
        # streams break the concat. Their sound is mixed by the editor's export.
        cmd += ["-map", "0:v:0"]
    if audio is not None:
        # -shortest: the narration is trimmed to the picture rather than
        # extending it with a frozen final frame.
        cmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    if size:
        # Kept scenes can be a different size from re-rendered ones when upscale was
        # switched between renders; the concat demuxer would otherwise mix sizes.
        cmd += ["-vf", f"scale={size[0]}:{size[1]}:flags=lanczos,setsar=1"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    _run(cmd)
    listing.unlink(missing_ok=True)


# --------------------------------------------------------------------------- modes

def _shape_off(path, params: dict) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as im:
            iw, ih = im.size
    except Exception:                                          # noqa: BLE001
        return False
    want = int(params.get("width", 480)) / int(params.get("height", 864))
    return abs(iw / ih - want) / want >= 0.03


async def match_shape(project_id: str, shot: dict, image_path: str, params: dict, report) -> str:
    """A picture of another shape than the film is REDRAWN in the film's shape before its clip
    is made -- the same picture recomposed, not cropped or stretched (2026-11-27, the person:
    "regenerate if dimensions don't match"; Khaing's landscape fox pictures had been squeezed
    into a portrait film). The new picture replaces the shot's. If redrawing fails, the clip
    falls back to a centre crop (fitted)."""
    if not image_path or not _shape_off(image_path, params):
        return image_path
    from app import agent, images, shotpics
    owner = db.one("SELECT user_id FROM projects WHERE id=?", (project_id,))
    user = agent.load_user(owner["user_id"]) if owner else None
    if user is None:
        return image_path
    w, h = int(params.get("width", 480)), int(params.get("height", 864))
    shape = "landscape" if w > h * 1.15 else "portrait" if h > w * 1.15 else "square"
    label = shot.get("title") or f"shot {shot['idx'] + 1}"
    await report(1, f"{label} — redrawing its picture as a {shape} frame to match the film")
    try:
        img = await images.create(
            user, "Recompose this exact picture as a " + shape + " frame: the same characters, "
            "place, moment, art style, colours and light; extend the scene naturally to fill the "
            "new frame; nothing new added, no text, no borders.",
            [("picture.png", Path(image_path).read_bytes())], orientation=shape,
            model=shotpics.IDENTITY_MODEL)
        for _ in range(180):
            _check_cancelled(project_id)
            row = await images.refresh(img) or db.one("SELECT * FROM images WHERE id=?", (img,))
            if row["status"] in images.TERMINAL:
                break
            await asyncio.sleep(3)
        row = db.one("SELECT * FROM images WHERE id=?", (img,))
        if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
            raise RuntimeError(row["status"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:                                   # noqa: BLE001
        log.warning("redrawing %s in the film's shape failed (%s); cropping instead",
                    shot["id"], exc)
        return image_path
    dest = cfg.uploads_dir / project_id
    dest.mkdir(parents=True, exist_ok=True)
    asset_id = db.new_id("ast")
    path = dest / f"{asset_id}.png"
    path.write_bytes(Path(row["path"]).read_bytes())
    idx = (db.one("SELECT MAX(idx) AS m FROM assets WHERE project_id=?", (project_id,))["m"] or 0) + 1
    db.run("INSERT INTO assets (id, project_id, kind, idx, path, orig_name, mime, created_at) "
           "VALUES (?,?,?,?,?,?,?,?)", (asset_id, project_id, "image", idx, str(path),
                                        f"{shape}-{img}.png", "image/png", db.now()))
    db.run("UPDATE shots SET asset_id=? WHERE id=?", (asset_id, shot["id"]))
    shot["asset_id"] = asset_id
    log.info("%s: picture redrawn as %s (%s) to match the film", shot["id"], shape, img)
    return str(path)


def fitted(path, params: dict) -> bytes:
    """The picture as the video model should get it: when its shape differs from the video's,
    centre-cropped to the video's shape (a video model resizes to width x height, so a
    landscape picture in a portrait video came out squeezed -- 2026-11-27, Khaing's fox film).
    Same-shaped pictures pass through untouched."""
    raw = Path(path).read_bytes()
    w, h = int(params.get("width", 480)), int(params.get("height", 864))
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(raw)) as im:
            iw, ih = im.size
            want = w / h
            if abs(iw / ih - want) / want < 0.03:
                return raw
            if iw / ih > want:                       # too wide: trim the sides
                cw = round(ih * want)
                box = ((iw - cw) // 2, 0, (iw - cw) // 2 + cw, ih)
            else:                                    # too tall: trim top and bottom
                ch = round(iw / want)
                box = (0, (ih - ch) // 2, iw, (ih - ch) // 2 + ch)
            buf = io.BytesIO()
            im.convert("RGB").crop(box).save(buf, "PNG")
            log.info("picture %s is %dx%d for a %dx%d video: cropped, not stretched",
                     Path(path).name, iw, ih, w, h)
            return buf.getvalue()
    except Exception:                                          # noqa: BLE001
        return raw


async def _render_shot(project_id: str, shot, assets: dict, params: dict,
                       report) -> Path:
    """One clip. Returns the file it was written to."""
    if shot.get("make") == "graphic":
        # An animated chart, drawn by code on the CPU (app/charts.py): no WIS, no GPU.
        from app import charts
        chart = json.loads(shot.get("opts") or "{}").get("chart") if isinstance(shot.get("opts"), str) \
            else (shot.get("opts") or {}).get("chart")
        if not chart or not charts.valid(chart):
            raise RuntimeError("this chart shot has no valid chart data")
        await report(10, "drawing the animated chart")
        out = cfg.outputs_dir / project_id / f"shot_{shot['id']}.mp4"
        await asyncio.to_thread(charts.render, chart, int(params.get("width", 480)),
                                int(params.get("height", 864)), float(shot.get("seconds") or 6), out)
        db.run("UPDATE shots SET status='done', clip_path=? WHERE id=?", (str(out), shot["id"]))
        return out
    image_path = assets.get(shot["asset_id"])
    image_path = await match_shape(project_id, shot, image_path, params, report)
    if shot.get("make") in ("perform", "lipsync", "motion", "talk2", "longtalk", "recast", "restyle"):
        from app import editor
        payload = editor.perform_payload(project_id, shot, image_path, params)
        return await _submit_shot(project_id, shot, payload, report)
    from app import promptsmith
    payload = {
        "model": cfg.STORYBOARD_MODEL,
        "prompt": promptsmith.current(project_id, shot).get("prompt") or shot["prompt"],
        "seconds": int(shot["seconds"] or cfg.DEFAULT_SHOT_SECONDS),
        "width": int(params.get("width", 480)),
        "height": int(params.get("height", 864)),
        "client_request_id": shot["id"],
    }
    if params.get("upscale") in ("latent", "refine"):
        payload["upscale"] = params["upscale"]
    if image_path:
        payload["image"] = {"b64": wis.b64(fitted(image_path, params)),
                            "mime": "image/png"}
    # Continuity: this shot ends on the next shot's opening image, so the two
    # clips meet on the same frame instead of jumping. Only meaningful when
    # there IS a next image, and only when the person asked for it -- it
    # constrains the motion, which is not always what they want.
    if params.get("continuity") and shot.get("next_asset_id"):
        nxt = assets.get(shot["next_asset_id"])
        if nxt and image_path:
            payload["last_image"] = {"b64": wis.b64(fitted(nxt, params)),
                                     "mime": "image/png"}
    return await _submit_shot(project_id, shot, payload, report)


async def _submit_shot(project_id: str, shot, payload: dict, report) -> Path:
    job = await _attach(shot.get("wis_job_id")) if shot.get("status") == "running" else None
    if job is None:
        job = await wis.submit_video(payload)
        db.run("UPDATE shots SET wis_job_id=?, status='running', notes=? WHERE id=?",
               (job.id, json.dumps(job.notes), shot["id"]))
    _current["wis_job_id"] = job.id

    async def on_progress(j):
        _check_cancelled(project_id)
        label = shot.get("title") or f"shot {shot['idx'] + 1}"
        await report(j.progress, f"{label} — {j.message or j.status}")

    job = await wis.wait_for(job.id, on_progress=on_progress)
    _current["wis_job_id"] = None
    if job.status != "succeeded":
        label = shot.get("title") or f"shot {shot['idx'] + 1}"
        err = job.raw.get("error") if isinstance(job.raw.get("error"), dict) else {}
        if err.get("code") == "busy":
            # 2026-11-24: the person saw a size hint for a size they had already picked.
            raise RuntimeError(f"{label}: the GPU was too full to make this video — "
                               f"{err.get('message') or 'another model was still loaded'}. "
                               "Press 🎬 again; if it keeps happening, use ⋯ → 🧹 Free the GPU.")
        raise RuntimeError(f"{label}: {err.get('message') or job.message or job.status}")

    data = await wis.download_artifact(job)
    # Named by shot id: editor shots move between positions.
    out = cfg.outputs_dir / project_id / f"shot_{shot['id']}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    db.run("UPDATE shots SET status='done', clip_path=? WHERE id=?", (str(out), shot["id"]))
    return out


async def _render_storyboard(project_id: str, project, report) -> tuple[Path, bool]:
    shots = db.query("SELECT * FROM shots WHERE project_id=? ORDER BY idx", (project_id,))
    if not shots:
        raise RuntimeError("this project has no shots to render")
    rows = db.query("SELECT * FROM assets WHERE project_id=? ORDER BY kind, idx", (project_id,))
    assets = {r["id"]: r["path"] for r in rows}
    audio_assets = [r for r in rows if r["kind"] == "audio"]
    params = db.params_of(project)

    ordered = [dict(s) for s in shots]
    for i, shot in enumerate(ordered):
        shot["next_asset_id"] = ordered[i + 1]["asset_id"] if i + 1 < len(ordered) else None

    only = _only.pop(project_id, None)
    partial = bool(params.get("_partial"))
    # The local 27B writes the prompts of the shots about to be made, in one GPU turn
    # before any video model loads (promptsmith).
    to_make = [s["id"] for s in ordered
               if only is None or s["id"] in only
               or not (s.get("clip_path") and Path(s["clip_path"]).exists())]
    if partial and only is not None:
        to_make = [i for i in to_make if i in only]
    if to_make and cfg.PROMPT_POLISH:
        from app import promptsmith
        # Run as a task Stop can cancel: the prompt pass waits for the GPU and runs the
        # 27B for minutes, and _check_cancelled is only reached between shots (2026-11-27:
        # the person's Stop left it writing 9 prompts on the GPU).
        _check_cancelled(project_id)
        task = asyncio.create_task(promptsmith.polish_shots(project_id, to_make, report))
        _polish_tasks[project_id] = task
        try:
            await task
        finally:
            _polish_tasks.pop(project_id, None)
        _check_cancelled(project_id)
        fresh = {r["id"]: r["opts"] for r in db.query(
            "SELECT id, opts FROM shots WHERE project_id=?", (project_id,))}
        for s in ordered:
            s["opts"] = fresh.get(s["id"], s.get("opts"))
    clips: list[Path] = []
    total = len(ordered)
    missing = 0
    failed: list[tuple[str, str]] = []
    prev_clip: Path | None = None       # the clip of the shot just before this one
    for i, shot in enumerate(ordered):
        _check_cancelled(project_id)
        kept = shot.get("clip_path")
        if only is not None and shot["id"] not in only and kept and Path(kept).exists():
            # Not asked for, and its clip is still on disk: two minutes of GPU
            # that do not need spending again.
            clips.append(Path(kept))
            prev_clip = Path(kept)
            continue
        if partial and only is not None and shot["id"] not in only:
            missing += 1           # rendered later, when someone asks for it
            prev_clip = None
            continue
        if shot.get("start_from") == "prev_last":
            # A continuous shot opens on the previous shot's final frame, taken
            # now, so it follows whatever that shot became in this same render.
            if prev_clip is None:
                raise RuntimeError(f"{shot.get('title') or f'shot {i + 1}'} starts from the "
                                   "previous shot's last frame, but that shot has no clip yet")
            from app import editor
            aid, apath = editor.chain_picture(project_id, shot["id"], prev_clip)
            shot["asset_id"] = aid
            assets[aid] = apath
            if i > 0:
                ordered[i - 1]["next_asset_id"] = aid

        async def report_shot(pct, msg, _i=i):
            # Shot progress mapped into the whole project's bar, so the number
            # on screen means "this render", not "this clip".
            await report(int((_i + pct / 100) / total * 90), f"shot {_i + 1}/{total}: {msg}")

        if shot.get("make") in ("recast", "restyle") and not shot.get("asset_id"):
            # The painted frame is made here when step 1 was skipped (the action comes
            # from the video; the person need not draw anything first).
            from app import editor
            await report_shot(0, "painting the frame the remake starts from")
            aid = await editor.ensure_painted(db.one("SELECT * FROM projects WHERE id=?",
                                                     (project_id,)), shot["id"])
            if aid:
                shot["asset_id"] = aid
                assets[aid] = db.one("SELECT path FROM assets WHERE id=?", (aid,))["path"]
        await report_shot(0, "submitting")
        try:
            clips.append(await _render_shot(project_id, shot, assets, params, report_shot))
        except (asyncio.CancelledError, _Turn):
            raise
        except Exception as exc:                               # noqa: BLE001
            # One shot failing does not end the render (2026-11-27: a bad voice file on shot 3
            # stopped a 23-shot podcast after 2 shots). It is marked failed with its reason,
            # the rest are made, and the end of the render lists what failed; Videos again
            # redoes only those.
            why = f"[{exc.code}] {exc}" if isinstance(exc, wis.WisError) else str(exc)
            db.run("UPDATE shots SET status='failed', error=? WHERE id=?", (why[:600], shot["id"]))
            failed.append((shot.get("title") or f"shot {i + 1}", why))
            # kept across turn-taking, so the end of the render still lists it
            pp = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (project_id,)))
            pp.setdefault("_render_failed", []).append([failed[-1][0], why[:300]])
            db.set_params(project_id, pp)
            log.warning("project %s: %s failed (%s); carrying on", project_id,
                        shot.get("title") or shot["id"], why[:200])
            prev_clip = None
            missing += 1
            continue
        prev_clip = clips[-1]
        _check_cancelled(project_id)       # a Stop during the shot wins over taking turns
        if any(p != project_id for p in list(_queue._queue)):   # another production waits
            # Another production is waiting: step back after this shot. The shots made so
            # far keep their clips; the rest are made when it is this production's turn again.
            later = [s["id"] for s in ordered[i + 1:] if only is None or s["id"] in only]
            if later:
                made = sum(1 for s in ordered[:i + 1] if only is None or s["id"] in only)
                raise _Turn(later, made, made + len(later))

    _check_cancelled(project_id)
    earlier = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (project_id,))).get("_render_failed") or []
    if earlier:
        failed = [tuple(x) for x in earlier]
        raise _SomeFailed(failed, sum(1 for s in db.query(
            "SELECT status FROM shots WHERE project_id=?", (project_id,)) if s["status"] == "done"))
    if partial and missing:
        return None, False
    await report(92, f"joining {len(clips)} clips")
    out = cfg.outputs_dir / project_id / "final.mp4"
    audio = Path(audio_assets[0]["path"]) if audio_assets else None
    k = 2 if params.get("upscale") in ("latent", "refine") else 1
    # Editor productions mix sound on export from the timeline; an MCP storyboard has no
    # timeline, so its talking/singing/remade scenes keep their own sound in the film.
    _concat(clips, out, audio,
            (int(params.get("width", 480)) * k, int(params.get("height", 864)) * k),
            keep_sound=not params.get("editor"))
    return out, audio is not None


async def _render_single(project_id: str, project, report) -> tuple[Path, bool]:
    rows = db.query("SELECT * FROM assets WHERE project_id=? ORDER BY kind, idx", (project_id,))
    images = [r for r in rows if r["kind"] == "image"]
    audios = [r for r in rows if r["kind"] == "audio"]
    params = db.params_of(project)

    prompt = storyboard.autotag(project["story"].strip(), len(images))
    payload = {
        "model": cfg.SINGLE_MODEL,
        "prompt": prompt,
        "seconds": int(params.get("seconds", 10)),
        "width": int(params.get("width", 480)),
        "height": int(params.get("height", 864)),
        "ref_image_size": params.get("ref_image_size", "match"),
        "client_request_id": project_id,
    }
    if params.get("upscale") in ("latent", "refine"):
        payload["upscale"] = params["upscale"]
    if images:
        payload["reference_images"] = [
            {"b64": wis.b64(Path(r["path"]).read_bytes()), "mime": "image/png"}
            for r in images]
    if audios:
        payload["reference_audio"] = [
            {"b64": wis.b64(Path(r["path"]).read_bytes()), "mime": r["mime"] or "audio/wav"}
            for r in audios]

    await report(4, "submitting")
    job = await _attach(params.get("_wis_job"))
    if job is None:
        job = await wis.submit_video(payload)
        _remember_job(project_id, job.id)
    _current["wis_job_id"] = job.id
    if job.notes:
        db.touch_project(project_id, message="; ".join(job.notes)[:400])

    async def on_progress(j):
        _check_cancelled(project_id)
        await report(max(5, min(95, j.progress)), j.message or j.status)

    job = await wis.wait_for(job.id, on_progress=on_progress)
    _current["wis_job_id"] = None
    if job.status != "succeeded":
        raise RuntimeError(job.message or job.status)

    await report(96, "downloading")
    out = cfg.outputs_dir / project_id / "final.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(await wis.download_artifact(job))
    # ref2va decodes a joint audio+video latent, so this one has a real
    # soundtrack whether or not the caller supplied reference audio.
    return out, True


# --------------------------------------------------------------------------- loop

async def _render(project_id: str) -> None:
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if project is None:
        return
    from app import agent as _agent
    _agent.use_planner_of(project["user_id"])         # prompts on the owner's thinking model

    async def report(pct: int, msg: str) -> None:
        db.touch_project(project_id, progress=max(0, min(99, int(pct))), message=msg[:400])

    db.touch_project(project_id, status="running", progress=1, message="starting", error=None)
    try:
        if project["mode"] == "single":
            out, has_audio = await _render_single(project_id, project, report)
        else:
            out, has_audio = await _render_storyboard(project_id, project, report)
        if out is None:
            done = db.one("SELECT COUNT(*) AS n FROM shots WHERE project_id=? AND status='done'",
                          (project_id,))["n"]
            db.touch_project(project_id, status="succeeded", progress=100,
                             message=f"shots rendered ({done} ready)")
        else:
            db.touch_project(project_id, status="succeeded", progress=100,
                             message="done", output_path=str(out),
                             has_audio=1 if has_audio else 0)
        log.info("project %s rendered -> %s", project_id, out)
    except _SomeFailed as sf:
        names = "; ".join(f"{t}: {w[:160]}" for t, w in sf.failed[:6])
        more = f" (+{len(sf.failed) - 6} more)" if len(sf.failed) > 6 else ""
        db.touch_project(project_id, status="failed", progress=100,
                         message=f"{sf.made} shot(s) made, {len(sf.failed)} failed -- press Videos "
                                 "to redo only the failed ones",
                         error=f"{len(sf.failed)} shot(s) failed: {names}{more}")
        log.warning("project %s: %d shot(s) failed, %d made", project_id, len(sf.failed), sf.made)
    except _Turn as turn:
        params = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (project_id,)))
        params["_render_only"] = turn.remaining          # also what recover() resumes
        total = params.setdefault("_render_total", turn.total)   # the whole render, not this turn
        db.set_params(project_id, params)
        _only[project_id] = set(turn.remaining)
        db.touch_project(project_id, status="queued",
                         message=f"taking turns with other renders: {total - len(turn.remaining)} "
                                 f"of {total} shots made, the rest follow")
        _queue.put_nowait(project_id)
        log.info("project %s steps back after %d of %d shots (another render waits)",
                 project_id, total - len(turn.remaining), total)
    except asyncio.CancelledError:
        if _stopping and project_id not in _cancelled:
            # The app is restarting, not the person cancelling. Leave it 'running':
            # recover() picks it up and reattaches to the WIS job, which carries on.
            db.touch_project(project_id, message="interrupted by a restart; resumes when the studio is back")
            log.info("project %s interrupted by shutdown; will resume", project_id)
            raise
        db.touch_project(project_id, status="cancelled", message="cancelled by the user")
        db.run("UPDATE shots SET status='cancelled' WHERE project_id=? AND status='running'",
               (project_id,))
        log.info("project %s cancelled", project_id)
    except wis.WisError as exc:
        # The service's own wording, kept verbatim. Its 400s name the field that
        # is wrong and its `busy` says which sizes to ask for instead;
        # paraphrasing that into "render failed" throws away the only part of
        # the message that tells the person what to change.
        db.touch_project(project_id, status="failed", message="failed",
                         error=f"[{exc.code}] {exc}")
        log.warning("project %s failed: %s", project_id, exc)
    except Exception as exc:                                   # noqa: BLE001
        db.touch_project(project_id, status="failed", message="failed", error=str(exc)[:600])
        log.exception("project %s failed", project_id)
    finally:
        _current["project_id"] = None
        _current["wis_job_id"] = None
        _cancelled.discard(project_id)


async def worker() -> None:
    n = recover()
    log.info("render worker started (%d render(s) picked up again)", n)
    while True:
        project_id = await _queue.get()
        try:
            row = db.one("SELECT status FROM projects WHERE id=?", (project_id,))
            if row is None or row["status"] != "queued":
                # Deleted, or cancelled (persisted) while it waited.
                _cancelled.discard(project_id)
                _only.pop(project_id, None)
                continue
            if project_id in _cancelled:
                db.touch_project(project_id, status="cancelled",
                                 message="cancelled before it started")
                _cancelled.discard(project_id)
                _only.pop(project_id, None)
                continue
            _current["project_id"] = project_id
            await _render(project_id)
        except Exception:                                      # noqa: BLE001
            log.exception("render worker survived an error on %s", project_id)
        finally:
            _queue.task_done()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None
