"""Burmese (Myanmar) and Hindi speech for Story Studio: VoxCPM2 (OpenBMB, Apache-2.0).

Hindi preset voices (Meera, Anaya, Arjun, Vikram) were added 2026-11-21: Qwen3-TTS
speaks Hindi with a foreign accent (2-10% character errors by Whisper on two test
sentences), these presets made 0-6% on the same sentences. Both are offered; the studio picks the engine by speaker.

Same contract as `tts/server.py` (POST /speak, POST /clone, wav back, 503 when
the GPU is full), so `app/speech.py` only has to choose the URL.

Chosen by a measured bake-off on 2026-11-20 (~/mm-tts-bakeoff): with a
Burmese ASR as the judge, VoxCPM2 scored 5-6% syllable errors on OpenSLR-80
sentences against 5.0% for the human recordings, and was the only model that
read digits correctly. OmniVoice, F5-Myanmar-TTS and Meta MMS are
non-commercial and scored worse; MyanmarTTS (CC0) cloned poorly.

Rules the measurements set:
- Preset speakers are fixed reference clips (made once with VoxCPM2 voice
  design, best of three takes) used in "continuation" mode: clip + its
  transcript. VoxCPM2 alone gives a different voice on every call.
- `instruct` uses the reference without continuation, with the instruction as a
  "(...)" prefix -- VoxCPM2's own control syntax.
- Clones use the reference audio only, never a transcript. On a Burmese clip
  that scored 0% errors, against 2.2% with a transcript, so the transcript adds
  nothing, and the studio's Whisper writes poor Burmese transcripts.
- Text is spoken sentence by sentence and joined: a 961-character paragraph in
  one call drifted (14% errors), sentences stay at about 5%.

The card is shared with WIS: the model is dropped after every render
(TTS_IDLE_UNLOAD_S=0) and an out-of-memory error answers 503.
"""
from __future__ import annotations

import asyncio
import ctypes
import gc
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("studio.tts-my")

MODEL_DIR = os.getenv("TTS_MY_MODEL_DIR", "/models/voxcpm/VoxCPM2")
PRESET_DIR = Path(os.getenv("TTS_MY_PRESET_DIR", "/models/voxcpm/presets"))
IDLE = int(os.getenv("TTS_IDLE_UNLOAD_S", "0"))
KEEP_S = 60
TIMEOUT = int(os.getenv("TTS_RENDER_TIMEOUT_S", "300"))
MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "1200"))
CHUNK_CHARS = int(os.getenv("TTS_MY_CHUNK_CHARS", "220"))
GAP_S = 0.3
CFG, STEPS = 2.0, 10
MAX_REF_BYTES = 20 * 1024 * 1024
MAX_REF_S = 30.0

_meta = json.loads((PRESET_DIR / "presets.json").read_text(encoding="utf-8"))
PRESET_TEXT: str = _meta["transcript"]
# A preset speaks its own clip's words; Hindi clips say a Hindi passage.
TRANSCRIPTS: dict[str, str] = _meta.get("transcripts", {})
SPEAKERS: dict[str, str] = _meta["speakers"]
LANGUAGES = ["Burmese", "Hindi"]


class GpuBusy(Exception):
    pass


class Engine:
    def __init__(self):
        self.model = None
        self.lock = threading.Lock()
        self.last = 0.0
        self.renders = 0
        # A batch of lines keeps the model between requests (`keep`); this bounds it:
        # unloaded by the sweeper once nothing has asked to keep it for KEEP_S.
        self.keep_until = 0.0

    def _drop(self):
        self.model = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        # Without this the host RAM of each load stays with the process (glibc keeps
        # the freed arenas), and the third load/unload cycle hit the 16 GB limit.
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except OSError:
            pass

    def _load(self):
        if self.model is None:
            from voxcpm import VoxCPM
            t = time.time()
            # optimize=False: torch.compile needs a C compiler the image does not have,
            # and the bake-off speeds were measured without it.
            self.model = VoxCPM.from_pretrained(MODEL_DIR, load_denoiser=False, optimize=False,
                                                local_files_only=True)
            log.info("VoxCPM2 loaded in %.1fs", time.time() - t)
        return self.model

    def run(self, fn, keep: bool = False):
        with self.lock:
            try:
                wav, sr = fn(self._load())
            except Exception as exc:
                if not (isinstance(exc, torch.cuda.OutOfMemoryError)
                        or "out of memory" in str(exc).lower()):
                    if IDLE <= 0:
                        self._drop()
                    raise
                log.warning("out of GPU memory; unloading")
                self._drop()
                raise GpuBusy()
            self.last, self.renders = time.time(), self.renders + 1
            if keep:
                # More lines for this engine are queued right behind this one: reloading
                # 9 s per line cost a third of a 23-line batch (2026-11-24).
                self.keep_until = time.time() + KEEP_S
            elif IDLE <= 0:
                self._drop()
            return wav, sr

    def unload(self):
        with self.lock:
            self.keep_until = 0.0
            if self.model is not None:
                self._drop()

    def unload_if_idle(self):
        if self.model is None:
            return
        if IDLE <= 0:
            if time.time() < self.keep_until:
                return
        elif time.time() - self.last < IDLE:
            return
        if self.lock.acquire(blocking=False):
            try:
                self._drop()
            finally:
                self.lock.release()


E = Engine()
app = FastAPI(title="Story Studio Burmese TTS", version="1.0.0")


@app.on_event("startup")
async def _sweeper():
    async def sweep():
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(E.unload_if_idle)
    asyncio.create_task(sweep())


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": "VoxCPM2", "loaded": E.model is not None,
            "renders": E.renders, "speakers": SPEAKERS, "languages": LANGUAGES,
            "weights_present": Path(MODEL_DIR, "model.safetensors").exists()}


# ----------------------------------------------------------------------------- text

_SENT = re.compile(r"(?<=[။।॥!?\.])\s*|\n+")


def chunks(text: str) -> list[str]:
    """Sentences, packed up to CHUNK_CHARS; a sentence longer than that splits at ၊ or spaces."""
    parts: list[str] = []
    for s in (x.strip() for x in _SENT.split(text)):
        if not s:
            continue
        while len(s) > CHUNK_CHARS:
            cut = max(s.rfind("၊", 0, CHUNK_CHARS), s.rfind(",", 0, CHUNK_CHARS),
                      s.rfind(" ", 0, CHUNK_CHARS))
            cut = cut + 1 if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
            parts.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            parts.append(s)
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + 1 + len(p) <= CHUNK_CHARS:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def _text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise HTTPException(422, "text is empty")
    if len(text) > MAX_CHARS:
        raise HTTPException(413, f"text is over {MAX_CHARS} characters")
    return text


# ----------------------------------------------------------------------------- render

def _synth(m, text: str, seed, *, prefix: str = "", **ref) -> tuple[np.ndarray, int]:
    sr = int(m.tts_model.sample_rate)
    gap = np.zeros(int(GAP_S * sr), dtype=np.float32)
    pieces = []
    for i, part in enumerate(chunks(text)):
        if seed is not None:
            torch.manual_seed(int(seed) + i)
            torch.cuda.manual_seed_all(int(seed) + i)
        w = m.generate(text=prefix + part, cfg_value=CFG, inference_timesteps=STEPS, **ref)
        pieces += [np.asarray(w, dtype=np.float32).reshape(-1), gap]
    out = np.concatenate(pieces[:-1]) if pieces else np.zeros(sr, dtype=np.float32)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.99:
        out = out * (0.98 / peak)
    return out, sr


async def _render(fn, keep: bool = False) -> Response:
    try:
        out, sr = await asyncio.wait_for(asyncio.to_thread(E.run, fn, keep), TIMEOUT)
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
    buf = io.BytesIO()
    sf.write(buf, out, sr, format="WAV")
    return Response(buf.getvalue(), media_type="audio/wav",
                    headers={"X-TTS-Duration-S": f"{len(out) / sr:.3f}"})


class Speak(BaseModel):
    text: str
    speaker: str = "Thiri"
    language: str = "Burmese"
    instruct: str | None = None
    temperature: float | None = None     # accepted for contract parity; VoxCPM2 has none
    top_p: float | None = None
    seed: int | None = None
    keep: bool = False                   # keep the model loaded: more lines are coming


@app.post("/unload")
async def unload():
    await asyncio.to_thread(E.unload)
    return {"unloaded": True}


@app.post("/speak")
async def speak(req: Speak):
    text = _text(req.text)
    speaker = next((s for s in SPEAKERS if s.lower() == req.speaker.lower()), None)
    if speaker is None:
        raise HTTPException(422, f"speaker must be one of {', '.join(SPEAKERS)}")
    ref = str(PRESET_DIR / f"{speaker}.wav")
    instruct = re.sub(r"[()]", "", (req.instruct or "")).strip()[:300]
    if instruct:
        # Continuation would copy the preset's delivery and override the instruction.
        kw = {"prefix": f"({instruct})", "reference_wav_path": ref}
    else:
        kw = {"reference_wav_path": ref, "prompt_wav_path": ref, "prompt_text": TRANSCRIPTS.get(speaker, PRESET_TEXT)}
    return await _render(lambda m: _synth(m, text, req.seed, **kw), req.keep)


def _reference_file(raw: bytes, tmp: str) -> str:
    try:
        wav, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception:                                          # noqa: BLE001
        raise HTTPException(415, "the reference clip is not readable audio (send wav)")
    if wav.ndim == 2:
        wav = wav.mean(axis=1)
    wav = wav[: int(MAX_REF_S * sr)]
    if len(wav) < sr:
        raise HTTPException(422, "the reference clip is shorter than a second")
    path = os.path.join(tmp, "ref.wav")
    sf.write(path, wav, sr)
    return path


@app.post("/clone")
async def clone(text: str = Form(...), reference_audio: UploadFile = File(...),
                reference_text: str | None = Form(None), language: str = Form("Burmese"),
                mode: str = Form("style"), temperature: float | None = Form(None),
                top_p: float | None = Form(None), top_k: int | None = Form(None),
                repetition_penalty: float | None = Form(None), seed: int | None = Form(None),
                keep: bool = Form(False)):
    """The clip's voice speaking Burmese. Reference audio only (see the module
    docstring); reference_text and mode are accepted and ignored."""
    text = _text(text)
    raw = await reference_audio.read(MAX_REF_BYTES + 1)
    if len(raw) > MAX_REF_BYTES:
        raise HTTPException(413, "the reference clip is over 20 MB")
    with tempfile.TemporaryDirectory() as tmp:
        ref = _reference_file(raw, tmp)
        return await _render(lambda m: _synth(m, text, seed, reference_wav_path=ref), keep)
