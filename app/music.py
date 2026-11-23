"""Songs made on the GPU with ACE-Step 1.5, through the studio's own `music` container.

What a person can ask for (`task`):
- describe  -- one sentence; the 4B composer model writes the lyrics, style and
               structure itself ("an upbeat Hindi wedding song about the monsoon").
- custom    -- their own lyrics (with [Verse]/[Chorus] tags) plus a style, or
               instrumental. An optional reference track steers the sound.
- cover     -- re-perform a source track in a new style, optionally with new
               lyrics; `strength` says how closely to follow the source.
- repaint   -- regenerate one stretch (start..end seconds) of a source track.
- extract   -- pull one stem (vocals, drums, ...) out of a source track.
- add_layer -- generate a new stem (e.g. strings) that fits a source track.
- extend    -- make a track longer: a repaint whose window runs past its end.
- accompany -- build the named instruments around a single track (upstream
               "complete"; it never changes the length).

describe/custom/cover/repaint/extend run on XL turbo (8 steps, the fast
high-quality model); extract/add_layer/accompany exist only on XL base, which
the container also holds. The source or reference audio waits on disk while
the job queues.

How the source tasks must be configured (each line was a garbage-output cause):
- Caption and lyrics reach the DiT only from us; the LM is skipped for these
  tasks. Empty lyrics on a sung source give mumbling, so a studio song's own
  lyrics are reused and an upload's are transcribed (CPU Whisper).
- audio_cover_strength is the fraction of steps that see the source; below 1
  the rest run as plain text-to-music. So it is 1.0 except on cover.
- Repaint needs chunk_mask_mode=explicit, or the whole song is regenerated.
- track_classes must arrive as a list (repeated form keys), not a JSON string.
- The engine silently answers with turbo if XL base is not loaded; run()
  refuses a result from a model other than the one asked for.

Requests are queued by `app.gpuqueue`, which starts them only when WIS is idle
and enough VRAM is free; an out-of-memory answer puts the job back.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
from fastapi import HTTPException

from app import auth, db, gpuqueue, service
from app.config import cfg

TERMINAL = {"succeeded", "failed"}
TASKS = {
    "describe": "Describe it — the model writes lyrics and style",
    "custom": "Your lyrics and style (or instrumental)",
    "cover": "Cover a track in a new style",
    "repaint": "Redo one section of a track",
    "extract": "Pull out one stem",
    "add_layer": "Add a new instrument layer",
    "extend": "Make a track longer",
    "accompany": "Build a band around a single track",
}
SOURCE_TASKS = {"cover", "repaint", "extract", "add_layer", "extend", "accompany"}
BASE_TASKS = {"extract": "extract", "add_layer": "lego", "accompany": "complete"}
# Sources whose words matter; for these an upload with no lyrics is transcribed.
SUNG_TASKS = {"cover", "repaint", "extend"}
VOCAL_TRACKS = {"vocals", "backing_vocals"}
TRACKS = ["vocals", "backing_vocals", "drums", "bass", "guitar", "keyboard", "strings",
          "synth", "percussion", "brass", "woodwinds", "fx"]
LANGUAGES = {"en": "English", "hi": "Hindi", "zh": "Chinese", "ja": "Japanese",
             "ko": "Korean", "es": "Spanish", "fr": "French", "de": "German",
             "pt": "Portuguese", "it": "Italian", "ru": "Russian", "ar": "Arabic",
             "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
             "pa": "Punjabi", "ur": "Urdu", "sa": "Sanskrit", "unknown": "Auto"}
TURBO = "acestep-v15-xl-turbo"
BASE = "acestep-v15-xl-base"
MAX_LYRICS = 4000
MAX_PROMPT = 1000
MIN_S, MAX_S = 10, 360
MIN_ADD_S, MAX_ADD_S = 10, 120
MIN_REPAINT_S, MAX_REPAINT_S = 3, 90
JOB_TIMEOUT_S = 20 * 60


def _input_path(music_id: str, which: str) -> Path:
    return cfg.music_dir / "inputs" / f"{music_id}-{which}.wav"


# --------------------------------------------------------------------------- reads

def owned(user: auth.User, music_id: str):
    row = db.one("SELECT * FROM music WHERE id=?", (str(music_id or ""),))
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "no such song")
    return row


def rate_limited(user_id: str) -> bool:
    if auth.is_unlimited(user_id):
        return False
    row = db.one("SELECT COUNT(*) AS n FROM music WHERE user_id=? AND created_at > ? "
                 "AND status != 'failed'", (user_id, db.now() - 3600))
    return bool(row and row["n"] >= cfg.USER_MUSIC_PER_HOUR)


def source_meta(user: auth.User, music_id: str | None) -> dict:
    """What the studio stored about a song it made: its style and lyrics."""
    if not music_id:
        return {}
    return meta(owned(user, music_id))


def source_from_music(user: auth.User, music_id: str) -> bytes:
    row = owned(user, music_id)
    if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(400, f"song {row['id']} is not finished ({row['status']})")
    return Path(row["path"]).read_bytes()


async def health() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{cfg.MUSIC_URL}/health")
        resp.raise_for_status()
        return resp.json().get("data") or {}


def meta(row) -> dict:
    try:
        return json.loads(row["result_meta"] or "{}")
    except ValueError:
        return {}


# --------------------------------------------------------------------------- audio

async def _to_wav(raw: bytes, label: str, limit_s: int = MAX_S) -> bytes:
    """Any audio (mp3, m4a, wav, video soundtrack…) -> stereo 48 kHz wav."""
    from app.speech import _ffmpeg
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out.wav"
        src.write_bytes(raw)
        try:
            await _ffmpeg(["-i", str(src), "-vn", "-t", str(limit_s), "-ac", "2", "-ar", "48000",
                           str(dst)])
        except RuntimeError:
            raise HTTPException(400, f"the {label} is not readable audio")
        return dst.read_bytes()


def _wav_seconds(wav: bytes) -> float:
    import io
    import wave
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes() / float(w.getframerate())


async def transcribe_source(wav: bytes) -> str:
    """The sung words of a track, one line per phrase (CPU Whisper in the tts service)."""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(f"{cfg.TTS_URL}/transcribe_song",
                                     files={"audio": ("track.wav", wav, "audio/wav")})
    except httpx.HTTPError as exc:
        raise HTTPException(502, "could not read the lyrics from the track (the speech "
                                 f"service is unreachable: {type(exc).__name__}); type them in")
    if resp.status_code != 200:
        raise HTTPException(400, "could not read the lyrics from the track; type them in")
    return (resp.json().get("text") or "").strip()


# --------------------------------------------------------------------------- writes

def _num(value, name: str, lo: float, hi: float, integer: bool = False):
    if value in (None, ""):
        return None
    try:
        v = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a number")
    if not lo <= v <= hi:
        raise HTTPException(400, f"{name} must be between {lo:g} and {hi:g}")
    return v


async def create(user: auth.User, *, task: str = "custom", description: str = "",
                 style: str = "", lyrics: str = "", instrumental: bool = False,
                 language: str | None = None, duration=None, bpm=None, key: str = "",
                 time_signature: str = "", seed=None, strength=None, start=None, end=None,
                 track: str = "", instruments: list[str] | None = None,
                 add_seconds=None, title: str = "", source: bytes | None = None,
                 source_music_id: str | None = None,
                 reference: bytes | None = None) -> str:
    task = (task or "custom").strip()
    if task not in TASKS:
        raise HTTPException(400, f"task must be one of {', '.join(TASKS)}")
    description, style, lyrics = (description or "").strip(), (style or "").strip(), \
        (lyrics or "").strip()
    if len(style) > MAX_PROMPT or len(description) > MAX_PROMPT:
        raise HTTPException(400, f"the style/description is over {MAX_PROMPT} characters")
    if len(lyrics) > MAX_LYRICS:
        raise HTTPException(400, f"the lyrics are over {MAX_LYRICS} characters")
    if task == "describe" and not description:
        raise HTTPException(400, "describe the song you want — e.g. \"a gentle lullaby in "
                                 "Hindi with soft piano\"")
    if task == "custom" and not (style or lyrics or instrumental):
        raise HTTPException(400, "give a style, lyrics, or both (or ask for an instrumental)")
    if task in SOURCE_TASKS and source is None:
        raise HTTPException(400, f"{task} works on an existing track — give a source song")
    if task not in SOURCE_TASKS and source is not None:
        raise HTTPException(400, "a source track is only used by cover, repaint, extract, "
                                 "add_layer and extend; for a style hint use the reference")
    if task in ("extract", "add_layer") and track not in TRACKS:
        raise HTTPException(400, f"choose a track: {', '.join(TRACKS)}")
    if task == "cover" and not style:
        raise HTTPException(400, "describe the new sound for the cover, e.g. \"jazz, smoky "
                                 "female vocal, upright bass, brushed drums\"")
    if task == "add_layer" and not style:
        raise HTTPException(400, f"describe the new {track.replace('_', ' ')} part, e.g. "
                                 "\"warm legato strings following the chords\"")
    if task == "accompany" and not style:
        raise HTTPException(400, "describe the arrangement you want around the track")
    instruments = [i for i in (instruments or []) if i]
    if any(i not in TRACKS for i in instruments):
        raise HTTPException(400, f"instruments must be from: {', '.join(TRACKS)}")
    if task == "accompany" and not instruments:
        raise HTTPException(400, "choose the instruments to add")
    language = language or "unknown"
    if language not in LANGUAGES:
        # Accept the language's name too ("Hindi").
        language = next((k for k, v in LANGUAGES.items()
                         if v.lower() == str(language).lower()), None)
        if language is None:
            raise HTTPException(400, f"language must be one of {', '.join(LANGUAGES)}")
    duration = _num(duration, "duration", MIN_S, MAX_S)
    bpm = _num(bpm, "bpm", 30, 300, integer=True)
    seed = _num(seed, "seed", 0, 2 ** 31 - 1, integer=True)
    strength = _num(strength, "strength", 0.0, 1.0)
    start, end = _num(start, "start", 0, MAX_S), _num(end, "end", 0, MAX_S)
    if task == "repaint":
        if start is None or end is None or end <= start:
            raise HTTPException(400, "repaint needs start and end seconds, end after start")
        if not MIN_REPAINT_S <= end - start <= MAX_REPAINT_S:
            raise HTTPException(400, f"repaint a stretch of {MIN_REPAINT_S}-{MAX_REPAINT_S} "
                                     "seconds; for longer changes use a cover")
    if task == "extend":
        add_seconds = _num(add_seconds, "add_seconds", MIN_ADD_S, MAX_ADD_S) or 30.0
    if rate_limited(user.id):
        raise HTTPException(429, f"you have made {cfg.USER_MUSIC_PER_HOUR} songs in the last "
                                 "hour, which is the limit for this account")
    src_wav = await _to_wav(source, "source track") if source is not None else None
    ref_wav = await _to_wav(reference, "reference track", 60) if reference is not None else None

    src_seconds = _wav_seconds(src_wav) if src_wav is not None else None
    if task == "repaint" and end > src_seconds + 0.5:
        raise HTTPException(400, f"the track is only {src_seconds:.0f} s long; to add to its "
                                 "end, use extend")
    if task == "extend" and src_seconds + add_seconds > MAX_S:
        raise HTTPException(400, f"the result would be over {MAX_S} s")
    src = source_meta(user, source_music_id) if src_wav is not None else {}
    src_style, src_lyrics = (src.get("style") or "").strip(), (src.get("lyrics") or "").strip()
    if (task in SUNG_TASKS and src_wav is not None and not src_lyrics and not lyrics
            and not instrumental):
        # An upload: the model only knows the words we give it.
        src_lyrics = await transcribe_source(src_wav) or "[Instrumental]"
    if task in ("repaint", "extend") and not (style or src_style):
        raise HTTPException(400, "describe the music (genre, instruments, voice) so the new "
                                 "part matches")

    music_id = db.new_id("mus")
    for which, data in (("src", src_wav), ("ref", ref_wav)):
        if data is not None:
            _input_path(music_id, which).parent.mkdir(parents=True, exist_ok=True)
            _input_path(music_id, which).write_bytes(data)
    params = {"description": description, "style": style, "lyrics": lyrics,
              "instrumental": bool(instrumental), "language": language, "duration": duration,
              "bpm": bpm, "key": (key or "").strip()[:20],
              "time_signature": (time_signature or "").strip()[:4], "seed": seed,
              "strength": strength, "start": start, "end": end, "track": track,
              "instruments": instruments, "add_seconds": add_seconds,
              "src_seconds": src_seconds, "src_style": src_style, "src_lyrics": src_lyrics,
              "has_source": src_wav is not None, "has_reference": ref_wav is not None}
    title = (title or "").strip()[:120] or (description or style or lyrics.split("\n")[0]
                                           or TASKS[task])[:80]
    db.run("INSERT INTO music (id,user_id,task,title,params,status,message,created_at,"
           "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (music_id, user.id, task, title, json.dumps(params), "queued", "queued",
            db.now(), db.now()))
    gpuqueue.kick()
    return music_id


def _model(task: str) -> str:
    return BASE if task in BASE_TASKS else TURBO


def _form(task: str, p: dict) -> dict:
    """The ACE-Step /release_task fields for one job."""
    if task in SOURCE_TASKS:
        return _source_form(task, p)
    form = {"audio_format": "mp3", "batch_size": "1", "vocal_language": p["language"],
            "thinking": "true", "model": TURBO}
    if task == "describe":
        query = p["description"] + (" (instrumental, no vocals)" if p["instrumental"] else "")
        form.update(sample_mode="true", sample_query=query)
    else:
        form["prompt"] = p["style"]
        lyrics = "[Instrumental]" if p["instrumental"] else p["lyrics"]
        if lyrics:
            form["lyrics"] = lyrics
    for ours, theirs in (("duration", "audio_duration"), ("bpm", "bpm"), ("key", "key_scale"),
                         ("time_signature", "time_signature")):
        if p.get(ours) not in (None, ""):
            form[theirs] = str(p[ours])
    if p["seed"] is not None:
        form.update(use_random_seed="false", seed=str(p["seed"]))
    return form


def _source_form(task: str, p: dict) -> dict:
    """A job that works on a source track. Settings follow upstream's own UI per task
    (acestep/ui/gradio/events/generation/*); see the module docstring for why."""
    form = {"audio_format": "mp3", "batch_size": "1", "model": _model(task),
            "vocal_language": p["language"], "thinking": "false", "use_cot_caption": "false",
            "use_cot_language": "false", "shift": "3.0",
            "audio_cover_strength": "1.0", "cover_noise_strength": "0.0"}
    if task in BASE_TASKS:
        # DCW is left unset: the API does not read it from a form, and the
        # engine already turns it off for base (it garbles non-turbo output).
        form.update(task_type=BASE_TASKS[task], inference_steps="50", guidance_scale="7.0")
    else:
        form["inference_steps"] = "8"
    style = p["style"] or p.get("src_style", "")
    own = "[Instrumental]" if p["instrumental"] else p["lyrics"]
    src_lyrics = p.get("src_lyrics", "")
    if task == "cover":
        form.update(task_type="cover", prompt=p["style"], lyrics=own or src_lyrics,
                    audio_cover_strength=str(0.5 if p["strength"] is None else p["strength"]))
    elif task == "repaint":
        form.update(task_type="repaint", prompt=style, lyrics=own or src_lyrics,
                    repainting_start=str(p["start"]), repainting_end=str(p["end"]),
                    chunk_mask_mode="explicit", repaint_mode="balanced")
    elif task == "extend":
        lyrics = src_lyrics if not p["lyrics"] else f"{src_lyrics}\n\n{p['lyrics']}".strip()
        if p["instrumental"]:
            lyrics = "[Instrumental]"
        src_s = p["src_seconds"]
        form.update(task_type="repaint", prompt=style, lyrics=lyrics,
                    repainting_start=f"{src_s:.2f}",
                    repainting_end=f"{src_s + p['add_seconds']:.2f}",
                    chunk_mask_mode="explicit", repaint_mode="balanced")
    elif task == "extract":
        # Upstream fills the caption with the bare track name and blanks the lyrics.
        form.update(prompt=p["track"], lyrics="", track_name=p["track"],
                    vocal_language="unknown")
    elif task == "add_layer":
        sung = p["track"] in VOCAL_TRACKS and p["lyrics"]
        form.update(prompt=p["style"], lyrics=p["lyrics"] if sung else "[Instrumental]",
                    track_name=p["track"])
        if not sung:
            form["vocal_language"] = "unknown"
    elif task == "accompany":
        # A list: httpx sends repeated keys, which the engine reads as one list.
        form.update(prompt=p["style"], lyrics=own or src_lyrics,
                    track_classes=list(p["instruments"]))
    for ours, theirs in (("bpm", "bpm"), ("key", "key_scale"),
                         ("time_signature", "time_signature")):
        if p.get(ours) not in (None, ""):
            form[theirs] = str(p[ours])
    if p["seed"] is not None:
        form.update(use_random_seed="false", seed=str(p["seed"]))
    return form


async def _compose_first(client: httpx.AsyncClient, p: dict, form: dict) -> dict:
    query = p["description"]
    if p.get("duration"):
        query += f" (about {int(p['duration'])} seconds long)"
    resp = await client.post(f"{cfg.MUSIC_URL}/v1/create_sample", json={
        "query": query, "instrumental": bool(p["instrumental"]),
        "vocal_language": p["language"]}, timeout=300.0)
    body = resp.json()
    if resp.status_code != 200 or body.get("code") != 200:
        detail = str(body.get("error") or body.get("detail") or resp.status_code)
        if _oom(detail):
            raise gpuqueue.Busy()
        raise RuntimeError(f"writing the song failed: {detail[:300]}")
    song = body["data"]
    form = {k: v for k, v in form.items() if k not in ("sample_mode", "sample_query")}
    form["prompt"] = song.get("caption") or p["description"]
    form["lyrics"] = "[Instrumental]" if p["instrumental"] else (song.get("lyrics") or "")
    if song.get("vocal_language") and p["language"] == "unknown":
        form["vocal_language"] = song["vocal_language"]
    for ours, theirs, composed in (("bpm", "bpm", "bpm"), ("key", "key_scale", "keyscale"),
                                   ("time_signature", "time_signature", "timesignature")):
        if p.get(ours) in (None, "") and song.get(composed):
            form[theirs] = str(song[composed])
    return form


def _oom(text: str) -> bool:
    t = (text or "").lower()
    return "out of memory" in t or "cuda oom" in t or "outofmemory" in t


async def run(row) -> None:
    """Called by the GPU queue when the card is free. Raises gpuqueue.Busy on a full GPU."""
    music_id, task, p = row["id"], row["task"], json.loads(row["params"])
    files = {}
    if p.get("has_source"):
        files["src_audio"] = ("source.wav", _input_path(music_id, "src").read_bytes(),
                              "audio/wav")
    if p.get("has_reference"):
        files["reference_audio"] = ("reference.wav", _input_path(music_id, "ref").read_bytes(),
                                    "audio/wav")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            form = _form(task, p)
            if task == "describe" and any(p.get(k) not in (None, "") for k in
                                          ("duration", "bpm", "key", "time_signature")):
                # In one-shot describe mode the composer's own length/tempo/key
                # overwrite the person's. So: let it write the song first, then
                # render that as a custom song with the person's settings.
                form = await _compose_first(client, p, form)
            resp = await client.post(f"{cfg.MUSIC_URL}/release_task", data=form,
                                     files=files or None)
            if resp.status_code == 429:
                raise gpuqueue.Busy()
            body = resp.json()
            if resp.status_code != 200 or body.get("code") != 200:
                raise RuntimeError(body.get("error") or body.get("detail")
                                   or f"the music service answered HTTP {resp.status_code}")
            task_id = body["data"]["task_id"]
            loop = asyncio.get_running_loop()
            deadline = loop.time() + JOB_TIMEOUT_S
            while True:
                await asyncio.sleep(3)
                q = (await client.post(f"{cfg.MUSIC_URL}/query_result",
                                       json={"task_id_list": [task_id]})).json()
                item = (q.get("data") or [{}])[0]
                if item.get("status") == 1:
                    break
                if item.get("status") == 2:
                    detail = str(item.get("result") or item.get("error") or "")
                    if _oom(detail):
                        raise gpuqueue.Busy()
                    raise RuntimeError(f"the song failed: {detail[:300] or 'unknown error'}")
                if loop.time() > deadline:
                    raise RuntimeError(f"the song took longer than {JOB_TIMEOUT_S // 60} minutes")
            results = json.loads(item["result"]) if isinstance(item["result"], str) \
                else item["result"]
            first = results[0]
            used = first.get("dit_model")
            if used and used != form["model"]:
                # The engine answers with its default model when the one asked for
                # is not loaded; turbo cannot do stem tasks and returns noise.
                raise RuntimeError(f"the music engine used {used} instead of {form['model']}; "
                                   "stem tasks need the base model, which is not loaded")
            audio = await client.get(f"{cfg.MUSIC_URL}{first['file']}")
            audio.raise_for_status()
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        # The engine restarts itself after idling (see music/studio_entry.py);
        # a job that meets it mid-restart goes back in the queue.
        raise gpuqueue.Busy()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"the music service is unreachable ({type(exc).__name__})")

    dest = cfg.music_dir / row["user_id"]
    dest.mkdir(parents=True, exist_ok=True)
    path, part = dest / f"{music_id}.mp3", dest / f"{music_id}.part"
    part.write_bytes(audio.content)
    os.replace(part, path)
    metas = first.get("metas") or {}
    result = {"lyrics": first.get("lyrics") or "", "style": first.get("prompt") or "",
              "bpm": metas.get("bpm"), "key": metas.get("keyscale"),
              "time_signature": metas.get("timesignature"),
              "duration_s": metas.get("duration"), "seed": first.get("seed_value"),
              "model": first.get("dit_model"), "composer": first.get("lm_model")}
    duration = await _probe_seconds(path)
    db.run("UPDATE music SET status='succeeded', path=?, duration_s=?, result_meta=?, "
           "message=NULL, updated_at=? WHERE id=?",
           (str(path), duration, json.dumps(result), db.now(), music_id))
    for which in ("src", "ref"):
        _input_path(music_id, which).unlink(missing_ok=True)


async def _probe_seconds(path: Path) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
        str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        return round(float(out.decode().strip()), 1)
    except ValueError:
        return None


async def wait(music_id: str, seconds: float, poll: float = 2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        row = db.one("SELECT * FROM music WHERE id=?", (music_id,))
        if row["status"] in TERMINAL or loop.time() + poll > deadline:
            return row
        await asyncio.sleep(poll)


def add_to_project(user: auth.User, music_id: str, project) -> int:
    row = owned(user, music_id)
    if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(400, f"song {row['id']} is not finished ({row['status']}); call "
                                 "get_music until it has succeeded")
    return service.add_files(project, [(f"{row['id']}.mp3", "audio/mpeg",
                                        Path(row["path"]).read_bytes())])


def delete(user: auth.User, music_id: str) -> None:
    row = owned(user, music_id)
    if row["status"] == "generating":
        raise HTTPException(409, "this song is being generated; delete it once it finishes")
    _remove(row)


def _remove(row) -> None:
    for which in ("src", "ref"):
        _input_path(row["id"], which).unlink(missing_ok=True)
    if row["path"]:
        Path(row["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM music WHERE id=?", (row["id"],))


def delete_for_user(user_id: str) -> None:
    for row in db.query("SELECT * FROM music WHERE user_id=?", (user_id,)):
        _remove(row)


def purge_older_than(cutoff: float) -> None:
    for row in db.query("SELECT * FROM music WHERE updated_at < ? AND status != 'generating'",
                        (cutoff,)):
        _remove(row)
