"""Personal voices: train a voice from someone's own recordings, then use it.

Layer 1 is the RVC voice model, layer 2 the measured profile (range, vibrato,
tone) — see docs/VOICE_API_SPEC.md. The GPU work happens in the `rvc`
container (rvc/server.py); this module owns every rule and all state, and both
front doors — the /v1 API (pages and outside apps) and the MCP tools — call it.

Jobs live in `voice_jobs` and run through app.gpuqueue like speech and music:
- clean    one uploaded recording -> a dry vocal in the voice's dataset
- train    prepare, then ~5-minute training slices that go to the BACK of the
           queue after each one (so WIS never waits long), then finalize
- resing   change the singer of a song
- song     compose a new song in the person's manner, then sing it in the voice
- speech   say text in the voice

Retention: clips and job outputs are deleted RETENTION_DAYS after they last
changed, like everything else; the trained model and its profile are kept
until the owner deletes the voice (the user's rule, 2026-10-25).
"""
from __future__ import annotations

import asyncio
import json
import math
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app import auth, db, gpuqueue, music, oauth, service, speech
from app.config import cfg

CONSENT_VERSION = "2026-10-25"
CONSENT_TEXT = ("This is my own voice, or I have the explicit permission of the person whose "
                "voice it is. I won't use it to impersonate anyone or to deceive. I can delete "
                "it at any time.")
MAX_VOICES = 3
MAX_CLIPS = 40
MAX_TOTAL_S = 3600
CLIP_MIN_S, CLIP_MAX_S = 5, 600
# Usable seconds of singing (or of speech, for a speech voice). RVC voices are commonly
# trained on ~5 min; 10-30 min covers more of the range and sounds fuller, so under
# RECOMMENDED_S the voice trains but carries a warning. (Duplicating clips would pass
# the gate without adding anything: it is the same as training longer.)
MIN_TRAIN_S = 300
RECOMMENDED_S = 600
# rvc analysis: share of voiced time spent on held notes. Measured: studio speech clips
# 0.00-0.15, a real person's Bollywood singing 0.54, the synthetic test singer ~0.78.
# (0.6 -- set from the synthetic singer alone -- called the real singing speech.)
SINGING_RATIO = 0.3
TARGET_EPOCHS = {"standard": 300, "high": 500}
SLICE_BUDGET_S = 300           # one training slice holds the GPU for about this long
SLICE_GRAIN = 10               # rvc/ops.py saves only on multiples of this epoch
TRAIN_WAIT_S = 72 * 3600       # a training job may wait this long in total for the card
MAX_UPLOAD = 100 * 1024 * 1024
JOB_KINDS = ("clean", "train", "resing", "song", "speech", "style_train")
USE_KINDS = ("resing", "song", "speech")

router = APIRouter()


def _dir(*parts) -> Path:
    p = cfg.voices_dir
    for part in parts:
        p = p / part
    return p


# --------------------------------------------------------------------------- reads

def owned(user: auth.User, voice_id: str):
    row = db.one("SELECT * FROM voices WHERE id=?", (str(voice_id or ""),))
    # 404 (never 403) for someone else's voice, so ids don't leak.
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "no such voice")
    return row


def owned_job(user: auth.User, job_id: str):
    row = db.one("SELECT * FROM voice_jobs WHERE id=?", (str(job_id or ""),))
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "no such job")
    return row


def _clips(voice_id: str):
    return db.query("SELECT * FROM voice_clips WHERE voice_id=? ORDER BY created_at", (voice_id,))


def dataset(voice_id: str) -> dict:
    singing = speech_s = 0.0
    n = ok = 0
    warnings = []
    for c in _clips(voice_id):
        n += 1
        if c["status"] != "ok":
            continue
        ok += 1
        a = json.loads(c["analysis"] or "{}")
        if c["kind"] == "singing":
            singing += a.get("usable_s") or 0
        else:
            speech_s += a.get("usable_s") or 0
    if singing < MIN_TRAIN_S and speech_s >= MIN_TRAIN_S:
        warnings.append("trained mostly on speech — singing will sound thin")
    elif MIN_TRAIN_S <= singing < RECOMMENDED_S:
        warnings.append(f"{singing / 60:.1f} minutes of singing is enough to train; "
                        f"{RECOMMENDED_S // 60}+ minutes (more songs, low and high notes) "
                        "will sound fuller")
    need = 0 if (singing >= MIN_TRAIN_S or speech_s >= MIN_TRAIN_S) else \
        math.ceil((MIN_TRAIN_S - max(singing, speech_s)) / 60)
    return {"clips": n, "clips_ok": ok, "singing_s": round(singing, 1),
            "speech_s": round(speech_s, 1), "total_s": round(singing + speech_s, 1),
            "minutes_needed": need, "usable": need == 0, "warnings": warnings}


def _status(row, ds: dict) -> str:
    if row["status"] in ("training", "ready", "failed"):
        return row["status"]
    return "ready_to_train" if ds["usable"] else "collecting"


def voice_view(row) -> dict:
    ds = dataset(row["id"])
    job = db.one("SELECT * FROM voice_jobs WHERE voice_id=? AND kind='train' "
                 "ORDER BY created_at DESC LIMIT 1", (row["id"],))
    if row["status"] == "training" and job is not None and job["status"] == "failed":
        # The queue fails a job itself (GPU busy too long, out-of-memory retries).
        db.run("UPDATE voices SET status='failed', last_error=?, updated_at=? WHERE id=?",
               (job["error"] or "training failed", db.now(), row["id"]))
        row = db.one("SELECT * FROM voices WHERE id=?", (row["id"],))
    out = {"id": row["id"], "name": row["name"], "status": _status(row, ds),
           "created_at": row["created_at"],
           "consent": {"attested": True, "statement_version": row["consent_version"],
                       "attested_at": row["consent_at"]},
           "dataset": ds,
           "training": {"epochs_done": row["epochs_done"], "epochs_target": row["epochs_target"],
                        "slices_done": row["slices_done"], "trained_at": row["trained_at"],
                        "job_id": job["id"] if job else None,
                        "message": job["message"] if job and job["status"] != "succeeded" else None,
                        "queue_position": gpuqueue.position("voice_jobs", job["id"]) if job else None},
           "profile": json.loads(row["profile"]) if row["profile"] else None,
           "kept": "until you delete it (the trained voice and its profile never expire)",
           "last_error": row["last_error"]}
    from app import styles
    st = styles.of_voice(row["id"])
    out["style_model"] = styles.view(st) if st else None
    return out


def clip_view(c) -> dict:
    a = json.loads(c["analysis"] or "{}")
    return {"id": c["id"], "name": c["name"], "kind": c["kind"], "status": c["status"],
            "duration_s": a.get("original_s"), "usable_s": a.get("usable_s"),
            "checks": {k: a.get(k) for k in ("music_removed", "noise_db", "clipping",
                                             "singing_ratio") if k in a},
            "pitch": a.get("f0"), "rejected_reason": c["rejected_reason"],
            "error": c["error"], **service.retention(c["updated_at"])}


def job_view(j, base: str = "") -> dict:
    out = {"id": j["id"], "kind": j["kind"], "status": j["status"], "voice_id": j["voice_id"],
           "message": j["message"], "stage": j["stage"], "progress": j["progress"],
           "error": j["error"], "created_at": j["created_at"], "updated_at": j["updated_at"],
           "queue_position": gpuqueue.position("voice_jobs", j["id"]),
           **service.retention(j["updated_at"])}
    if j["status"] == "succeeded" and j["kind"] in USE_KINDS:
        res = json.loads(j["result"] or "{}")
        files = res.get("files") or []
        links = {}
        for name in files:
            links[name.split(".")[0]] = base + service.signed_path(
                "voice", f"{j['id']}~{name}", 7 * 86400) if base else \
                f"/v1/jobs/{j['id']}/files/{name}"
        out["result"] = {k: v for k, v in res.items() if k != "files"}
        out["result"]["audio_url"] = links.get("mix") or links.get("out")
        out["result"]["stems"] = {k: v for k, v in links.items() if k not in ("mix", "out")}
    return out


# --------------------------------------------------------------------------- writes

def create(user: auth.User, name: str, attest: bool, ip: str | None) -> str:
    if attest is not True:
        raise HTTPException(400, "tick the consent statement: the voice must be yours, or you "
                                 "must have the permission of the person it belongs to")
    name = (name or "").strip()[:60] or "My voice"
    n = db.one("SELECT COUNT(*) AS n FROM voices WHERE user_id=?", (user.id,))["n"]
    if n >= MAX_VOICES and not auth.is_unlimited(user.id):
        raise HTTPException(400, f"you already have {MAX_VOICES} voices; delete one first")
    vid = db.new_id("voi")
    db.run("INSERT INTO voices (id,user_id,name,status,consent_version,consent_at,consent_ip,"
           "epochs_done,epochs_target,slices_done,created_at,updated_at) "
           "VALUES (?,?,?,?,?,?,?,0,0,0,?,?)",
           (vid, user.id, name, "collecting", CONSENT_VERSION, db.now(), (ip or "")[:64],
            db.now(), db.now()))
    return vid


async def add_clip(user: auth.User, voice_id: str, filename: str, raw: bytes,
                   kind: str = "auto") -> str:
    v = owned(user, voice_id)
    if v["status"] == "training":
        raise HTTPException(409, "the voice is training; add clips when it has finished")
    if not raw:
        raise HTTPException(400, "the file is empty")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "the file is over 100 MB")
    clips = _clips(voice_id)
    if len(clips) >= MAX_CLIPS:
        raise HTTPException(400, f"a voice can have at most {MAX_CLIPS} recordings")
    if kind not in ("auto", "singing", "speech"):
        raise HTTPException(400, "kind must be singing, speech or auto")
    seconds = await _probe(raw)
    if seconds is None:
        raise HTTPException(400, "that file is not readable audio")
    if seconds < CLIP_MIN_S:
        raise HTTPException(400, f"recordings must be at least {CLIP_MIN_S} seconds")
    if seconds > CLIP_MAX_S + 1:
        raise HTTPException(400, f"recordings can be at most {CLIP_MAX_S // 60} minutes; split it")
    total = sum((json.loads(c["analysis"] or "{}").get("original_s") or 0) for c in clips)
    if total + seconds > MAX_TOTAL_S:
        raise HTTPException(400, f"a voice can hold at most {MAX_TOTAL_S // 60} minutes of audio")
    cid = db.new_id("clip")
    _dir("inputs").mkdir(parents=True, exist_ok=True)
    (_dir("inputs") / cid).write_bytes(raw)
    db.run("INSERT INTO voice_clips (id,voice_id,user_id,name,kind,status,created_at,updated_at) "
           "VALUES (?,?,?,?,?,?,?,?)", (cid, voice_id, user.id, (filename or "clip")[:120],
                                        kind, "cleaning", db.now(), db.now()))
    _job(user.id, voice_id, "clean", {"clip_id": cid})
    db.run("UPDATE voices SET status=CASE WHEN status='failed' THEN 'failed' ELSE "
           "'collecting' END, updated_at=? WHERE id=? AND status NOT IN ('training','ready')",
           (db.now(), voice_id))
    return cid


async def delete_clip(user: auth.User, voice_id: str, clip_id: str) -> None:
    v = owned(user, voice_id)
    if v["status"] == "training":
        raise HTTPException(409, "the voice is training; delete clips when it has finished")
    c = db.one("SELECT * FROM voice_clips WHERE id=? AND voice_id=?", (clip_id, voice_id))
    if c is None:
        raise HTTPException(404, "no such clip")
    await _rvc_delete(f"/clips/{voice_id}/{clip_id}")
    (_dir("inputs") / clip_id).unlink(missing_ok=True)
    db.run("DELETE FROM voice_clips WHERE id=?", (clip_id,))


def start_training(user: auth.User, voice_id: str, quality: str = "standard") -> str:
    v = owned(user, voice_id)
    if quality not in TARGET_EPOCHS:
        raise HTTPException(400, "quality must be standard or high")
    if v["status"] == "training":
        raise HTTPException(409, "the voice is already training")
    if db.one("SELECT 1 FROM voice_clips WHERE voice_id=? AND status='cleaning'", (voice_id,)):
        raise HTTPException(409, "some recordings are still being cleaned; train when they finish")
    ds = dataset(voice_id)
    if not ds["usable"]:
        raise HTTPException(400, f"not enough audio yet: about {ds['minutes_needed']} more "
                                 f"minute(s) of clean singing needed (minimum "
                                 f"{MIN_TRAIN_S // 60} min)")
    today = db.one("SELECT COUNT(*) AS n FROM voice_jobs WHERE voice_id=? AND kind='train' "
                   "AND created_at > ?", (voice_id, db.now() - 86400))["n"]
    if today >= 3 and not auth.is_unlimited(user.id):
        raise HTTPException(429, "a voice can be trained at most 3 times a day")
    jid = _job(user.id, voice_id, "train", {"quality": quality, "target": TARGET_EPOCHS[quality]},
               wait=TRAIN_WAIT_S)
    db.run("UPDATE voices SET status='training', epochs_done=0, epochs_target=?, slices_done=0, "
           "last_error=NULL, updated_at=? WHERE id=?", (TARGET_EPOCHS[quality], db.now(), voice_id))
    return jid


async def use(user: auth.User, voice_id: str, kind: str, params: dict,
              source: bytes | None = None) -> str:
    """Queue a resing / song / speech job with a ready voice."""
    v = owned(user, voice_id)
    if v["status"] != "ready" or not v["profile"]:
        raise HTTPException(409, "this voice is not trained yet" if v["status"] != "training"
                            else "this voice is training; use it when training has finished")
    if kind == "resing":
        if source is None:
            raise HTTPException(400, "give the song to re-sing")
        key = str(params.get("key", "auto"))
        if key != "auto":
            try:
                if not -24 <= int(key) <= 24:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "key must be auto or a whole number of semitones -24..24")
    elif kind == "speech":
        text = (params.get("text") or "").strip()
        if not text or len(text) > 2000:
            raise HTTPException(400, "text must be 1 to 2000 characters")
    elif kind == "song":
        if not (params.get("description") or params.get("lyrics")):
            raise HTTPException(400, "describe the song, or give lyrics")
    else:
        raise HTTPException(400, "unknown use")
    _rate(user, kind)
    jid = _job(user.id, voice_id, kind, params)
    if source is not None:
        if len(source) > MAX_UPLOAD:
            db.run("DELETE FROM voice_jobs WHERE id=?", (jid,))
            raise HTTPException(413, "the file is over 100 MB")
        _dir("inputs").mkdir(parents=True, exist_ok=True)
        (_dir("inputs") / jid).write_bytes(source)
    gpuqueue.kick()
    return jid


def _rate(user: auth.User, kind: str) -> None:
    if auth.is_unlimited(user.id):
        return
    hour = db.now() - 3600
    if kind == "speech":
        n = db.one("SELECT COUNT(*) AS n FROM voice_jobs WHERE user_id=? AND kind='speech' AND "
                   "created_at > ? AND status != 'failed'", (user.id, hour))["n"]
        if n >= cfg.USER_SPEECH_PER_HOUR:
            raise HTTPException(429, "hourly speech limit reached")
    else:
        # Shares the hourly music allowance: songs made in either place both count.
        n = db.one("SELECT COUNT(*) AS n FROM voice_jobs WHERE user_id=? AND kind IN "
                   "('resing','song') AND created_at > ? AND status != 'failed'",
                   (user.id, hour))["n"]
        m = db.one("SELECT COUNT(*) AS n FROM music WHERE user_id=? AND created_at > ? "
                   "AND status != 'failed'", (user.id, hour))["n"]
        if n + m >= cfg.USER_MUSIC_PER_HOUR:
            raise HTTPException(429, "hourly music limit reached")


def _job(user_id: str, voice_id: str, kind: str, params: dict, wait: float | None = None) -> str:
    jid = db.new_id("vjob")
    db.run("INSERT INTO voice_jobs (id,user_id,voice_id,kind,params,status,message,"
           "wait_limit_s,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (jid, user_id, voice_id, kind, json.dumps(params), "queued", "queued", wait,
            db.now(), db.now()))
    gpuqueue.kick()
    return jid


async def delete_voice(user: auth.User, voice_id: str) -> None:
    owned(user, voice_id)
    if db.one("SELECT 1 FROM voice_jobs WHERE voice_id=? AND status='generating'", (voice_id,)):
        raise HTTPException(409, "the voice is busy on the GPU; delete it in a minute")
    await _remove_voice(voice_id)


async def _remove_voice(voice_id: str) -> None:
    from app import styles
    st = styles.of_voice(voice_id)
    if st:
        styles.remove(st["id"])
    await _rvc_delete(f"/voices/{voice_id}")
    for c in _clips(voice_id):
        (_dir("inputs") / c["id"]).unlink(missing_ok=True)
    for j in db.query("SELECT id, user_id FROM voice_jobs WHERE voice_id=?", (voice_id,)):
        _remove_job_files(j)
    db.run("UPDATE voice_jobs SET status='failed', error='the voice was deleted' "
           "WHERE voice_id=? AND status='queued'", (voice_id,))
    db.run("DELETE FROM voice_clips WHERE voice_id=?", (voice_id,))
    db.run("DELETE FROM voices WHERE id=?", (voice_id,))


async def delete_for_user(user_id: str) -> None:
    for v in db.query("SELECT id FROM voices WHERE user_id=?", (user_id,)):
        await _remove_voice(v["id"])
    db.run("DELETE FROM voice_jobs WHERE user_id=?", (user_id,))


def _remove_job_files(j) -> None:
    shutil.rmtree(_dir("out", j["user_id"], j["id"]), ignore_errors=True)
    (_dir("inputs") / j["id"]).unlink(missing_ok=True)


async def purge_older_than(cutoff: float) -> None:
    """Clips and job outputs expire like everything else; voices + profiles never do."""
    from app import styles
    styles.purge_older_than(cutoff)
    for c in db.query("SELECT * FROM voice_clips WHERE updated_at < ? AND status != 'cleaning'",
                      (cutoff,)):
        await _rvc_delete(f"/clips/{c['voice_id']}/{c['id']}")
        (_dir("inputs") / c["id"]).unlink(missing_ok=True)
        db.run("DELETE FROM voice_clips WHERE id=?", (c["id"],))
    for j in db.query("SELECT * FROM voice_jobs WHERE updated_at < ? AND status IN "
                      "('succeeded','failed')", (cutoff,)):
        _remove_job_files(j)
        db.run("DELETE FROM voice_jobs WHERE id=?", (j["id"],))


# --------------------------------------------------------------------------- GPU work

async def _rvc(method: str, path: str, timeout: float = 3600.0, **kw) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{cfg.RVC_URL}{path}", **kw)
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        raise gpuqueue.Busy()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"the voice service is unreachable ({type(exc).__name__})")
    if resp.status_code == 503:
        raise gpuqueue.Busy()
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        raise RuntimeError(body.get("detail") or f"the voice service answered {resp.status_code}")
    return body


async def _rvc_delete(path: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.delete(f"{cfg.RVC_URL}{path}")
    except httpx.HTTPError:
        pass


async def _rvc_fetch(job_id: str, names: list[str], dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    got = []
    async with httpx.AsyncClient(timeout=300.0) as client:
        for name in names:
            r = await client.get(f"{cfg.RVC_URL}/out/{job_id}/{name}")
            if r.status_code == 200:
                (dest / name).write_bytes(r.content)
                got.append(name)
        await client.delete(f"{cfg.RVC_URL}/out/{job_id}")
    return got


def _set(job_id: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE voice_jobs SET {sets}, updated_at=? WHERE id=?",
           (*fields.values(), db.now(), job_id))


async def run(row) -> None:
    """Called by app.gpuqueue when the card is free. Raises gpuqueue.Busy to retry."""
    if not db.one("SELECT 1 FROM voices WHERE id=?", (row["voice_id"],)):
        _set(row["id"], status="failed", error="the voice was deleted")
        return
    from app import styles
    handler = {"clean": _run_clean, "train": _run_train, "resing": _run_resing,
               "song": _run_song, "speech": _run_speech,
               "style_train": styles.run_training}[row["kind"]]
    try:
        await handler(row, json.loads(row["params"] or "{}"))
    except (gpuqueue.Busy, asyncio.CancelledError):
        raise
    except Exception as exc:                                   # noqa: BLE001
        if row["kind"] == "train":
            db.run("UPDATE voices SET status='failed', last_error=?, updated_at=? WHERE id=?",
                   (str(exc)[:500], db.now(), row["voice_id"]))
        if row["kind"] == "style_train":
            db.run("UPDATE style_models SET status='failed', last_error=?, updated_at=? "
                   "WHERE id=?", (str(exc)[:500], db.now(),
                                  json.loads(row["params"])["style_id"]))
        if row["kind"] == "clean":
            p = json.loads(row["params"])
            db.run("UPDATE voice_clips SET status='failed', error=?, updated_at=? WHERE id=?",
                   (str(exc)[:300], db.now(), p["clip_id"]))
        raise


async def _run_clean(row, p: dict) -> None:
    cid = p["clip_id"]
    clip = db.one("SELECT * FROM voice_clips WHERE id=?", (cid,))
    if clip is None:
        _set(row["id"], status="succeeded", message="clip was deleted")
        return
    raw = (_dir("inputs") / cid).read_bytes()
    _set(row["id"], message="cleaning the recording", stage="cleaning")
    a = await _rvc("POST", "/clips", data={"voice": row["voice_id"], "clip": cid},
                   files={"file": (clip["name"], raw)})
    (_dir("inputs") / cid).unlink(missing_ok=True)      # the cleaned copy is what we keep
    kind = clip["kind"]
    if kind == "auto":
        kind = "singing" if (a.get("singing_ratio") or 0) >= SINGING_RATIO else "speech"
    status, reason = "ok", None
    if (a.get("voiced_s") or 0) < 3:
        status, reason = "rejected", "no voice was found in this recording"
    elif (a.get("usable_s") or 0) < CLIP_MIN_S:
        status, reason = "rejected", "less than 5 seconds of usable voice after cleaning"
    db.run("UPDATE voice_clips SET status=?, kind=?, analysis=?, rejected_reason=?, updated_at=? "
           "WHERE id=?", (status, kind, json.dumps(a), reason, db.now(), cid))
    _set(row["id"], status="succeeded", message=None, stage="done", progress=1.0)


async def _run_train(row, p: dict) -> None:
    vid = row["voice_id"]
    v = db.one("SELECT * FROM voices WHERE id=?", (vid,))
    stage = row["stage"] or "prepare"
    target = int(p["target"])
    if stage == "prepare":
        _set(row["id"], message="preparing the recordings", stage="prepare")
        await _rvc("POST", f"/voices/{vid}/prepare")
        _requeue(row["id"], "training", "trained 0 epochs so far; waiting for the GPU", 0.05)
        return
    if stage == "training":
        done = v["epochs_done"] or 0
        per_epoch = p.get("s_per_epoch")
        step = 10 if not per_epoch else max(
            SLICE_GRAIN, int(SLICE_BUDGET_S / per_epoch) // SLICE_GRAIN * SLICE_GRAIN)
        until = min(target, done + step)
        _set(row["id"], message=f"training: epochs {done}→{until} of {target}", stage="training")
        res = await _rvc("POST", f"/voices/{vid}/train_slice", data={"until": str(until)},
                         timeout=1800.0)
        new = int(res.get("epochs_done") or done)
        if new > done and res.get("seconds"):
            # The first slice includes ~20 s of start-up; later slices refine it.
            p["s_per_epoch"] = max(0.5, res["seconds"] / (new - done))
            db.run("UPDATE voice_jobs SET params=? WHERE id=?", (json.dumps(p), row["id"]))
        db.run("UPDATE voices SET epochs_done=?, slices_done=slices_done+1, updated_at=? "
               "WHERE id=?", (new, db.now(), vid))
        finished = res.get("stopped_early") or new >= target
        if finished:
            _requeue(row["id"], "finalize", "training done; saving the voice", 0.95)
        else:
            _requeue(row["id"], "training", f"trained {new} of {target} epochs; "
                     "the GPU is shared, so training continues in slices", 0.05 + 0.9 * new / target)
        return
    if stage == "finalize":
        _set(row["id"], message="saving the voice and measuring your range", stage="finalize")
        res = await _rvc("POST", f"/voices/{vid}/finalize")
        prof = res["profile"]
        ref = prof.pop("reference_clip", None)
        prof["reference_clip_id"] = ref
        db.run("UPDATE voices SET status='ready', profile=?, trained_at=?, last_error=NULL, "
               "updated_at=? WHERE id=?", (json.dumps(prof), db.now(), db.now(), vid))
        _set(row["id"], status="succeeded", message=None, stage="done", progress=1.0,
             result=json.dumps({"profile": prof}))


def _requeue(job_id: str, stage: str, message: str, progress: float) -> None:
    """Back of the queue: whatever arrived during this slice runs before the next one."""
    db.run("UPDATE voice_jobs SET status='queued', stage=?, message=?, progress=?, attempts=0, "
           "created_at=?, updated_at=? WHERE id=?",
           (stage, message, round(progress, 3), db.now(), db.now(), job_id))
    gpuqueue.kick()


def _profile(voice_id: str) -> dict:
    v = db.one("SELECT profile FROM voices WHERE id=?", (voice_id,))
    return json.loads(v["profile"]) if v and v["profile"] else {}


async def _resing(row, p: dict, song: bytes, out_names=("mix.mp3", "vocal.mp3", "music.mp3",
                                                        "backing.mp3")) -> dict:
    prof = _profile(row["voice_id"])
    data = {"job": row["id"].replace("vjob_", "job_"), "key": str(p.get("key", "auto")),
            "keep_backing_vocals": "true" if p.get("keep_backing_vocals", True) else "false",
            "reverb": str(p.get("reverb", 0.2))}
    if prof.get("range", {}).get("mid_hz"):
        data["target_mid_hz"] = str(prof["range"]["mid_hz"])
    for k in ("vocal_db", "music_db", "backing_db"):
        if p.get(k) is not None:
            data[k] = str(int(p[k]))
    res = await _rvc("POST", f"/voices/{row['voice_id']}/resing", data=data,
                     files={"file": ("song", song)}, timeout=1800.0)
    files = await _rvc_fetch(data["job"], list(out_names), _dir("out", row["user_id"], row["id"]))
    return {"key_shift_semitones": res.get("key_shift_semitones"), "files": files}


async def _run_resing(row, p: dict) -> None:
    _set(row["id"], message="separating the song and re-singing it", stage="converting")
    song = (_dir("inputs") / row["id"]).read_bytes()
    res = await _resing(row, p, song)
    (_dir("inputs") / row["id"]).unlink(missing_ok=True)
    _set(row["id"], status="succeeded", message=None, stage="done", progress=1.0,
         result=json.dumps(res))


async def _run_song(row, p: dict) -> None:
    """Compose + perform with the song model, steered by the profile and the person's own
    singing as the sound reference, then put the exact voice on with RVC."""
    prof = _profile(row["voice_id"])
    _set(row["id"], message="writing and performing the song", stage="performing")
    if not p.get("language"):
        # Left on auto, the composer once wrote Catalan lyrics for an English request.
        # Plain-ASCII text means English; anything else (Devanagari, accents) stays auto.
        text = p.get("description") or p.get("lyrics") or ""
        p["language"] = "en" if text.isascii() else None
    style = ", ".join(x for x in (p.get("style") or "", prof.get("style_caption") or "",
                                  "solo lead vocal") if x)
    form = {"audio_format": "wav", "batch_size": "1", "model": music.TURBO,
            "vocal_language": p.get("language") or "unknown", "inference_steps": "8",
            "shift": "3.0", "use_cot_caption": "false"}
    if p.get("lyrics"):
        form.update(prompt=style, lyrics=p["lyrics"], thinking="true")
    else:
        # Let the composer write it, then render with the person's style text.
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(f"{cfg.MUSIC_URL}/v1/create_sample", json={
                "query": p["description"], "instrumental": False,
                "vocal_language": p.get("language") or "unknown"})
        body = r.json()
        if r.status_code != 200 or body.get("code") != 200:
            raise RuntimeError("writing the song failed")
        song = body["data"]
        form.update(prompt=", ".join(x for x in (song.get("caption") or p["description"],
                                                 prof.get("style_caption")) if x),
                    lyrics=song.get("lyrics") or "", thinking="true")
        for ours, theirs in (("bpm", "bpm"), ("keyscale", "key_scale")):
            if song.get(ours):
                form[theirs] = str(song[ours])
    for k, f in (("duration_s", "audio_duration"), ("bpm", "bpm"), ("key", "key_scale")):
        if p.get(k):
            form[f] = str(p[k])
    if p.get("seed") is not None:
        form.update(use_random_seed="false", seed=str(p["seed"]))
    from app import styles
    st = styles.of_voice(row["voice_id"])
    use_style = p.get("use_style_model", True) is not False and styles.adapter_ready(st)
    files = None
    ref_id = prof.get("reference_clip_id")
    if ref_id:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(f"{cfg.RVC_URL}/clips/{row['voice_id']}/{ref_id}")
        if r.status_code == 200:
            files = {"reference_audio": ("ref.wav", r.content, "audio/wav")}
    if use_style:
        # Performed by the person's own style adapter on the style engine (layer 3).
        _set(row["id"], message="performing the song in your singing style", stage="performing")
        form.pop("thinking", None)
        first, raw = await styles.render(st["id"], form, files,
                                         float(p.get("style_strength") or 0.8))
    else:
        first, raw = await _render_default(form, files)
    _set(row["id"], message="singing it in your voice", stage="converting", progress=0.6)
    res = await _resing(row, {"key": "auto"}, raw)
    res.update({"lyrics": first.get("lyrics") or form.get("lyrics"),
                "style": first.get("prompt") or form.get("prompt"),
                "bpm": (first.get("metas") or {}).get("bpm"),
                "key": (first.get("metas") or {}).get("keyscale"),
                "seed": first.get("seed_value")})
    _set(row["id"], status="succeeded", message=None, stage="done", progress=1.0,
         result=json.dumps(res))


async def _render_default(form: dict, files: dict | None) -> tuple[dict, bytes]:
    """Render on the shared song engine (no style adapter)."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{cfg.MUSIC_URL}/release_task", data=form, files=files)
            if resp.status_code == 429:
                raise gpuqueue.Busy()
            body = resp.json()
            if resp.status_code != 200 or body.get("code") != 200:
                raise RuntimeError(body.get("error") or "the song model refused the request")
            tid = body["data"]["task_id"]
            for _ in range(400):
                await asyncio.sleep(3)
                q = (await client.post(f"{cfg.MUSIC_URL}/query_result",
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
            audio = await client.get(f"{cfg.MUSIC_URL}{first['file']}")
            audio.raise_for_status()
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        raise gpuqueue.Busy()
    return first, audio.content


async def _run_speech(row, p: dict) -> None:
    """Speak with the person's own speech as the clone reference when they gave some (accent,
    pace), else a preset speaker in their register; then RVC puts on their exact timbre."""
    prof = _profile(row["voice_id"])
    _set(row["id"], message="speaking the text", stage="speaking")
    lang = p.get("language") or "Auto"
    if str(lang).strip().lower() in ("burmese", "myanmar", "my", "mya"):
        lang = speech.BURMESE
    if str(lang).strip().lower() in ("hindi", "hi", "hin"):
        lang = speech.HINDI
    if str(lang).strip().lower() in ("sanskrit", "sa", "san"):
        raise RuntimeError("Sanskrit chant has one voice (Vāgdhenu) and cannot be sung in a "
                           "trained voice yet; use generate_speech with language=Sanskrit")
    # Burmese and Hindi go to the VoxCPM2 engine (tts-my), like generate_speech: Qwen has no
    # Burmese and speaks Hindi with an accent, which RVC would then carry into their voice.
    lang = speech._resolve_language(lang, p["text"], None)
    burmese, hindi = lang == speech.BURMESE, lang == speech.HINDI
    url = speech.engine_url(lang, "clone" if hindi else None)
    talk = db.one("SELECT * FROM voice_clips WHERE voice_id=? AND kind='speech' AND status='ok' "
                  "ORDER BY created_at DESC LIMIT 1", (row["voice_id"],))
    async with httpx.AsyncClient(timeout=600.0) as client:
        if talk is not None:
            r = await client.get(f"{cfg.RVC_URL}/clips/{row['voice_id']}/{talk['id']}")
            ref = await speech.reference_wav(r.content) if r.status_code == 200 else None
        else:
            ref = None
        if ref:
            resp = await client.post(f"{url}/clone", data={
                "text": p["text"], "language": lang, "mode": "style"},
                files={"reference_audio": ("reference.wav", ref, "audio/wav")})
        else:
            high = (prof.get("range", {}).get("mid_hz") or 200) >= 165
            speaker = (("Thiri" if high else "Kyaw") if burmese else
                       ("Meera" if high else "Arjun") if hindi else
                       ("Serena" if high else "Aiden"))
            resp = await client.post(f"{url}/speak", json={
                "text": p["text"], "speaker": speaker, "language": lang,
                "instruct": p.get("delivery") or None})
    if resp.status_code == 503:
        raise gpuqueue.Busy()
    if resp.status_code != 200:
        raise RuntimeError(f"the speech service answered HTTP {resp.status_code}")
    _set(row["id"], message="converting to your voice", stage="converting", progress=0.6)
    job = row["id"].replace("vjob_", "job_")
    data = {"job": job, "key": "auto", "content": "speech"}
    if prof.get("range", {}).get("mid_hz"):
        # Speech sits lower than singing: aim about 5 semitones under the sung median.
        data["target_mid_hz"] = str(prof["range"]["mid_hz"] * 2 ** (-5 / 12))
    res = await _rvc("POST", f"/voices/{row['voice_id']}/convert", data=data,
                     files={"file": ("speech.wav", resp.content, "audio/wav")}, timeout=900.0)
    files = await _rvc_fetch(job, ["out.mp3"], _dir("out", row["user_id"], row["id"]))
    _set(row["id"], status="succeeded", message=None, stage="done", progress=1.0,
         result=json.dumps({"files": files, "key_shift_semitones": res.get("key_shift_semitones")}))


async def _probe(raw: bytes) -> float | None:
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        f.write(raw)
        f.flush()
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
            f.name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- /v1 API
# The voices page uses these too, so a cookie works as well as a bearer token.

def _who(request: Request) -> auth.User:
    user = auth.current_user(request) or oauth.user_for_request(request)
    if user is None:
        raise HTTPException(401, "not signed in")
    return user


@router.get("/v1/voices")
async def api_list(request: Request):
    user = _who(request)
    rows = db.query("SELECT * FROM voices WHERE user_id=? ORDER BY created_at DESC", (user.id,))
    return {"items": [voice_view(r) for r in rows], "consent_text": CONSENT_TEXT,
            "consent_version": CONSENT_VERSION, "retention_note": service.keep_a_copy_note()}


@router.post("/v1/voices", status_code=201)
async def api_create(request: Request):
    user = _who(request)
    body = await request.json()
    consent = body.get("consent") or {}
    if consent.get("statement_version") not in (None, CONSENT_VERSION):
        raise HTTPException(400, "the consent statement has changed; show the current one")
    vid = create(user, body.get("name") or "", consent.get("attest") is True,
                 request.client.host if request.client else None)
    return voice_view(owned(user, vid))


@router.get("/v1/voices/{voice_id}")
async def api_get(request: Request, voice_id: str):
    return voice_view(owned(_who(request), voice_id))


@router.patch("/v1/voices/{voice_id}")
async def api_rename(request: Request, voice_id: str):
    user = _who(request)
    owned(user, voice_id)
    name = ((await request.json()).get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "give a name")
    db.run("UPDATE voices SET name=?, updated_at=? WHERE id=?", (name, db.now(), voice_id))
    return voice_view(owned(user, voice_id))


@router.delete("/v1/voices/{voice_id}", status_code=204)
async def api_delete(request: Request, voice_id: str):
    await delete_voice(_who(request), voice_id)


@router.post("/v1/voices/{voice_id}/clips", status_code=201)
async def api_add_clip(request: Request, voice_id: str, file: UploadFile = File(...),
                       kind: str = Form("auto")):
    user = _who(request)
    cid = await add_clip(user, voice_id, file.filename or "clip",
                         await file.read(MAX_UPLOAD + 1), kind)
    return clip_view(db.one("SELECT * FROM voice_clips WHERE id=?", (cid,)))


@router.get("/v1/voices/{voice_id}/clips")
async def api_clips(request: Request, voice_id: str):
    owned(_who(request), voice_id)
    return {"items": [clip_view(c) for c in _clips(voice_id)]}


@router.delete("/v1/voices/{voice_id}/clips/{clip_id}", status_code=204)
async def api_delete_clip(request: Request, voice_id: str, clip_id: str):
    await delete_clip(_who(request), voice_id, clip_id)


@router.get("/v1/voices/{voice_id}/clips/{clip_id}/audio")
async def api_clip_audio(request: Request, voice_id: str, clip_id: str):
    owned(_who(request), voice_id)
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(f"{cfg.RVC_URL}/clips/{voice_id}/{clip_id}")
    if r.status_code != 200:
        raise HTTPException(404, "not cleaned yet")
    from fastapi.responses import Response
    return Response(r.content, media_type="audio/wav")


@router.patch("/v1/voices/{voice_id}/clips/{clip_id}")
async def api_set_clip_kind(request: Request, voice_id: str, clip_id: str):
    """The person corrects what a recording is (singing or speech)."""
    v = owned(_who(request), voice_id)
    if v["status"] == "training":
        raise HTTPException(409, "the voice is training; change recordings when it has finished")
    kind = ((await request.json()) or {}).get("kind")
    if kind not in ("singing", "speech"):
        raise HTTPException(400, "kind must be singing or speech")
    c = db.one("SELECT * FROM voice_clips WHERE id=? AND voice_id=?", (clip_id, voice_id))
    if c is None:
        raise HTTPException(404, "no such clip")
    db.run("UPDATE voice_clips SET kind=?, updated_at=? WHERE id=?", (kind, db.now(), clip_id))
    return clip_view(db.one("SELECT * FROM voice_clips WHERE id=?", (clip_id,)))


@router.post("/v1/voices/{voice_id}/train", status_code=202)
async def api_train(request: Request, voice_id: str):
    user = _who(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    jid = start_training(user, voice_id, (body or {}).get("quality") or "standard")
    return job_view(owned_job(user, jid))


@router.get("/v1/voices/{voice_id}/profile")
async def api_profile(request: Request, voice_id: str):
    v = owned(_who(request), voice_id)
    if not v["profile"]:
        raise HTTPException(404, "the voice is not trained yet")
    return json.loads(v["profile"])


async def _source(request: Request, user: auth.User, form) -> bytes | None:
    up = form.get("file")
    if up is not None and getattr(up, "filename", ""):
        return await up.read(MAX_UPLOAD + 1)
    if form.get("music_id"):
        return music.source_from_music(user, str(form["music_id"]))
    return None


@router.post("/v1/voices/{voice_id}/resing", status_code=202)
async def api_resing(request: Request, voice_id: str):
    user = _who(request)
    form = await request.form()
    params = {"key": str(form.get("key") or "auto"),
              "keep_backing_vocals": str(form.get("keep_backing_vocals", "true")).lower()
              not in ("false", "0", "off"),
              "reverb": float(form.get("reverb") or 0.2), "title": str(form.get("title") or "")}
    for k in ("vocal_db", "music_db", "backing_db"):
        if form.get(k) not in (None, ""):
            params[k] = int(form[k])
    jid = await use(user, voice_id, "resing", params, await _source(request, user, form))
    return job_view(owned_job(user, jid))


@router.post("/v1/voices/{voice_id}/songs", status_code=202)
async def api_song(request: Request, voice_id: str):
    user = _who(request)
    body = await request.json()
    params = {k: body.get(k) for k in ("description", "lyrics", "style", "language", "duration_s",
                                       "bpm", "key", "seed", "use_style_model", "style_strength")
              if body.get(k) not in (None, "")}
    jid = await use(user, voice_id, "song", params)
    return job_view(owned_job(user, jid))


@router.post("/v1/voices/{voice_id}/speech", status_code=202)
async def api_speech(request: Request, voice_id: str):
    user = _who(request)
    body = await request.json()
    params = {k: body.get(k) for k in ("text", "language", "delivery") if body.get(k)}
    jid = await use(user, voice_id, "speech", params)
    return job_view(owned_job(user, jid))


@router.get("/v1/jobs")
async def api_jobs(request: Request, voice_id: str | None = None, kind: str | None = None):
    user = _who(request)
    sql, args = "SELECT * FROM voice_jobs WHERE user_id=?", [user.id]
    if voice_id:
        sql += " AND voice_id=?"
        args.append(voice_id)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = db.query(sql + " ORDER BY created_at DESC LIMIT 100", tuple(args))
    return {"items": [job_view(r) for r in rows]}


@router.get("/v1/jobs/{job_id}")
async def api_job(request: Request, job_id: str):
    return job_view(owned_job(_who(request), job_id))


@router.post("/v1/jobs/{job_id}/cancel")
async def api_cancel(request: Request, job_id: str):
    user = _who(request)
    j = owned_job(user, job_id)
    if j["status"] == "queued":
        _set(job_id, status="failed", error="cancelled", message=None)
        if j["kind"] == "train":
            db.run("UPDATE voices SET status='failed', last_error='training was cancelled' "
                   "WHERE id=?", (j["voice_id"],))
    elif j["status"] == "generating":
        raise HTTPException(409, "it is on the GPU right now; it can be cancelled when this "
                                 "step finishes")
    return job_view(owned_job(user, job_id))


@router.get("/v1/jobs/{job_id}/files/{name}")
async def api_job_file(request: Request, job_id: str, name: str, download: int = 0):
    j = owned_job(_who(request), job_id)
    p = _dir("out", j["user_id"], j["id"], Path(name).name)
    if not p.exists():
        raise HTTPException(404, "no such file (it may have been deleted after 90 days)")
    return FileResponse(p, media_type="audio/mpeg",
                        filename=f"{job_id}-{p.name}" if download else None)


def signed_file(token: str):
    """For /a/<job~name>/<exp>/<sig>/audio.mp3 links handed out to assistants."""
    job_id, _, name = token.partition("~")
    j = db.one("SELECT * FROM voice_jobs WHERE id=?", (job_id,))
    p = _dir("out", j["user_id"], job_id, Path(name).name) if j else None
    if p is None or not p.exists():
        raise HTTPException(404, "this link is not valid, or it has expired")
    return FileResponse(p, media_type="audio/mpeg")
