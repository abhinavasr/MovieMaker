"""Qwen3-TTS for Story Studio: preset speakers, and voice cloning from a clip.

Runs in the `vagdhenu-qwen-tts` image (reused, not rebuilt) as the studio's own
container, so the Vagdhenu stack and tts.abhinava.xyz are never touched.

Two checkpoints, at most one on the GPU at a time:
- CustomVoice: nine built-in speakers plus an optional style instruction. This
  is what speaks when no reference is given. Weights in this container's cache.
- Base: clones a voice from a reference clip. Its weights are Vagdhenu's copy,
  mounted read-only, so nothing is downloaded twice.

The card is shared with WIS, which holds most of it while warm and does not
know this process exists. So a render never tries to push WIS out: an
out-of-memory error unloads, and the caller gets a 503 that says the GPU is busy.
The model is also dropped right after each render (TTS_IDLE_UNLOAD_S=0) so the
memory goes straight back.

Cloning likeness depends almost entirely on the reference transcript. With it,
Qwen runs in ICL mode: the clip is the start of the utterance and the new text
continues it, so accent, rhythm and delivery carry over. Without it only the
speaker embedding is used ("x-vector only") and the result is a generic voice of
roughly the right timbre -- measured as "useless" by the first real user. So a
missing transcript is filled in with Whisper (on the CPU: it must never compete
with WIS for the card), and timbre-only is an explicit choice, not a fallback.
"""
from __future__ import annotations

import asyncio
import gc
import glob
import io
import logging
import os
import threading
import time

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("studio.tts")

CUSTOM_GLOB = os.getenv(
    "TTS_CUSTOM_GLOB", "/models/hf/hub/models--Qwen--Qwen3-TTS-12Hz-1.7B-CustomVoice/snapshots/*")
BASE_GLOB = os.getenv(
    "TTS_BASE_GLOB", "/base-hf/hub/models--Qwen--Qwen3-TTS-12Hz-1.7B-Base/snapshots/*")
# VoiceDesign: a NEW voice from a written description ("elderly woman, warm low voice, slow").
# The studio designs one short sample per character and clones every line from it, so a
# character keeps one voice (2026-11-25: characters without a voice all got the male default).
DESIGN_GLOB = os.getenv(
    "TTS_DESIGN_GLOB", "/models/hf/hub/models--Qwen--Qwen3-TTS-12Hz-1.7B-VoiceDesign/snapshots/*")
DEVICE = os.getenv("TTS_DEVICE", "cuda:0")
IDLE = int(os.getenv("TTS_IDLE_UNLOAD_S", "0"))
TIMEOUT = int(os.getenv("TTS_RENDER_TIMEOUT_S", "240"))
MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "1200"))
MAX_REF_BYTES = 20 * 1024 * 1024
MAX_REF_S = 30.0
WHISPER_GLOB = os.getenv(
    "TTS_WHISPER_GLOB", "/models/hf/hub/models--openai--whisper-large-v3-turbo/snapshots/*")
WHISPER_IDLE_S = 600

SPEAKERS = ["Vivian", "Serena", "Ono_Anna", "Sohee", "Ryan", "Aiden", "Uncle_Fu", "Dylan", "Eric"]
LANGUAGES = ["Auto", "English", "Chinese", "Japanese", "Korean", "German", "French",
             "Russian", "Portuguese", "Spanish", "Italian"]


class GpuBusy(Exception):
    pass


class Engine:
    def __init__(self):
        self.kind: str | None = None          # "custom" | "base" | "design" | None
        self.model = None
        self.lock = threading.Lock()
        self.last = 0.0
        self.renders = 0

    def _drop(self):
        self.model, self.kind = None, None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _load(self, kind: str):
        if self.kind == kind:
            return self.model
        self._drop()
        from qwen_tts import Qwen3TTSModel
        # Local snapshot folders, not repo ids: given an id, qwen_tts asks the
        # Hub for model info even when the weights are cached, and fails offline.
        pattern = {"custom": CUSTOM_GLOB, "base": BASE_GLOB, "design": DESIGN_GLOB}[kind]
        found = sorted(glob.glob(pattern))
        if not found:
            raise RuntimeError(f"the {kind} checkpoint is not on disk ({pattern})")
        source = found[-1]
        log.info("loading %s (%s)", kind, source)
        self.model = Qwen3TTSModel.from_pretrained(
            source, device_map=DEVICE, dtype=torch.bfloat16, attn_implementation="sdpa")
        self.kind = kind
        return self.model

    def run(self, kind: str, fn):
        with self.lock:
            try:
                wavs, sr = fn(self._load(kind))
            except Exception as exc:
                # Loading can fail as AcceleratorError rather than
                # OutOfMemoryError, so the message is what identifies it.
                if not (isinstance(exc, torch.cuda.OutOfMemoryError)
                        or "out of memory" in str(exc).lower()):
                    raise
                log.warning("out of GPU memory rendering %s; unloading", kind)
                self._drop()
                raise GpuBusy()
            self.last, self.renders = time.time(), self.renders + 1
            if IDLE <= 0:
                # Default: give the memory back at once. Loading takes a few
                # seconds; holding ~5 GB could make a WIS video that starts
                # in the meantime run out of memory.
                self._drop()
            return np.asarray(wavs[0], dtype=np.float32), int(sr)

    def unload_if_idle(self):
        if self.model is None or IDLE <= 0 or time.time() - self.last < IDLE:
            return
        if self.lock.acquire(blocking=False):
            try:
                log.info("idle for %ss; unloading %s", IDLE, self.kind)
                self._drop()
            finally:
                self.lock.release()


class Transcriber:
    """Whisper large-v3-turbo on the CPU, loaded on first use, dropped when idle."""

    def __init__(self):
        self.pipe, self.last, self.lock = None, 0.0, threading.Lock()

    def __call__(self, wav: np.ndarray, sr: int, song: bool = False) -> tuple[str, str | None]:
        with self.lock:
            if self.pipe is None:
                found = sorted(glob.glob(WHISPER_GLOB))
                if not found:
                    raise RuntimeError("the transcription model is not installed")
                from transformers import pipeline
                log.info("loading whisper on cpu")
                self.pipe = pipeline("automatic-speech-recognition", model=found[-1],
                                     device="cpu", torch_dtype=torch.float32)
            if sr != 16000:
                import librosa
                wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
            if song:
                # A whole song: 30 s windows, one line per sung phrase.
                out = self.pipe({"raw": wav, "sampling_rate": 16000}, chunk_length_s=30,
                                batch_size=1, return_timestamps=True,
                                generate_kwargs={"task": "transcribe"})
                self.last = time.time()
                lines = [(c.get("text") or "").strip() for c in out.get("chunks") or []]
                return "\n".join(l for l in lines if l), None
            out = self.pipe({"raw": wav, "sampling_rate": 16000},
                            return_language=True, generate_kwargs={"task": "transcribe"})
            self.last = time.time()
            chunks = out.get("chunks") or [{}]
            return (out.get("text") or "").strip(), chunks[0].get("language")

    def unload_if_idle(self):
        if self.pipe is not None and time.time() - self.last > WHISPER_IDLE_S \
                and self.lock.acquire(blocking=False):
            try:
                self.pipe = None
                gc.collect()
            finally:
                self.lock.release()


def prepare_reference(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode, mono, trim silence at both ends, level the peak. Rejects bad clips."""
    try:
        ref, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception:                                          # noqa: BLE001
        raise HTTPException(415, "the reference clip is not readable audio (use wav, flac "
                                 "or ogg)")
    if ref.ndim == 2:
        ref = ref.mean(axis=1)
    # Edge silence becomes part of the prompt and slows/flattens the start of
    # the clone. Frames under -40 dB of the clip's peak count as silence.
    frame = max(1, sr // 50)
    if len(ref) > frame * 3:
        rms = np.sqrt(np.convolve(ref ** 2, np.ones(frame) / frame, mode="same"))
        loud = np.where(rms > rms.max() * 0.01)[0]
        if len(loud):
            pad = sr // 10
            ref = ref[max(0, loud[0] - pad):min(len(ref), loud[-1] + pad)]
    peak = float(np.abs(ref).max()) if len(ref) else 0.0
    if peak < 1e-3:
        raise HTTPException(422, "the reference clip is silent")
    ref = ref * (0.89 / peak)                                  # about -1 dBFS
    seconds = len(ref) / float(sr)
    if seconds < 2.0:
        raise HTTPException(422, "the reference clip has under 2 seconds of speech; use 5-20 s")
    if seconds > MAX_REF_S:
        raise HTTPException(422, f"the reference clip must be under {MAX_REF_S:g} seconds")
    return ref, sr


def seed_all(seed) -> None:
    if seed is not None:
        torch.manual_seed(int(seed))
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(int(seed))


def sampling(temperature, top_p, top_k, repetition_penalty) -> dict:
    """generate() kwargs from the caller's knobs; the sub-talker follows the talker."""
    kw = {}
    if temperature is not None:
        kw["temperature"] = kw["subtalker_temperature"] = float(min(max(temperature, 0.1), 1.5))
    if top_p is not None:
        kw["top_p"] = kw["subtalker_top_p"] = float(min(max(top_p, 0.1), 1.0))
    if top_k is not None:
        kw["top_k"] = kw["subtalker_top_k"] = int(min(max(top_k, 1), 200))
    if repetition_penalty is not None:
        kw["repetition_penalty"] = float(min(max(repetition_penalty, 1.0), 2.0))
    return kw


T = Transcriber()
E = Engine()
app = FastAPI(title="Story Studio TTS", version="1.0.0")


@app.on_event("startup")
async def _sweeper():
    async def sweep():
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(E.unload_if_idle)
            await asyncio.to_thread(T.unload_if_idle)
    asyncio.create_task(sweep())


@app.get("/healthz")
def healthz():
    return {"status": "ok", "loaded": E.kind, "renders": E.renders,
            "speakers": SPEAKERS, "languages": LANGUAGES,
            "clone_available": bool(glob.glob(BASE_GLOB)),
            "design_available": bool(glob.glob(DESIGN_GLOB)),
            "transcribe_available": bool(glob.glob(WHISPER_GLOB))}


def median_f0(x: np.ndarray, sr: int) -> float:
    """Median pitch of the voiced frames (autocorrelation). The studio checks a designed voice
    against its description with it: a "female" voice came out at 128 Hz (2026-11-25)."""
    x = np.asarray(x, dtype=np.float32)
    fr, hop, out = int(0.04 * sr), int(0.01 * sr), []
    lo, hi = int(sr / 400), int(sr / 70)
    for i in range(0, max(0, len(x) - fr), hop):
        w = x[i:i + fr] * np.hanning(fr)
        if np.sqrt((w ** 2).mean()) < 0.02:
            continue
        ac = np.correlate(w, w, "full")[fr - 1:]
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.35 * ac[0]:
            out.append(sr / k)
    return float(np.median(out)) if out else 0.0


def _wav(out: np.ndarray, sr: int, pitch: bool = False) -> Response:
    buf = io.BytesIO()
    sf.write(buf, out, sr, format="WAV")
    headers = {"X-TTS-Duration-S": f"{len(out) / sr:.3f}"}
    if pitch:
        headers["X-TTS-F0"] = f"{median_f0(out, sr):.0f}"
    return Response(buf.getvalue(), media_type="audio/wav", headers=headers)


def _text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise HTTPException(422, "text is empty")
    if len(text) > MAX_CHARS:
        raise HTTPException(413, f"text is over {MAX_CHARS} characters")
    return text


async def _render(kind: str, fn, pitch: bool = False) -> Response:
    try:
        out, sr = await asyncio.wait_for(asyncio.to_thread(E.run, kind, fn), TIMEOUT)
    except GpuBusy:
        raise HTTPException(503, "the GPU is busy with pictures or videos right now; "
                                 "try again in a few minutes")
    except asyncio.TimeoutError:
        raise HTTPException(504, f"speech took longer than {TIMEOUT}s")
    except HTTPException:
        raise
    except Exception as exc:                                   # noqa: BLE001
        log.exception("render failed")
        raise HTTPException(500, f"render failed: {type(exc).__name__}: {exc}")
    return _wav(out, sr, pitch)


class Speak(BaseModel):
    text: str
    speaker: str = "Ryan"
    language: str = "Auto"
    instruct: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None


@app.post("/speak")
async def speak(req: Speak):
    text = _text(req.text)
    speaker = next((s for s in SPEAKERS if s.lower() == req.speaker.lower()), None)
    if speaker is None:
        raise HTTPException(422, f"speaker must be one of {', '.join(SPEAKERS)}")
    language = next((l for l in LANGUAGES if l.lower() == req.language.lower()), None)
    if language is None:
        raise HTTPException(422, f"language must be one of {', '.join(LANGUAGES)}")
    instruct = (req.instruct or "").strip()[:500] or None
    kw = sampling(req.temperature, req.top_p, None, None)

    def go(m):                     # runs under the engine lock, so the seed holds
        seed_all(req.seed)
        return m.generate_custom_voice(text=text, speaker=speaker, language=language,
                                       instruct=instruct, **kw)
    return await _render("custom", go)


class Design(BaseModel):
    text: str
    instruct: str
    language: str = "English"
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None


@app.post("/design")
async def design(req: Design):
    """Speak `text` in a new voice described by `instruct` (gender, age, pitch, timbre,
    pace, accent, emotion). Used to make each character's voice sample."""
    text = _text(req.text)
    instruct = (req.instruct or "").strip()[:800]
    if not instruct:
        raise HTTPException(422, "describe the voice in `instruct`")
    language = next((l for l in LANGUAGES if l.lower() == req.language.lower()), None)
    if language is None or language == "Auto":
        language = "English"
    kw = sampling(req.temperature, req.top_p, None, None)

    def go(m):
        seed_all(req.seed)
        return m.generate_voice_design(text=text, instruct=instruct, language=language, **kw)
    return await _render("design", go, pitch=True)


async def _transcribe(ref: np.ndarray, sr: int) -> tuple[str, str | None]:
    try:
        return await asyncio.to_thread(T, ref, sr)
    except Exception as exc:                                   # noqa: BLE001
        log.exception("transcription failed")
        raise HTTPException(500, f"could not transcribe the reference clip: {exc}")


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """The words in a reference clip, after the same clean-up /clone applies."""
    raw = await audio.read(MAX_REF_BYTES + 1)
    if len(raw) > MAX_REF_BYTES:
        raise HTTPException(413, "the reference clip is over 20 MB")
    ref, sr = prepare_reference(raw)
    text, lang = await _transcribe(ref, sr)
    return {"text": text, "language": lang, "seconds": round(len(ref) / sr, 1)}


MAX_SONG_S = 360.0


@app.post("/transcribe_song")
async def transcribe_song(audio: UploadFile = File(...)):
    """The sung words in a whole track (up to 6 minutes), one line per phrase.

    No trimming or levelling: that clean-up is for short spoken reference clips."""
    raw = await audio.read(100 * 1024 * 1024 + 1)
    try:
        wav, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception:                                          # noqa: BLE001
        raise HTTPException(415, "the track is not readable audio (send wav)")
    if wav.ndim == 2:
        wav = wav.mean(axis=1)
    wav = wav[: int(MAX_SONG_S * sr)]
    try:
        text, _ = await asyncio.to_thread(T, wav, sr, True)
    except Exception as exc:                                   # noqa: BLE001
        log.exception("song transcription failed")
        raise HTTPException(500, f"could not transcribe the track: {exc}")
    return {"text": text, "seconds": round(len(wav) / sr, 1)}


@app.post("/clone")
async def clone(text: str = Form(...), reference_audio: UploadFile = File(...),
                reference_text: str | None = Form(None), language: str = Form("Auto"),
                mode: str = Form("style"), temperature: float | None = Form(None),
                top_p: float | None = Form(None), top_k: int | None = Form(None),
                repetition_penalty: float | None = Form(None), seed: int | None = Form(None)):
    """mode=style: ICL with the transcript (auto-transcribed if missing) -- the
    clip's accent, pace and delivery carry over. mode=timbre: speaker embedding
    only -- just the voice colour, delivery is the model's own."""
    text = _text(text)
    language = next((l for l in LANGUAGES if l.lower() == language.lower()), None)
    if language is None:
        raise HTTPException(422, f"language must be one of {', '.join(LANGUAGES)}")
    if mode not in ("style", "timbre"):
        raise HTTPException(422, "mode must be style or timbre")
    raw = await reference_audio.read(MAX_REF_BYTES + 1)
    if len(raw) > MAX_REF_BYTES:
        raise HTTPException(413, "the reference clip is over 20 MB")
    ref, sr = prepare_reference(raw)
    ref_text = (reference_text or "").strip() or None
    transcribed = False
    if mode == "style" and ref_text is None:
        ref_text, _ = await _transcribe(ref, sr)
        transcribed = True
        if not ref_text:
            raise HTTPException(422, "no speech was found in the reference clip")
    kw = sampling(temperature, top_p, top_k, repetition_penalty)

    def go(m):                     # runs under the engine lock, so the seed holds
        seed_all(seed)
        return m.generate_voice_clone(text=text, language=language, ref_audio=(ref, sr),
                                      ref_text=ref_text if mode == "style" else None,
                                      x_vector_only_mode=mode == "timbre", **kw)
    response = await _render("base", go)
    if transcribed:
        # Header values must be latin-1; the transcript goes percent-encoded.
        from urllib.parse import quote
        response.headers["X-TTS-Reference-Text"] = quote(ref_text[:1500])
    return response
