"""Speech made on the GPU with Qwen3-TTS, through the studio's own `tts` container.

Two ways to get a voice:
- a preset speaker (Qwen3-TTS CustomVoice: nine voices, plus an optional style
  instruction such as "cheerful, a little fast"). This is the default.
- a clone of a reference clip the caller supplies (Qwen3-TTS Base). With the
  clip's transcript the likeness is close; without it only the speaker
  embedding is used.

Burmese goes to a second engine, VoxCPM2 in the `tts-my` container (Qwen3-TTS
has no Burmese). Same contract; it has its own four preset speakers
(BURMESE_SPEAKERS) and clones from the audio alone. `_resolve_language` holds
the routing rule: Burmese script in the text, or a Burmese speaker, means Burmese.

Hindi (added 2026-11-21) has BOTH engines, chosen by speaker: the nine Qwen
voices speak it with a foreign accent (2-10% character errors by Whisper on two test
sentences; Qwen has no Hindi tag, so it is sent as Auto), and four native Hindi
presets on VoxCPM2 (HINDI_SPEAKERS, 0-6%) are the default. Hindi clones go to VoxCPM2.
`uses_voxcpm` is the one place that decides which engine a clip runs on.

Sanskrit (added 2026-11-21) is CHANTED, not spoken: Vāgdhenu (~/vagdhenu, IISc,
Apache-2.0), a metre-aware śloka-to-chant model with one voice, "Vagdhenu". It is
never auto-detected (Hindi and Sanskrit share Devanagari): language=Sanskrit or
speaker=Vagdhenu chooses it. The metre (chandas) is detected per śloka unless
given; text with several ślokas (each ending in ॥) is chanted one at a time and
joined. No cloning and no `instruct`: the model has neither.

Requests are queued, not refused: `create` saves the row as `queued` and
`app.gpuqueue` runs it when the card is free (WIS idle, enough VRAM). The tts
container never pushes WIS out; if it still runs out of memory it answers 503
and the clip goes back in the queue.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from app import auth, db, gpuqueue, service
from app.config import cfg

TERMINAL = {"succeeded", "failed"}
# The reference clip waits on disk while the job is queued.
def _ref_path(speech_id: str) -> Path:
    return cfg.speech_dir / "refs" / f"{speech_id}.wav"

MAX_TEXT = 1200
MAX_INSTRUCT = 500
SPEAKERS = {
    "Vivian": "female, Chinese-native, bright young voice",
    "Serena": "female, Chinese-native, warm and gentle",
    "Ono_Anna": "female, Japanese-native, light and playful",
    "Sohee": "female, Korean-native, warm and emotional",
    "Ryan": "male, English-native, dynamic and energetic",
    "Aiden": "male, American English, sunny and clear",
    "Uncle_Fu": "male, Chinese-native, low and mellow, older",
    "Dylan": "male, Beijing Chinese, youthful and natural",
    "Eric": "male, Sichuan Chinese, lively and slightly husky",
}
DEFAULT_SPEAKER = "Ryan"
# Burmese (VoxCPM2, tts-my). Fixed reference clips made with voice design on
# 2026-11-20; they live in tts-models/voxcpm/presets with these same names.
BURMESE_SPEAKERS = {
    "Thiri": "female, Burmese-native, warm and gentle, calm storyteller",
    "Nandar": "female, Burmese-native, bright and cheerful, lively",
    "Kyaw": "male, Burmese-native, clear and confident, friendly",
    "U_Ba": "male, Burmese-native, deep and slow, older and wise",
}
DEFAULT_BURMESE_SPEAKER = "Thiri"
# Hindi (VoxCPM2, tts-my). Same method, 2026-11-21; best of three takes by Whisper.
HINDI_SPEAKERS = {
    "Meera": "female, Hindi-native, warm and gentle, calm storyteller",
    "Anaya": "female, Hindi-native, bright and cheerful, lively",
    "Arjun": "male, Hindi-native, clear and confident, friendly",
    "Vikram": "male, Hindi-native, deep and slow, older and wise",
}
DEFAULT_HINDI_SPEAKER = "Meera"
# Sanskrit śloka chant (Vāgdhenu): one voice, the metre carries the melody.
SANSKRIT_SPEAKERS = {
    "Vagdhenu": "Sanskrit śloka CHANT (traditional metre-aware recitation, Vāgdhenu); "
                "Sanskrit only",
}
QWEN_SPEAKERS = dict(SPEAKERS)
SPEAKERS = {**SPEAKERS, **BURMESE_SPEAKERS, **HINDI_SPEAKERS, **SANSKRIT_SPEAKERS}
BURMESE = "Burmese"
HINDI = "Hindi"
SANSKRIT = "Sanskrit"
METERS = ["auto", "anuṣṭubh", "pramāṇikā", "vasantatilakā", "upajāti", "indravajrā",
          "upendravajrā", "vaṃśastha", "rathoddhatā", "śālinī", "indravaṃśā",
          "drutavilambita", "bhujaṅgaprayāta", "mālinī", "śārdūlavikrīḍita", "sragdharā",
          "gadya"]
LANGUAGES = ["Auto", "English", "Chinese", "Japanese", "Korean", "German", "French",
             "Russian", "Portuguese", "Spanish", "Italian", HINDI, BURMESE, SANSKRIT]


def _has_burmese(text: str) -> bool:
    """True when most letters are Myanmar script (U+1000-U+109F, extensions included)."""
    letters = [c for c in text if c.isalpha() or "\u1000" <= c <= "\u109f"]
    mm = sum(1 for c in letters if "\u1000" <= c <= "\u109f" or "\uaa60" <= c <= "\uaa7f"
             or "\ua9e0" <= c <= "\ua9ff")
    return bool(letters) and mm / len(letters) >= 0.5


def _has_hindi(text: str) -> bool:
    """True when most letters are Devanagari (U+0900-U+097F, extended U+A8E0-U+A8FF)."""
    letters = [c for c in text if c.isalpha() or "\u0900" <= c <= "\u097f"]
    hi = sum(1 for c in letters if "\u0900" <= c <= "\u097f" or "\ua8e0" <= c <= "\ua8ff")
    return bool(letters) and hi / len(letters) >= 0.5


def _is(speaker, group) -> bool:
    return bool(speaker) and any(s.lower() == str(speaker).lower() for s in group)


def _resolve_language(language: str, text: str, speaker: str | None) -> str:
    """Auto becomes Burmese for Burmese text or a Burmese speaker (Qwen has no Burmese,
    so it would otherwise mumble), and Hindi for Devanagari text or a Hindi speaker."""
    if language == "Auto":
        if _is(speaker, SANSKRIT_SPEAKERS):
            return SANSKRIT
        if _is(speaker, BURMESE_SPEAKERS) or _has_burmese(text):
            return BURMESE
        if _is(speaker, HINDI_SPEAKERS) or _has_hindi(text):
            return HINDI
    return language


def uses_voxcpm(language: str, voice: str | None = None) -> bool:
    """VoxCPM2 (tts-my) for Burmese, the Hindi presets and Hindi clones; Qwen otherwise."""
    if language == BURMESE or _is(voice, HINDI_SPEAKERS):
        return True
    return language == HINDI and voice == "clone"


def engine_url(language: str, voice: str | None = None) -> str:
    return cfg.TTS_MY_URL if uses_voxcpm(language, voice) else cfg.TTS_URL


def engine_language(language: str, voice: str | None = None) -> str:
    """The language tag the engine is sent. Qwen has no Hindi tag; Auto reads it."""
    if language == HINDI and not uses_voxcpm(language, voice):
        return "Auto"
    return language

# --------------------------------------------------------------------------- reads

def owned(user: auth.User, speech_id: str):
    row = db.one("SELECT * FROM speech WHERE id=?", (str(speech_id or ""),))
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "no such speech clip")
    return row


def rate_limited(user_id: str, adding: int = 1) -> bool:
    if auth.is_unlimited(user_id):
        return False
    # Failed clips don't count: they made nothing.
    # Failed clips and character voice samples (voice 'design', made automatically) don't count.
    row = db.one("SELECT COUNT(*) AS n FROM speech WHERE user_id=? AND created_at > ? "
                 "AND status != 'failed' AND voice != 'design'", (user_id, db.now() - 3600))
    return bool(row and row["n"] + adding > cfg.USER_SPEECH_PER_HOUR)


async def health(url: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{url or cfg.TTS_URL}/healthz")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- audio

async def _ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError((err or b"").decode(errors="replace")[-300:] or "ffmpeg failed")


async def reference_wav(raw: bytes) -> bytes:
    """Any audio a caller gives (mp3, m4a, wav…) -> mono 24 kHz wav, at most 30 s."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out.wav"
        src.write_bytes(raw)
        try:
            await _ffmpeg(["-i", str(src), "-t", "30", "-ac", "1", "-ar", "24000", str(dst)])
        except RuntimeError:
            raise HTTPException(400, "the reference clip is not readable audio")
        return dst.read_bytes()


# --------------------------------------------------------------------------- writes

MATCHES = {"style": "Copy the speaking style — accent, pace and delivery (needs the transcript; "
                    "filled in automatically if missing)",
           "timbre": "Voice colour only — the model speaks in its own way"}
MAX_TAKES = 3
# Measured on a distinctive-style English reference: 0.7 kept pitch, pitch
# movement and pace closest; 0.3 flattened the delivery and rushed it.
DEFAULT_CLONE_TEMPERATURE = 0.7


async def transcribe(raw: bytes) -> dict:
    """The words in a reference clip, after the same clean-up a clone applies."""
    wav = await reference_wav(raw)
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{cfg.TTS_URL}/transcribe",
                                     files={"audio": ("reference.wav", wav, "audio/wav")})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"the speech service is unreachable ({type(exc).__name__})")
    if resp.status_code != 200:
        raise HTTPException(400, (resp.json() or {}).get("detail") or "could not transcribe")
    return resp.json()


async def create(user: auth.User, text: str, *, speaker: str | None = None,
                 language: str | None = None, instruct: str | None = None,
                 reference: bytes | None = None, reference_text: str | None = None,
                 match: str | None = None, temperature=None, seed=None,
                 takes=1, meter: str | None = None, design: str | None = None) -> list[str]:
    """Queue one clip, or `takes` variations of it (different seeds) to choose from.
    `design` = a voice description: the clip is spoken in a NEW voice made from it
    (Qwen3-TTS VoiceDesign) -- a character's voice sample, cloned for all their lines."""
    if design:
        return _create_design(user, text, design, language, seed)
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "text is required — the words to speak")
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"text is over {MAX_TEXT} characters; split it into "
                                 "several clips")
    language = language or "Auto"
    found = next((l for l in LANGUAGES if l.lower() == str(language).lower()), None)
    if found is None:
        raise HTTPException(400, f"language must be one of {', '.join(LANGUAGES)}")
    language = _resolve_language(found, text, speaker)
    instruct = (instruct or "").strip() or None
    if instruct and len(instruct) > MAX_INSTRUCT:
        raise HTTPException(400, f"instruct is over {MAX_INSTRUCT} characters")
    if language == SANSKRIT:
        if reference is not None:
            raise HTTPException(400, "Sanskrit is chanted by Vāgdhenu's own voice; it cannot "
                                     "clone a reference clip")
        if instruct:
            raise HTTPException(400, "instruct does not apply to Sanskrit chant; the metre "
                                     "sets the melody (pass `meter` to choose it)")
        if speaker and not _is(speaker, SANSKRIT_SPEAKERS):
            raise HTTPException(400, f"Sanskrit is chanted only by "
                                     f"{', '.join(SANSKRIT_SPEAKERS)}")
        speaker = "Vagdhenu"
    meter = (meter or "").strip() or None
    if meter is not None:
        if language != SANSKRIT:
            raise HTTPException(400, "meter applies only to Sanskrit chant")
        found_m = next((m for m in METERS if m.lower() == meter.lower()), None)
        if found_m is None:
            raise HTTPException(400, f"meter must be one of {', '.join(METERS)}")
        meter = found_m
    if reference is not None:
        if speaker:
            raise HTTPException(400, "give a speaker OR a reference clip to clone, not both")
        if instruct:
            raise HTTPException(400, "instruct only works with a preset speaker, not with a "
                                     "cloned voice")
        voice = "clone"
    else:
        speaker = speaker or {BURMESE: DEFAULT_BURMESE_SPEAKER,
                              HINDI: DEFAULT_HINDI_SPEAKER}.get(language, DEFAULT_SPEAKER)
        voice = next((s for s in SPEAKERS if s.lower() == str(speaker).lower()), None)
        if voice is None:
            raise HTTPException(400, f"speaker must be one of {', '.join(SPEAKERS)}")
        if language == BURMESE and voice not in BURMESE_SPEAKERS:
            raise HTTPException(400, f"Burmese is spoken by {', '.join(BURMESE_SPEAKERS)} "
                                     f"(or a cloned voice); {voice} does not speak Burmese")
        if voice in BURMESE_SPEAKERS and language != BURMESE:
            raise HTTPException(400, f"{voice} speaks Burmese only; use language Burmese or "
                                     "Auto, or another speaker")
        if voice in SANSKRIT_SPEAKERS and language != SANSKRIT:
            raise HTTPException(400, f"{voice} chants Sanskrit only; use language Sanskrit")
        if voice in HINDI_SPEAKERS and language != HINDI:
            raise HTTPException(400, f"{voice} speaks Hindi only; use language Hindi or Auto, "
                                     "or another speaker")
    match = (match or "style") if voice == "clone" else None
    if match is not None and match not in MATCHES:
        raise HTTPException(400, "match must be style or timbre")
    try:
        takes = int(takes or 1)
        temperature = None if temperature in (None, "") else float(temperature)
        seed = None if seed in (None, "") else int(seed)
    except (TypeError, ValueError):
        raise HTTPException(400, "takes, temperature and seed must be numbers")
    if not 1 <= takes <= MAX_TAKES:
        raise HTTPException(400, f"takes must be 1 to {MAX_TAKES}")
    if temperature is not None and not 0.1 <= temperature <= 1.5:
        raise HTTPException(400, "temperature must be between 0.1 and 1.5")
    if temperature is None and voice == "clone":
        temperature = DEFAULT_CLONE_TEMPERATURE
    if seed is not None and not 0 <= seed < 2 ** 31:
        raise HTTPException(400, "seed must be between 0 and 2^31-1")
    if rate_limited(user.id, takes):
        raise HTTPException(429, f"that would go over {cfg.USER_SPEECH_PER_HOUR} speech clips "
                                 "in an hour, which is the limit for this account")
    ref_wav = await reference_wav(reference) if reference is not None else None

    # Every clip records the seed it used, so a good take can be repeated.
    import random
    base_seed = seed if seed is not None else random.randrange(2 ** 31 - MAX_TAKES)
    ids = []
    for i in range(takes):
        speech_id = db.new_id("spk")
        if ref_wav is not None:
            _ref_path(speech_id).parent.mkdir(parents=True, exist_ok=True)
            _ref_path(speech_id).write_bytes(ref_wav)
        params = {"match": match, "temperature": temperature, "seed": base_seed + i,
                  "take": i + 1, "takes": takes}
        if language == SANSKRIT:
            params["meter"] = meter or "auto"
        db.run("INSERT INTO speech (id,user_id,text,voice,language,instruct,reference_text,"
               "params,status,message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (speech_id, user.id, text, voice, language, instruct,
                (reference_text or "").strip() or None, json.dumps(params), "queued", "queued",
                db.now() + i * 1e-3, db.now()))
        ids.append(speech_id)
    gpuqueue.kick()
    return ids


DESIGN_LANGUAGES = ("English", "Chinese", "Japanese", "Korean", "German", "French", "Russian",
                    "Portuguese", "Spanish", "Italian")


def _create_design(user: auth.User, text: str, brief: str, language: str | None,
                   seed) -> list[str]:
    text = (text or "").strip()
    brief = " ".join(str(brief or "").split())[:700]
    if not text or not brief:
        raise HTTPException(400, "a voice design needs sample text and a voice description")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
    lang = next((l for l in DESIGN_LANGUAGES if l.lower() == str(language or "").lower()),
                "English")
    speech_id = db.new_id("spk")
    params = {"seed": int(seed) if seed not in (None, "") else None}
    db.run("INSERT INTO speech (id,user_id,text,voice,language,instruct,reference_text,"
           "params,status,message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (speech_id, user.id, text, "design", lang, brief, None, json.dumps(params),
            "queued", "queued", db.now(), db.now()))
    gpuqueue.kick()
    return [speech_id]


def params(row) -> dict:
    try:
        return json.loads(row["params"] or "{}")
    except (ValueError, IndexError, KeyError):
        return {}


_re = __import__("re")
_SLOKA_END = _re.compile(r"॥\s*[०-९0-9]*\s*॥?")
_VERSE_NO = _re.compile(r"॥\s*[०-९0-9.]+\s*॥")
# "सञ्जय उवाच ।" / "śrībhagavān uvāca" -- a speaker line, prose rather than verse.
_UVACA = _re.compile(r"^\s*([^\n।|॥]{1,40}?\s+(?:उवाच|uvāca|uvaca))\s*[।|]?\s*\n?",
                     _re.IGNORECASE)
_PUNCT_ONLY = "॥।|0123456789०१२३४५६७८९. \n"


def slokas(text: str) -> list[tuple[str, str | None]]:
    """Split text into chantable pieces: (text, metre override or None).

    One piece per śloka (each ends in ॥). Verse numbers are removed -- Vāgdhenu
    fails on "॥ १ ॥" (IndexError, measured 2026-11-21). A leading "X उवाच" line
    is split off and chanted as prose (gadya): left in front of the verse it
    defeated metre detection and the verse fell back to the wrong metre.
    """
    text = _VERSE_NO.sub("॥", text)
    raw, start = [], 0
    for m in _SLOKA_END.finditer(text):
        raw.append(text[start:m.end()])
        start = m.end()
    raw.append(text[start:])
    out: list[tuple[str, str | None]] = []
    for piece in raw:
        piece = piece.strip()
        m = _UVACA.match(piece)
        if m and piece[m.end():].strip(_PUNCT_ONLY):
            out.append((m.group(1).strip() + " ।", "gadya"))
            piece = piece[m.end():].strip()
        if piece.strip(_PUNCT_ONLY):
            out.append((piece, None))
    return out or [(text.strip(), None)]


async def _chant(client: httpx.AsyncClient, text: str, meter: str, seed: int) -> tuple[bytes, str]:
    """One śloka -> (wav bytes, the metre used). Busy on a full GPU."""
    resp = await client.post(f"{cfg.VAGDHENU_URL}/tts", headers={"X-API-Key": cfg.VAGDHENU_KEY},
                             json={"text": text, "meter": meter or "auto", "seed": int(seed),
                                   "format": "wav", "encoding": "base64"})
    if resp.status_code == 503 or (resp.status_code == 500 and "memory" in resp.text.lower()):
        raise gpuqueue.Busy()
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        raise RuntimeError(detail or f"the chant service answered HTTP {resp.status_code}")
    body = resp.json()
    import base64
    used = body.get("meter") or meter
    if body.get("meter_fallback"):
        used = f"{used} (metre not recognised; fell back to it)"
    return base64.b64decode(body["audio_base64"]), used


async def _run_chant(row, p: dict) -> None:
    speech_id, user_id = row["id"], row["user_id"]
    pieces = slokas(row["text"])
    seed = int(p.get("seed") or 60) % (2 ** 31)
    meters: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=360.0) as client:
            wavs = []
            for i, (piece, forced) in enumerate(pieces):
                wav, used = await _chant(client, piece, forced or p.get("meter") or "auto",
                                         seed + i)
                wavs.append(wav)
                meters.append(used)
            # Vāgdhenu otherwise keeps 2.4 GB of VRAM for 15 idle minutes.
            try:
                await client.post(f"{cfg.VAGDHENU_URL}/unload",
                                  headers={"X-API-Key": cfg.VAGDHENU_KEY})
            except httpx.HTTPError:
                pass
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        raise gpuqueue.Busy()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"the chant service is unreachable ({type(exc).__name__})")
    dest = cfg.speech_dir / user_id
    dest.mkdir(parents=True, exist_ok=True)
    path, part = dest / f"{speech_id}.mp3", dest / f"{speech_id}.part.mp3"
    with tempfile.TemporaryDirectory() as tmp:
        ins, chain = [], ""
        for i, wav in enumerate(wavs):
            f = Path(tmp) / f"{i}.wav"
            f.write_bytes(wav)
            ins += ["-i", str(f)]
            # A breath between ślokas, as a reciter leaves.
            chain += f"[{i}:a]apad=pad_dur={0.8 if i < len(wavs) - 1 else 0}[a{i}];"
        chain += "".join(f"[a{i}]" for i in range(len(wavs))) + f"concat=n={len(wavs)}:v=0:a=1[out]"
        await _ffmpeg([*ins, "-filter_complex", chain, "-map", "[out]",
                       "-codec:a", "libmp3lame", "-q:a", "2", str(part)])
    os.replace(part, path)
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
        str(path), stdout=asyncio.subprocess.PIPE)
    out, _ = await probe.communicate()
    try:
        duration = float(out.decode().strip())
    except ValueError:
        duration = None
    p["meters_used"] = meters
    db.run("UPDATE speech SET status='succeeded', path=?, duration_s=?, params=?, "
           "message=?, updated_at=? WHERE id=?",
           (str(path), duration, json.dumps(p), ("metre: " + "; ".join(meters))[:400],
            db.now(), speech_id))


def _next_uses(url: str, speech_id: str) -> bool:
    """Is the next queued speech line for the same engine?"""
    nxt = db.one("SELECT language, voice FROM speech WHERE status='queued' AND id!=? "
                 "ORDER BY created_at LIMIT 1", (speech_id,))
    return bool(nxt) and engine_url(nxt["language"], nxt["voice"]) == url


_kept = {"on": False}


async def unload_kept() -> None:
    """Hand the card back after a batch (VoxCPM2 kept its model between lines)."""
    if not _kept["on"]:
        return
    _kept["on"] = False
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(f"{cfg.TTS_MY_URL}/unload")
    except httpx.HTTPError:
        pass                              # the engine's own sweeper unloads within a minute


async def run(row) -> None:
    """Called by the GPU queue when the card is free. Raises gpuqueue.Busy on a full GPU."""
    if row["language"] == SANSKRIT:
        return await _run_chant(row, params(row))
    speech_id, user_id, text = row["id"], row["user_id"], row["text"]
    voice, language, instruct = row["voice"], row["language"], row["instruct"]
    ref_text, p = row["reference_text"], params(row)
    ref_wav = _ref_path(speech_id).read_bytes() if voice == "clone" else None
    url = cfg.TTS_URL if voice == "design" else engine_url(language, voice)
    # VoxCPM2 can keep its model loaded when the next queued line goes to it too (one
    # load per batch instead of one per line); after the last it unloads as before.
    keep = url == cfg.TTS_MY_URL and _next_uses(url, speech_id)
    if keep:
        _kept["on"] = True
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            tag = engine_language(language, voice)
            if voice == "design":
                resp = await client.post(f"{cfg.TTS_URL}/design", json={
                    "text": text, "instruct": instruct, "language": language,
                    "seed": p.get("seed")})
            elif ref_wav is not None:
                data = {"text": text, "language": tag, "mode": p.get("match") or "style"}
                if keep:
                    data["keep"] = "true"
                for k in ("temperature", "seed"):
                    if p.get(k) is not None:
                        data[k] = str(p[k])
                if ref_text:
                    data["reference_text"] = ref_text
                resp = await client.post(
                    f"{url}/clone", data=data,
                    files={"reference_audio": ("reference.wav", ref_wav, "audio/wav")})
            else:
                resp = await client.post(f"{url}/speak", json={
                    "text": text, "speaker": voice, "language": tag,
                    "instruct": instruct, "temperature": p.get("temperature"),
                    "seed": p.get("seed"), **({"keep": True} if keep else {})})
        if resp.status_code == 503:
            raise gpuqueue.Busy()
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = None
            raise RuntimeError(detail or f"the speech service answered HTTP {resp.status_code}")
        duration = float(resp.headers.get("X-TTS-Duration-S") or 0) or None
        if voice == "design" and resp.headers.get("X-TTS-F0"):
            p["f0"] = float(resp.headers["X-TTS-F0"])          # checked against the brief
            db.run("UPDATE speech SET params=? WHERE id=?", (json.dumps(p), speech_id))
        dest = cfg.speech_dir / user_id
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{speech_id}.mp3"
        part = dest / f"{speech_id}.part.mp3"
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "out.wav"
            wav.write_bytes(resp.content)
            await _ffmpeg(["-i", str(wav), "-codec:a", "libmp3lame", "-q:a", "2", str(part)])
        os.replace(part, path)
        heard = resp.headers.get("X-TTS-Reference-Text")
        if heard and not ref_text:
            # Keep what Whisper heard, so the person can see it and correct it.
            from urllib.parse import unquote
            db.run("UPDATE speech SET reference_text=? WHERE id=?", (unquote(heard), speech_id))
        db.run("UPDATE speech SET status='succeeded', path=?, duration_s=?, message=NULL, "
               "updated_at=? WHERE id=?", (str(path), duration, db.now(), speech_id))
        _ref_path(speech_id).unlink(missing_ok=True)
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        # The engine is restarting (it crashed or was redeployed). That is not this
        # clip's fault: on 2026-11-21 a tts-my restart of a few seconds failed 62
        # queued clips in a row. Back in the queue instead.
        raise gpuqueue.Busy()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"the speech service is unreachable ({type(exc).__name__})")


async def wait(speech_id: str, seconds: float, poll: float = 1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        row = db.one("SELECT * FROM speech WHERE id=?", (speech_id,))
        if row["status"] in TERMINAL or loop.time() + poll > deadline:
            return row
        await asyncio.sleep(poll)


def add_to_project(user: auth.User, speech_id: str, project) -> int:
    row = owned(user, speech_id)
    if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(400, f"speech {row['id']} is not finished ({row['status']}); call "
                                 "get_speech until it has succeeded")
    return service.add_files(project, [(f"{row['id']}.mp3", "audio/mpeg",
                                        Path(row["path"]).read_bytes())])


def delete(user: auth.User, speech_id: str) -> None:
    row = owned(user, speech_id)
    if row["status"] == "generating":
        raise HTTPException(409, "this clip is being generated; delete it once it finishes")
    _ref_path(row["id"]).unlink(missing_ok=True)
    if row["path"]:
        Path(row["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM speech WHERE id=?", (row["id"],))


def delete_for_user(user_id: str) -> None:
    for row in db.query("SELECT id, path FROM speech WHERE user_id=?", (user_id,)):
        _ref_path(row["id"]).unlink(missing_ok=True)
        if row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM speech WHERE user_id=?", (user_id,))


def purge_older_than(cutoff: float) -> None:
    for row in db.query("SELECT id, path FROM speech WHERE updated_at < ?", (cutoff,)):
        _ref_path(row["id"]).unlink(missing_ok=True)
        if row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
        db.run("DELETE FROM speech WHERE id=?", (row["id"],))
