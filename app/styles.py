"""Singing-style models (layer 3): a LoKr adapter for the song model, trained on full
songs a person sang, so new songs are PERFORMED in their manner before the voice model
gives them their exact sound. See docs/VOICE_API_SPEC.md §6 and §10.6.

Measured on a synthetic singer (2026-10-25): 100 epochs on 10 minutes of songs took
~15 minutes and moved generated vocals toward the singer (resemblyzer 0.77 -> 0.86,
and the sung middle note into her range). Training holds ~20 GB of the card and the
engine cannot resume a run, so it is NOT sliced like voice training. It runs whole,
once WIS is idle and the card has room, on the separate `music-train` engine
(cfg.STYLE_WINDOW can restrict the start hours; by default it is any time).

Retention: the person's uploaded songs expire like everything else (90 days); the
trained adapter is the final model and is kept until the voice is deleted.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app import auth, db, gpuqueue, music, service
from app.config import cfg

MIN_SONGS = 3
MIN_TOTAL_S = 480               # 8 minutes; the pilot used 10
MAX_SONGS = 40
SONG_MIN_S, SONG_MAX_S = 30, 360
TRAIN_CAP_S = 240               # ACE-Step's preprocessing truncates each song here anyway
GPU_BUDGET_S = 90 * 60          # aim a run at about this long
TRAIN_WAIT_S = 72 * 3600
MAX_UPLOAD = 100 * 1024 * 1024

router = APIRouter()


def _root() -> Path:
    return cfg.TRAINING_DIR       # shared with music-train as /app/training


def _engine_path(*parts) -> str:
    return "/".join(("/app/training",) + parts)


# --------------------------------------------------------------------------- reads

def of_voice(voice_id: str):
    return db.one("SELECT * FROM style_models WHERE voice_id=?", (voice_id,))


def owned(user: auth.User, style_id: str):
    row = db.one("SELECT * FROM style_models WHERE id=?", (str(style_id or ""),))
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "no such style model")
    return row


def _songs(style_id: str):
    return db.query("SELECT * FROM style_songs WHERE style_model_id=? ORDER BY created_at",
                    (style_id,))


def song_view(s) -> dict:
    return {"id": s["id"], "title": s["title"], "duration_s": s["duration_s"],
            "lyrics": s["lyrics"] or "", "lyrics_source": s["lyrics_source"],
            "status": s["status"], "error": s["error"], **service.retention(s["updated_at"])}


def adapter_ready(row) -> bool:
    return bool(row and row["status"] == "ready" and
                (_root() / row["id"] / "adapter" / "lokr_weights.safetensors").exists())


def view(row) -> dict:
    songs = _songs(row["id"])
    ok = [s for s in songs if s["status"] == "ok"]
    total = sum(min(s["duration_s"] or 0, TRAIN_CAP_S) for s in ok)
    job = db.one("SELECT * FROM voice_jobs WHERE kind='style_train' AND params LIKE ? "
                 "ORDER BY created_at DESC LIMIT 1", (f'%"{row["id"]}"%',))
    status = row["status"]
    if status == "training" and job is not None and job["status"] == "failed":
        db.run("UPDATE style_models SET status='failed', last_error=?, updated_at=? WHERE id=?",
               (job["error"] or "training failed", db.now(), row["id"]))
        status = "failed"
    if status == "collecting" and len(ok) >= MIN_SONGS and total >= MIN_TOTAL_S:
        status = "ready_to_train"
    return {"id": row["id"], "voice_id": row["voice_id"], "status": status,
            "songs": len(songs), "songs_confirmed": len(ok),
            "songs_needing_review": sum(1 for s in songs if s["status"] == "needs_review"),
            "songs_transcribing": sum(1 for s in songs if s["status"] == "transcribing"),
            "total_s": round(total, 1), "minimum": {"songs": MIN_SONGS,
                                                    "minutes": MIN_TOTAL_S // 60},
            "training": {"epochs_target": row["epochs_target"], "trained_at": row["trained_at"],
                         "job_id": job["id"] if job else None,
                         "message": job["message"] if job and job["status"] != "succeeded"
                         else None,
                         "window": "any time the GPU is free" if tuple(cfg.STYLE_WINDOW) == (0, 24)
                         else f"{cfg.STYLE_WINDOW[0]:02d}:00-{cfg.STYLE_WINDOW[1]:02d}:00 UTC"},
            "strength_default": 0.8, "last_error": row["last_error"],
            "kept": "the trained style is kept until you delete it; the uploaded songs are "
                    f"deleted {cfg.RETENTION_DAYS} days after upload"}


# --------------------------------------------------------------------------- writes

def create(user: auth.User, voice_id: str) -> str:
    from app import voices
    voices.owned(user, voice_id)
    if of_voice(voice_id):
        raise HTTPException(409, "this voice already has a style model")
    sid = db.new_id("sty")
    db.run("INSERT INTO style_models (id,voice_id,user_id,status,epochs_target,created_at,"
           "updated_at) VALUES (?,?,?,?,0,?,?)",
           (sid, voice_id, user.id, "collecting", db.now(), db.now()))
    return sid


async def add_song(user: auth.User, style_id: str, filename: str, raw: bytes,
                   lyrics: str = "", title: str = "") -> str:
    from app import voices
    st = owned(user, style_id)
    if st["status"] == "training":
        raise HTTPException(409, "the style is training; add songs when it has finished")
    if not raw or len(raw) > MAX_UPLOAD:
        raise HTTPException(413 if raw else 400, "the file is empty or over 100 MB")
    if len(_songs(style_id)) >= MAX_SONGS:
        raise HTTPException(400, f"a style model can have at most {MAX_SONGS} songs")
    seconds = await voices._probe(raw)
    if seconds is None:
        raise HTTPException(400, "that file is not readable audio")
    if not SONG_MIN_S <= seconds <= SONG_MAX_S + 1:
        raise HTTPException(400, f"songs must be {SONG_MIN_S} s to {SONG_MAX_S // 60} minutes long")
    song_id = db.new_id("ssong")
    folder = _root() / style_id / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    wav = await music._to_wav(raw, "song", SONG_MAX_S)
    (folder / f"{song_id}.wav").write_bytes(wav)
    lyrics = (lyrics or "").strip()[:4000]
    db.run("INSERT INTO style_songs (id,style_model_id,user_id,title,duration_s,lyrics,"
           "lyrics_source,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (song_id, style_id, user.id, (title or filename or "song")[:120], round(seconds, 1),
            lyrics, "given" if lyrics else "transcribing",
            "ok" if lyrics else "transcribing", db.now(), db.now()))
    if not lyrics:
        asyncio.create_task(_draft_lyrics(song_id, wav))
    return song_id


async def _draft_lyrics(song_id: str, wav: bytes) -> None:
    """Whisper on the CPU (the tts service) drafts the words; the person must confirm
    them, because wrong lyrics teach wrong pronunciation."""
    try:
        text = await music.transcribe_source(wav)
        words = text.replace(",", " ").replace("-", " ").lower().split()
        if len(words) >= 20 and len(set(words)) / len(words) < 0.15:
            # Measured: Whisper loops on some sung takes ("hmm, hmm, hmm…", "ooh-ooh-…").
            db.run("UPDATE style_songs SET lyrics='', lyrics_source='transcribed', "
                   "status='needs_review', error=?, updated_at=? WHERE id=?",
                   ("could not make out the words in this song; please type the lyrics",
                    db.now(), song_id))
            return
        db.run("UPDATE style_songs SET lyrics=?, lyrics_source='transcribed', "
               "status='needs_review', updated_at=? WHERE id=?", (text, db.now(), song_id))
    except Exception as exc:                                   # noqa: BLE001
        db.run("UPDATE style_songs SET status='needs_review', lyrics_source='transcribed', "
               "error=?, updated_at=? WHERE id=?",
               (f"could not draft the lyrics ({exc}); type them in", db.now(), song_id))


def confirm_song(user: auth.User, style_id: str, song_id: str, lyrics: str | None,
                 confirmed: bool) -> None:
    owned(user, style_id)
    s = db.one("SELECT * FROM style_songs WHERE id=? AND style_model_id=?", (song_id, style_id))
    if s is None:
        raise HTTPException(404, "no such song")
    if s["status"] == "transcribing":
        raise HTTPException(409, "the lyrics are still being drafted")
    text = (lyrics if lyrics is not None else s["lyrics"] or "").strip()[:4000]
    if confirmed and not text:
        raise HTTPException(400, "give the lyrics before confirming (or [Instrumental])")
    db.run("UPDATE style_songs SET lyrics=?, status=?, error=NULL, updated_at=? WHERE id=?",
           (text, "ok" if confirmed else "needs_review", db.now(), song_id))


def delete_song(user: auth.User, style_id: str, song_id: str) -> None:
    st = owned(user, style_id)
    if st["status"] == "training":
        raise HTTPException(409, "the style is training; remove songs when it has finished")
    (_root() / style_id / "audio" / f"{song_id}.wav").unlink(missing_ok=True)
    db.run("DELETE FROM style_songs WHERE id=? AND style_model_id=?", (song_id, style_id))


def start_training(user: auth.User, style_id: str) -> str:
    from app import voices
    st = owned(user, style_id)
    v = view(st)
    if v["status"] == "training":
        raise HTTPException(409, "the style is already training")
    if v["songs_needing_review"] or v["songs_transcribing"]:
        raise HTTPException(409, f"confirm the lyrics of {v['songs_needing_review']} song(s) "
                                 "first (or remove them)" if v["songs_needing_review"] else
                            "lyrics are still being drafted; confirm them when they appear")
    if v["status"] not in ("ready_to_train", "ready", "failed"):
        need = []
        if v["songs_confirmed"] < MIN_SONGS:
            need.append(f"{MIN_SONGS - v['songs_confirmed']} more song(s) with confirmed lyrics")
        if v["total_s"] < MIN_TOTAL_S:
            need.append(f"about {round((MIN_TOTAL_S - v['total_s']) / 60) or 1} more minute(s)")
        raise HTTPException(400, "not enough audio yet: " + " and ".join(need))
    today = db.one("SELECT COUNT(*) AS n FROM voice_jobs WHERE kind='style_train' AND "
                   "user_id=? AND created_at > ?", (user.id, db.now() - 86400))["n"]
    if today >= 1 and not auth.is_unlimited(user.id):
        raise HTTPException(429, "a style can be trained once a day")
    jid = voices._job(user.id, st["voice_id"], "style_train", {"style_id": style_id},
                      wait=TRAIN_WAIT_S)
    db.run("UPDATE voice_jobs SET message=? WHERE id=?",
           ("waiting for the GPU to be free (training needs most of it for up to ~90 min)"
            if v["training"]["window"].startswith("any") else
            f"waiting for the training window ({v['training']['window']}) and a free GPU", jid))
    db.run("UPDATE style_models SET status='training', last_error=NULL, updated_at=? WHERE id=?",
           (db.now(), style_id))
    return jid


async def delete(user: auth.User, style_id: str) -> None:
    st = owned(user, style_id)
    if db.one("SELECT 1 FROM voice_jobs WHERE kind='style_train' AND status='generating' AND "
              "params LIKE ?", (f'%"{style_id}"%',)):
        raise HTTPException(409, "the style is training right now; delete it when it finishes")
    remove(st["id"])


def remove(style_id: str) -> None:
    db.run("UPDATE voice_jobs SET status='failed', error='the style model was deleted' "
           "WHERE kind='style_train' AND status='queued' AND params LIKE ?", (f'%"{style_id}"%',))
    shutil.rmtree(_root() / style_id, ignore_errors=True)
    db.run("DELETE FROM style_songs WHERE style_model_id=?", (style_id,))
    db.run("DELETE FROM style_models WHERE id=?", (style_id,))


def purge_older_than(cutoff: float) -> None:
    """Uploaded songs expire; the trained adapter (style_models row + adapter/) does not."""
    for s in db.query("SELECT * FROM style_songs WHERE updated_at < ?", (cutoff,)):
        (_root() / s["style_model_id"] / "audio" / f"{s['id']}.wav").unlink(missing_ok=True)
        db.run("DELETE FROM style_songs WHERE id=?", (s["id"],))


# --------------------------------------------------------------------------- GPU work

async def _engine(method: str, path: str, timeout: float = 600.0, **kw) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{cfg.STYLE_URL}{path}", **kw)
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        raise gpuqueue.Busy()
    body = resp.json() if resp.content else {}
    if resp.status_code != 200 or body.get("code", 200) != 200:
        detail = str(body.get("error") or body.get("detail") or resp.status_code)
        if music._oom(detail):
            raise gpuqueue.Busy()
        raise RuntimeError(f"the style engine: {detail[:300]}")
    return body.get("data") or {}


async def _wait(path: str, done, job_id: str, label: str, limit_s: float) -> dict:
    loop = asyncio.get_running_loop()
    end = loop.time() + limit_s
    while True:
        await asyncio.sleep(10)
        d = await _engine("GET", path)
        if done(d):
            return d
        if d.get("status"):
            db.run("UPDATE voice_jobs SET message=?, updated_at=? WHERE id=?",
                   (f"{label}: {str(d['status'])[:160]}", db.now(), job_id))
        if loop.time() > end:
            raise RuntimeError(f"{label} took longer than {int(limit_s // 60)} minutes")


def _epochs(total_s: float) -> int:
    # Pilot: ~0.9 s per epoch per minute of audio on xl-turbo (7-11 s/epoch at 10 min).
    per_epoch = max(5.0, 0.9 * total_s / 60)
    return max(min(80, cfg.STYLE_MAX_EPOCHS),
               min(cfg.STYLE_MAX_EPOCHS, int(GPU_BUDGET_S / per_epoch) // 10 * 10))


async def run_training(row, p: dict) -> None:
    """One whole run on the style engine: stage, scan, preprocess, train, keep the adapter."""
    st = db.one("SELECT * FROM style_models WHERE id=?", (p["style_id"],))
    if st is None:
        db.run("UPDATE voice_jobs SET status='failed', error='the style model was deleted' "
               "WHERE id=?", (row["id"],))
        return
    from app import voices
    prof = voices._profile(st["voice_id"])
    sid = st["id"]
    base = _root() / sid
    work = base / "dataset"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tag = f"{sid}_voice"
    caption = ", ".join(x for x in (prof.get("style_caption"), "solo lead vocal") if x)
    songs = [s for s in _songs(sid) if s["status"] == "ok"]
    total = 0.0
    for s in songs:
        src = base / "audio" / f"{s['id']}.wav"
        if not src.exists():
            continue
        # A hard link, not a symlink: the engine resolves a symlink and then looks for
        # the caption/lyrics next to the TARGET, finds none, and labels nothing.
        try:
            (work / f"{s['id']}.wav").hardlink_to(src)
        except OSError:
            shutil.copy2(src, work / f"{s['id']}.wav")
        (work / f"{s['id']}.lyrics.txt").write_text(s["lyrics"] or "[Instrumental]")
        (work / f"{s['id']}.caption.txt").write_text(caption)
        total += min(s["duration_s"] or 0, TRAIN_CAP_S)
    epochs = _epochs(total)
    db.run("UPDATE style_models SET epochs_target=?, updated_at=? WHERE id=?",
           (epochs, db.now(), sid))
    voices._set(row["id"], stage="preparing", message="loading the song model", progress=0.02)
    await _engine("POST", "/v1/init", json={"init_llm": False}, timeout=900.0)
    await _engine("POST", "/v1/dataset/scan", json={
        "audio_dir": _engine_path(sid, "dataset"), "dataset_name": sid, "custom_tag": tag,
        "tag_position": "prepend", "all_instrumental": False})
    await _engine("POST", "/v1/dataset/save", json={
        "save_path": _engine_path(sid, "dataset.json"), "dataset_name": sid,
        "custom_tag": tag, "tag_position": "prepend", "all_instrumental": False})
    voices._set(row["id"], stage="preprocessing", message="preparing the songs", progress=0.05)
    task = await _engine("POST", "/v1/dataset/preprocess_async", json={
        "output_dir": _engine_path(sid, "tensors"), "skip_existing": False})
    # Poll THIS task: the "latest" route says "idle" whenever it knows no task (e.g. the
    # engine restarted), which is not the same as finished.
    d = await _wait(f"/v1/dataset/preprocess_status/{task['task_id']}",
                    lambda d: d.get("status") in ("completed", "failed"),
                    row["id"], "preparing", 1800)
    import re
    m = re.search(r"Preprocessed (\d+)/(\d+)", str(d.get("progress") or ""))
    if d.get("status") != "completed" or not m or int(m.group(1)) == 0:
        raise RuntimeError(f"preparing the songs failed: {str(d.get('progress'))[:200]}")
    voices._set(row["id"], stage="training", message=f"training {epochs} epochs", progress=0.1)
    await _engine("POST", "/v1/training/start_lokr", json={
        "tensor_dir": _engine_path(sid, "tensors"), "output_dir": _engine_path(sid, "lokr"),
        "train_epochs": epochs, "save_every_n_epochs": 10})

    def finished(d):
        ep = d.get("current_epoch") or 0
        db.run("UPDATE voice_jobs SET progress=? WHERE id=?",
               (round(0.1 + 0.85 * min(1.0, ep / epochs), 3), row["id"]))
        return not d.get("is_training")
    d = await _wait("/v1/training/status", finished, row["id"], "training", 4 * 3600)
    if d.get("error"):
        raise RuntimeError(f"training failed: {str(d['error'])[:300]}")
    final = base / "lokr" / "final"
    if not (final / "lokr_weights.safetensors").exists():
        ckpts = sorted((base / "lokr" / "checkpoints").glob("epoch_*"),
                       key=lambda q: int(q.name.split("_")[1]))
        if not ckpts:
            raise RuntimeError("training produced no adapter")
        final = ckpts[-1]
    adapter = base / "adapter"
    shutil.rmtree(adapter, ignore_errors=True)
    shutil.copytree(final, adapter)
    for leftover in ("tensors", "lokr", "dataset", "dataset.json"):
        q = base / leftover
        shutil.rmtree(q, ignore_errors=True) if q.is_dir() else q.unlink(missing_ok=True)
    db.run("UPDATE style_models SET status='ready', trained_at=?, last_error=NULL, updated_at=? "
           "WHERE id=?", (db.now(), db.now(), sid))
    voices._set(row["id"], status="succeeded", stage="done", message=None, progress=1.0,
                result=json.dumps({"style_id": sid, "epochs": epochs}))
    # The engine exits itself ~45 s after this (music/train_entry.py), which also drops
    # the adapter training left on its decoder.


async def render(style_id: str, form: dict, files: dict | None, strength: float) -> tuple[dict, bytes]:
    """Render a song on the style engine with this person's adapter loaded."""
    await _engine("POST", "/v1/init", json={"init_llm": False}, timeout=900.0)
    try:
        await _engine("POST", "/v1/lora/unload")
    except RuntimeError:
        pass                                   # "no adapter loaded" is the normal case
    await _engine("POST", "/v1/lora/load",
                  json={"lora_path": _engine_path(style_id, "adapter")})
    await _engine("POST", "/v1/lora/scale", json={"scale": max(0.0, min(1.0, strength))})
    form = {**form, "model": music.TURBO,
            "prompt": f"{style_id}_voice, " + form.get("prompt", "")}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{cfg.STYLE_URL}/release_task", data=form, files=files)
            body = resp.json()
            if resp.status_code != 200 or body.get("code") != 200:
                raise RuntimeError(body.get("error") or "the style engine refused the song")
            tid = body["data"]["task_id"]
            for _ in range(400):
                await asyncio.sleep(3)
                q = (await client.post(f"{cfg.STYLE_URL}/query_result",
                                       json={"task_id_list": [tid]})).json()
                item = (q.get("data") or [{}])[0]
                if item.get("status") == 1:
                    break
                if item.get("status") == 2:
                    detail = str(item.get("result") or "")
                    if music._oom(detail):
                        raise gpuqueue.Busy()
                    raise RuntimeError(f"the song failed: {detail[:200]}")
            else:
                raise RuntimeError("the song took too long")
            results = json.loads(item["result"]) if isinstance(item["result"], str) \
                else item["result"]
            first = results[0]
            audio = await client.get(f"{cfg.STYLE_URL}{first['file']}")
            audio.raise_for_status()
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        raise gpuqueue.Busy()
    return first, audio.content


# --------------------------------------------------------------------------- /v1 API

def _who(request: Request) -> auth.User:
    from app import voices
    return voices._who(request)


@router.post("/v1/voices/{voice_id}/style-model", status_code=201)
async def api_create(request: Request, voice_id: str):
    user = _who(request)
    return view(owned(user, create(user, voice_id)))


@router.get("/v1/style-models/{style_id}")
async def api_get(request: Request, style_id: str):
    return view(owned(_who(request), style_id))


@router.delete("/v1/style-models/{style_id}", status_code=204)
async def api_delete(request: Request, style_id: str):
    await delete(_who(request), style_id)


@router.post("/v1/style-models/{style_id}/songs", status_code=201)
async def api_add_song(request: Request, style_id: str, file: UploadFile = File(...),
                       lyrics: str = Form(""), title: str = Form("")):
    user = _who(request)
    sid = await add_song(user, style_id, file.filename or "song",
                         await file.read(MAX_UPLOAD + 1), lyrics, title)
    return song_view(db.one("SELECT * FROM style_songs WHERE id=?", (sid,)))


@router.get("/v1/style-models/{style_id}/songs")
async def api_songs(request: Request, style_id: str):
    owned(_who(request), style_id)
    return {"items": [song_view(s) for s in _songs(style_id)]}


@router.patch("/v1/style-models/{style_id}/songs/{song_id}")
async def api_confirm(request: Request, style_id: str, song_id: str):
    user = _who(request)
    body = await request.json()
    confirm_song(user, style_id, song_id, body.get("lyrics"), bool(body.get("confirmed")))
    return song_view(db.one("SELECT * FROM style_songs WHERE id=?", (song_id,)))


@router.delete("/v1/style-models/{style_id}/songs/{song_id}", status_code=204)
async def api_delete_song(request: Request, style_id: str, song_id: str):
    delete_song(_who(request), style_id, song_id)


@router.post("/v1/style-models/{style_id}/train", status_code=202)
async def api_train(request: Request, style_id: str):
    from app import voices
    user = _who(request)
    jid = start_training(user, style_id)
    return voices.job_view(voices.owned_job(user, jid))
