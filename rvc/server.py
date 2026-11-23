"""The studio's voice service: Ultimate RVC behind a small HTTP API.

Studio network only, no host port. The studio (app/voices.py) owns all state — who owns
which voice, consent, jobs — and decides WHEN to run something (app/gpuqueue.py). This
server only does the work, one GPU operation at a time, each in a fresh child process
(ops.py) so the card is empty between jobs.

Files: the studio uploads inputs here and downloads outputs from /out/<job>/<name>.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

DATA = Path(os.getenv("RVC_DATA", "/data"))
INBOX, OUT = DATA / "inbox", DATA / "out"
for d in (INBOX, OUT, DATA / "datasets", DATA / "work"):
    d.mkdir(parents=True, exist_ok=True)
OPS = Path(__file__).with_name("ops.py")
MAX_UPLOAD = 100 * 1024 * 1024
OP_TIMEOUT = {"clean": 900, "prepare": 3600, "train_slice": 1800, "finalize": 1800,
              "resing": 1800, "convert": 900, "separate": 1200}
ID = re.compile(r"^[a-z]{3,5}_[A-Za-z0-9]{6,40}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rvc")
app = FastAPI(title="studio-rvc")
gpu = asyncio.Lock()
state = {"busy": None, "since": None}


def _id(value: str, what: str) -> str:
    if not ID.match(value or ""):
        raise HTTPException(400, f"bad {what}")
    return value


async def _run(op: str, args: dict) -> dict:
    """Run one ops.py operation in a child process. 503 = GPU full, try again later."""
    async with gpu:
        state.update(busy=op, since=time.time())
        with tempfile.TemporaryDirectory() as tmp:
            a, r = Path(tmp) / "args.json", Path(tmp) / "result.json"
            a.write_text(json.dumps(args))
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(OPS), op, str(a), str(r), cwd=os.getenv("HOME", "/cache"))
            try:
                await asyncio.wait_for(proc.wait(), OP_TIMEOUT[op])
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise HTTPException(500, f"{op} took longer than {OP_TIMEOUT[op] // 60} minutes")
            finally:
                state.update(busy=None, since=None)
            if not r.exists():
                raise HTTPException(500, f"{op} crashed (exit {proc.returncode})")
            res = json.loads(r.read_text())
    if not res["ok"]:
        if res.get("oom"):
            raise HTTPException(503, "the GPU is out of memory")
        raise HTTPException(500, res["error"])
    return res["result"]


async def _save(upload: UploadFile) -> Path:
    raw = await upload.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "file over 100 MB")
    if not raw:
        raise HTTPException(400, "empty file")
    p = INBOX / f"{uuid.uuid4().hex}{Path(upload.filename or 'x').suffix[:8]}"
    p.write_bytes(raw)
    return p


@app.get("/health")
async def health():
    from_models = Path(os.getenv("URVC_MODELS_DIR", "/models")) / "rvc" / "voice_models"
    return {"ok": True, "busy": state["busy"], "busy_for_s":
            round(time.time() - state["since"], 1) if state["since"] else None,
            "voices": len([p for p in from_models.glob("*") if p.is_dir() and
                           not p.name.startswith(".")])}


@app.post("/clips")
async def add_clip(voice: str = Form(...), clip: str = Form(...), file: UploadFile = File(...)):
    """Clean one recording into the voice's dataset (vocal isolation, de-reverb, silence)."""
    voice, clip = _id(voice, "voice"), _id(clip, "clip")
    src = await _save(file)
    try:
        return await _run("clean", {"voice": voice, "clip": clip, "src": str(src),
                                    "job": uuid.uuid4().hex})
    finally:
        src.unlink(missing_ok=True)


@app.get("/clips/{voice}/{clip}")
async def get_clip(voice: str, clip: str):
    p = DATA / "datasets" / _id(voice, "voice") / f"{_id(clip, 'clip')}.wav"
    if not p.exists():
        raise HTTPException(404, "no such clip")
    return FileResponse(p, media_type="audio/wav")


@app.delete("/clips/{voice}/{clip}")
async def delete_clip(voice: str, clip: str):
    (DATA / "datasets" / _id(voice, "voice") / f"{_id(clip, 'clip')}.wav").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/voices/{voice}/prepare")
async def prepare(voice: str):
    return await _run("prepare", {"voice": _id(voice, "voice")})


@app.post("/voices/{voice}/train_slice")
async def train_slice(voice: str, until: int = Form(...), batch_size: int = Form(8)):
    return await _run("train_slice", {"voice": _id(voice, "voice"), "until": until,
                                      "batch_size": batch_size})


@app.post("/voices/{voice}/finalize")
async def finalize(voice: str):
    return await _run("finalize", {"voice": _id(voice, "voice")})


@app.delete("/voices/{voice}")
async def delete_voice(voice: str):
    """Everything about a voice: dataset, training data, model."""
    voice = _id(voice, "voice")
    models = Path(os.getenv("URVC_MODELS_DIR", "/models")) / "rvc"
    async with gpu:
        for p in (DATA / "datasets" / voice, models / "training" / voice,
                  models / "voice_models" / voice):
            shutil.rmtree(p, ignore_errors=True)
    return {"ok": True}


def _out(job: str) -> Path:
    return OUT / _id(job, "job")


@app.post("/voices/{voice}/resing")
async def resing(voice: str, job: str = Form(...), file: UploadFile = File(...),
                 key: str = Form("auto"), target_mid_hz: float | None = Form(None),
                 keep_backing_vocals: bool = Form(True), vocal_db: int = Form(0),
                 music_db: int = Form(0), backing_db: int = Form(0), reverb: float = Form(0.2)):
    src = await _save(file)
    try:
        res = await _run("resing", {
            "voice": _id(voice, "voice"), "job": job, "src": str(src), "out_dir": str(_out(job)),
            "key": key, "target_mid_hz": target_mid_hz, "keep_backing_vocals": keep_backing_vocals,
            "vocal_db": vocal_db, "music_db": music_db, "backing_db": backing_db,
            "reverb": reverb})
    finally:
        src.unlink(missing_ok=True)
    return {**res, "files": sorted(p.name for p in _out(job).iterdir())}


@app.post("/voices/{voice}/convert")
async def convert(voice: str, job: str = Form(...), file: UploadFile = File(...),
                  key: str = Form("auto"), target_mid_hz: float | None = Form(None),
                  content: str = Form("singing")):
    src = await _save(file)
    try:
        res = await _run("convert", {
            "voice": _id(voice, "voice"), "job": job, "src": str(src), "out_dir": str(_out(job)),
            "key": key, "target_mid_hz": target_mid_hz,
            "content": "speech" if content == "speech" else "singing"})
    finally:
        src.unlink(missing_ok=True)
    return {**res, "files": sorted(p.name for p in _out(job).iterdir())}


@app.post("/separate")
async def separate(job: str = Form(...), file: UploadFile = File(...)):
    src = await _save(file)
    try:
        await _run("separate", {"job": job, "src": str(src), "out_dir": str(_out(job))})
    finally:
        src.unlink(missing_ok=True)
    return {"files": sorted(p.name for p in _out(job).iterdir())}


@app.get("/out/{job}/{name}")
async def get_out(job: str, name: str):
    p = _out(job) / Path(name).name
    if not p.exists():
        raise HTTPException(404, "no such file")
    return FileResponse(p)


@app.delete("/out/{job}")
async def drop_out(job: str):
    shutil.rmtree(_out(job), ignore_errors=True)
    return {"ok": True}
