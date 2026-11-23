"""GPU operations for the studio's voice service, one per process.

`server.py` runs each of these as `python ops.py <op> <args.json> <result.json>` so the
CUDA context, the separator and the RVC model are all freed when it exits — the card is
shared with WIS, songs and speech, and nothing may stay resident between jobs. The RVC
trainer also ends with os._exit(0), so it has to live in a child process anyway.

Everything is Ultimate RVC 0.6's own core API; see docs/VOICE_API_SPEC.md §10.
"""
from __future__ import annotations

import glob
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np

DATA = Path(os.getenv("RVC_DATA", "/data"))
DATASETS = DATA / "datasets"          # cleaned clips, per voice
WORK = DATA / "work"                  # per-job scratch, deleted after the job


# --------------------------------------------------------------------------- analysis

_rmvpe = None


def _pitch(wav16: np.ndarray) -> np.ndarray:
    """RMVPE f0 in Hz per 10 ms frame (0 = unvoiced). Accurate on singing."""
    global _rmvpe
    if _rmvpe is None:
        import torch
        from ultimate_rvc.common import RVC_MODELS_DIR
        from ultimate_rvc.rvc.lib.predictors.RMVPE import RMVPE0Predictor
        _rmvpe = RMVPE0Predictor(str(RVC_MODELS_DIR / "predictors" / "rmvpe.pt"),
                                 device="cuda" if torch.cuda.is_available() else "cpu")
    out = []
    step = 16000 * 30                  # 30 s windows keep memory flat on long clips
    for i in range(0, len(wav16), step):
        out.append(_rmvpe.infer_from_audio(wav16[i:i + step], thred=0.03))
    return np.concatenate(out) if out else np.zeros(0)


def _load16(path) -> np.ndarray:
    import librosa
    y, _ = librosa.load(str(path), sr=16000, mono=True)
    return y.astype(np.float32)


def _plateaus(f0: np.ndarray, min_frames=20, max_cents=60.0):
    """Runs of voiced frames that hold one pitch (a sung note): (start, end) indices."""
    runs, i, n = [], 0, len(f0)
    while i < n:
        if f0[i] <= 0:
            i += 1
            continue
        j, ref = i, f0[i]
        while j + 1 < n and f0[j + 1] > 0 and abs(1200 * math.log2(f0[j + 1] / ref)) < max_cents:
            j += 1
            ref = 0.9 * ref + 0.1 * f0[j]
        if j - i + 1 >= min_frames:
            runs.append((i, j + 1))
        i = j + 1
    return runs


def analyse(wav16: np.ndarray) -> dict:
    """What a clip contains: how much is sung vs spoken, and the pitch it lives at."""
    import librosa
    f0 = _pitch(wav16)
    voiced = f0 > 0
    frame_s = 0.01
    held = _plateaus(f0)
    held_s = sum(e - s for s, e in held) * frame_s
    voiced_s = float(voiced.sum()) * frame_s
    # Sung audio spends most voiced time on held notes; speech glides between pitches.
    singing_ratio = held_s / voiced_s if voiced_s else 0.0
    rms = librosa.feature.rms(y=wav16, frame_length=1024, hop_length=160)[0]
    db = 20 * np.log10(rms + 1e-9)
    loud = db > (db.max() - 40)
    out = {"duration_s": round(len(wav16) / 16000, 2), "voiced_s": round(voiced_s, 2),
           "held_s": round(held_s, 2), "singing_ratio": round(singing_ratio, 3),
           "noise_db": round(float(np.percentile(db, 10)), 1),
           "clipping": bool(np.mean(np.abs(wav16) > 0.99) > 1e-4),
           "usable_s": round(float(loud.sum()) * 0.01, 2)}
    if voiced.sum() > 50:
        v = f0[voiced]
        out["f0"] = {f"p{p}": round(float(np.percentile(v, p)), 1) for p in (5, 25, 50, 75, 95)}
    return out


NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note(hz: float) -> str:
    m = int(round(69 + 12 * math.log2(hz / 440.0)))
    return f"{NOTES[m % 12]}{m // 12 - 1}"


def voice_type(mid_hz: float) -> str:
    # By the median SUNG pitch (singers sit mid-range): bass < C#3, baritone < F3,
    # tenor < A#3, alto < D4, mezzo < F#4, soprano above.
    for name, top in (("bass", 140), ("baritone", 175), ("tenor", 230), ("alto", 295),
                      ("mezzo", 370)):
        if mid_hz < top:
            return name
    return "soprano"


def profile(paths: list[str]) -> dict:
    """Layer 2: range, vibrato and tone from the person's cleaned singing."""
    import librosa
    f0s, vib_rate, vib_ext, long_notes, with_vib, flat, cent, dyn = [], [], [], 0, 0, [], [], []
    best = (0.0, None)
    for p in paths:
        y = _load16(p)
        f0 = _pitch(y)
        f0s.append(f0[f0 > 0])
        for s, e in _plateaus(f0, min_frames=40, max_cents=120):
            seg = f0[s:e]
            cents = 1200 * np.log2(seg / np.median(seg))
            cents = cents - np.convolve(cents, np.ones(25) / 25, mode="same")   # drop the glide
            power = np.abs(np.fft.rfft(cents * np.hanning(len(cents)))) ** 2
            freqs = np.fft.rfftfreq(len(cents), 0.01)
            band = (freqs >= 4) & (freqs <= 8)
            share = power[band].sum() / (power[(freqs >= 0.5) & (freqs <= 15)].sum() + 1e-9)
            extent = math.sqrt(2) * float(np.std(cents))     # +/- cents of a sine wobble
            long_notes += 1
            # A vibrato note: most of the wobble sits at 4-8 Hz and it is audible (> 10 c).
            if band.any() and share > 0.5 and extent > 10:
                with_vib += 1
                vib_rate.append(float(freqs[band][power[band].argmax()]))
                vib_ext.append(extent)
        sf = librosa.feature.spectral_flatness(y=y, hop_length=160)[0]
        sc = librosa.feature.spectral_centroid(y=y, sr=16000, hop_length=160)[0]
        n = min(len(sf), len(f0))
        vmask = f0[:n] > 0
        if vmask.any():
            flat.append(float(np.median(sf[:n][vmask])))
            cent.append(float(np.median(sc[:n][vmask])))
        rms = librosa.feature.rms(y=y, hop_length=160)[0][:n]
        if vmask.any():
            d = 20 * np.log10(rms[vmask] + 1e-9)
            dyn.append(float(np.percentile(d, 90) - np.percentile(d, 10)))
        held = sum(e - s for s, e in _plateaus(f0)) * 0.01
        if held > best[0]:
            best = (held, p)
    allf = np.concatenate(f0s) if f0s else np.zeros(0)
    if len(allf) < 100:
        raise RuntimeError("no singing pitch found in the clips")
    pct = {p: float(np.percentile(allf, p)) for p in (5, 25, 50, 75, 95)}
    # Heuristic scales (not calibrated against labelled voices): spectral flatness of
    # voiced frames spans decades, so it is mapped on a log scale.
    breath = float(np.clip((math.log10(np.median(flat)) + 3.0) / 1.5, 0, 1)) if flat else None
    bright = float(np.clip((np.median(cent) - 900) / 1800, 0, 1)) if cent else None
    vib = {"rate_hz": round(float(np.median(vib_rate)), 2) if vib_rate else None,
           "extent_cents": round(float(np.median(vib_ext)), 1) if vib_ext else None,
           "share_of_long_notes": round(with_vib / long_notes, 2) if long_notes else 0.0}
    vt = voice_type(pct[50])
    words = [("breathy" if breath and breath > 0.55 else "clear" if breath is not None and breath < 0.3
              else "soft"), ("bright" if bright and bright > 0.6 else "warm" if bright is not None
                             and bright < 0.4 else ""), vt]
    cap = " ".join(w for w in words if w) + " voice"
    if vib["share_of_long_notes"] >= 0.4 and vib["rate_hz"]:
        speed = "quick" if vib["rate_hz"] >= 6 else "slow" if vib["rate_hz"] < 5 else "gentle"
        depth = "wide" if (vib["extent_cents"] or 0) > 60 else "light"
        cap += f", {depth} {speed} vibrato on held notes"
    elif long_notes:
        cap += ", straight tone with little vibrato"
    d = float(np.median(dyn)) if dyn else None
    if d is not None:
        cap += ", wide dynamics" if d > 18 else ", even dynamics" if d < 10 else ""
    return {"range": {"low_hz": round(pct[5], 1), "low_note": note(pct[5]),
                      "mid_hz": round(pct[50], 1), "mid_note": note(pct[50]),
                      "high_hz": round(pct[95], 1), "high_note": note(pct[95]),
                      "comfortable": [note(pct[25]), note(pct[75])]},
            "voice_type": vt, "vibrato": vib,
            "tone": {"breathiness": round(breath, 2) if breath is not None else None,
                     "brightness": round(bright, 2) if bright is not None else None,
                     "dynamics_db": round(d, 1) if d is not None else None},
            "style_caption": cap, "reference_clip": Path(best[1]).stem if best[1] else None}


# --------------------------------------------------------------------------- operations

def _sep(src, work, model, seg):
    from ultimate_rvc.core.generate.song_cover import separate_audio
    return separate_audio(src, work, model, seg)


def op_clean(a: dict) -> dict:
    """One uploaded clip -> dry lead vocal in the voice's dataset, plus its analysis."""
    from ultimate_rvc.typing_extra import SegmentSize, SeparationModel
    work = WORK / a["job"]
    work.mkdir(parents=True, exist_ok=True)
    try:
        src = work / "in.wav"
        _ffmpeg(a["src"], src, sr=44100)
        raw = analyse(_load16(src))
        vocals, music = _sep(src, work, SeparationModel.UVR_MDX_NET_VOC_FT, SegmentSize.SEG_512)
        _reverb, dry = _sep(vocals, work, SeparationModel.REVERB_HQ_BY_FOXJOY, SegmentSize.SEG_256)
        y = _load16(dry)
        m = _load16(music)
        v_rms = float(np.sqrt(np.mean(y ** 2)) + 1e-9)
        m_rms = float(np.sqrt(np.mean(m ** 2)) + 1e-9)
        out = DATASETS / a["voice"]
        out.mkdir(parents=True, exist_ok=True)
        dest = out / f"{a['clip']}.wav"
        _trim_silence(dry, dest)
        res = analyse(_load16(dest))
        res["music_removed"] = bool(20 * math.log10(m_rms / v_rms) > -20)
        res["original_s"] = raw["duration_s"]
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _ffmpeg(src, dst, sr=None, extra=()):
    import subprocess
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "2"]
    if sr:
        cmd += ["-ar", str(sr)]
    subprocess.run(cmd + list(extra) + [str(dst)], check=True)


def _trim_silence(src, dst):
    """Drop stretches quieter than -45 dB below the peak (kept gaps <= 0.4 s)."""
    import soundfile as sf
    y, sr = sf.read(str(src), always_2d=True)
    mono = y.mean(axis=1)
    hop = int(sr * 0.02)
    frames = len(mono) // hop
    if frames == 0:
        sf.write(str(dst), y, sr)
        return
    e = np.sqrt(np.mean(mono[:frames * hop].reshape(frames, hop) ** 2, axis=1))
    db = 20 * np.log10(e + 1e-9)
    keep = db > db.max() - 45
    # keep short gaps so phrases are not glued together
    k = keep.copy()
    gap = 0
    for i in range(frames):
        if keep[i]:
            if 0 < gap <= 20:
                k[i - gap:i] = True
            gap = 0
        else:
            gap += 1
    idx = np.repeat(k, hop)
    sf.write(str(dst), y[:frames * hop][idx], sr)


def op_prepare(a: dict) -> dict:
    """Preprocess the voice's cleaned clips and extract pitch/content features (GPU)."""
    from ultimate_rvc.core.train.extract import extract_features
    from ultimate_rvc.core.train.prepare import preprocess_dataset
    from ultimate_rvc.typing_extra import (AudioSplitMethod, EmbedderModel, F0Method,
                                           TrainingSampleRate)
    ds = DATASETS / a["voice"]
    files = sorted(glob.glob(str(ds / "*.wav")))
    if not files:
        raise RuntimeError("the voice has no clips")
    t0 = time.time()
    preprocess_dataset(a["voice"], ds, sample_rate=TrainingSampleRate.HZ_40K,
                       filter_audio=True, split_method=AudioSplitMethod.AUTOMATIC,
                       chunk_len=3.0, overlap_len=0.3, cpu_cores=min(8, os.cpu_count() or 4))
    extract_features(a["voice"], f0_method=F0Method.RMVPE, embedder_model=EmbedderModel.CONTENTVEC,
                     include_mutes=2, cpu_cores=min(8, os.cpu_count() or 4))
    return {"seconds": round(time.time() - t0, 1), "clips": len(files)}


def _training_dir(voice: str) -> Path:
    from ultimate_rvc.common import TRAINING_MODELS_DIR
    return TRAINING_MODELS_DIR / voice


def epochs_saved(voice: str) -> int:
    """The epoch the resumable checkpoint holds (0 = none yet)."""
    g = _training_dir(voice) / "G_2333333.pth"
    if not g.exists():
        return 0
    import torch
    return int(torch.load(str(g), map_location="cpu", weights_only=False).get("iteration", 0))


# The trainer saves when the ABSOLUTE epoch is a multiple of save_interval (not at the
# end of a run), so every slice must end on a multiple of this grain or its tail is lost.
GRAIN = 10


def op_train_slice(a: dict) -> dict:
    """Train from the last checkpoint up to `until` epochs (rounded up to GRAIN)."""
    from ultimate_rvc.core.train.train import run_training
    from ultimate_rvc.typing_extra import PrecisionType, Vocoder
    start = epochs_saved(a["voice"])
    until = -(-int(a["until"]) // GRAIN) * GRAIN
    if until <= start:
        return {"epochs_done": start, "seconds": 0.0, "stopped_early": False,
                "best": (_training_dir(a["voice"]) / f"{a['voice']}_best.pth").exists()}
    t0 = time.time()
    run_training(a["voice"], num_epochs=until, batch_size=int(a.get("batch_size", 8)),
                 save_interval=GRAIN, detect_overtraining=bool(a.get("detect_overtraining", True)),
                 overtraining_threshold=int(a.get("overtraining_threshold", 50)),
                 vocoder=Vocoder.HIFI_GAN, precision=PrecisionType.BF16)
    done = epochs_saved(a["voice"])
    # Overtraining detection ends the run itself, before `until`.
    stopped_early = done < until
    return {"epochs_done": done, "seconds": round(time.time() - t0, 1),
            "stopped_early": stopped_early,
            "best": (_training_dir(a["voice"]) / f"{a['voice']}_best.pth").exists()}


def op_finalize(a: dict) -> dict:
    """Install the best weights + index as a usable voice; drop training leftovers; profile."""
    from ultimate_rvc.common import VOICE_MODELS_DIR
    td = _training_dir(a["voice"])
    best = td / f"{a['voice']}_best.pth"
    index = sorted(td.glob("*.index"))
    if not best.exists():
        raise RuntimeError("training produced no model")
    if not index:
        from ultimate_rvc.rvc.train.process.extract_index import main as make_index
        make_index(str(td), "Auto")
        index = sorted(td.glob("*.index"))
    dest = VOICE_MODELS_DIR / a["voice"]
    tmp = VOICE_MODELS_DIR / f".{a['voice']}.new"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    shutil.copy2(best, tmp / f"{a['voice']}.pth")
    if index:
        shutil.copy2(index[-1], tmp / f"{a['voice']}.index")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    prof = profile(sorted(glob.glob(str(DATASETS / a["voice"] / "*.wav"))))
    if a.get("drop_training", True):
        # The cleaned clips in DATASETS are kept (a retrain re-extracts from them);
        # everything derived here is regenerated on the next prepare.
        for pat in ("G_*.pth", "D_*.pth", "eval", "sliced_audios*", "f0*", "*extracted",
                    "*.index", f"{a['voice']}_best.pth", "added_*", "trained_*"):
            for p in td.glob(pat):
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    return {"profile": prof, "model_mb": round((dest / f"{a['voice']}.pth").stat().st_size / 1e6, 1)}


def _median_f0(path) -> float | None:
    f0 = _pitch(_load16(path))
    v = f0[f0 > 0]
    return float(np.median(v)) if len(v) > 50 else None


def _shift(a: dict, src_mid: float | None) -> tuple[int, int]:
    """(octaves, semitones). 'auto' moves only the vocal, by whole octaves, toward the
    voice's median, so the song stays in key with its music."""
    key = a.get("key", "auto")
    if key == "auto":
        if not src_mid or not a.get("target_mid_hz"):
            return 0, 0
        # Measured (synthetic singer, resemblyzer): a male lead converted at its own pitch
        # stayed closer to the ORIGINAL singer (0.84 vs 0.79); one octave up it became
        # closer to the trained voice (0.84 vs 0.78). Pitch range dominates who it
        # sounds like, so move by whole octaves once the gap is >= 5 semitones.
        semis = 12 * math.log2(float(a["target_mid_hz"]) / src_mid)
        octaves = 0 if abs(semis) < 5 else int(math.copysign(max(1, round(abs(semis) / 12)), semis))
        return octaves, 0
    k = int(key)
    if k % 12 == 0:
        return k // 12, 0
    return 0, k


def op_resing(a: dict) -> dict:
    """Separate a song, re-sing its lead vocal in the voice, mix back on the original music."""
    from ultimate_rvc.core.generate.common import convert
    from ultimate_rvc.core.generate.song_cover import mix_song, pitch_shift, postprocess
    from ultimate_rvc.typing_extra import (AudioExt, EmbedderModel, F0Method, RVCContentType,
                                           SegmentSize, SeparationModel)
    work = WORK / a["job"]
    work.mkdir(parents=True, exist_ok=True)
    out = Path(a["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    try:
        src = work / "song.wav"
        _ffmpeg(a["src"], src, sr=44100)
        vocals, music = _sep(src, work, SeparationModel.UVR_MDX_NET_VOC_FT, SegmentSize.SEG_512)
        backing, lead = _sep(vocals, work, SeparationModel.UVR_MDX_NET_KARA_2, SegmentSize.SEG_512)
        _rev, dry = _sep(lead, work, SeparationModel.REVERB_HQ_BY_FOXJOY, SegmentSize.SEG_256)
        src_mid = _median_f0(dry)
        octaves, semis = _shift(a, src_mid)
        conv = convert(audio_track=dry, directory=work, model_name=a["voice"], n_octaves=octaves,
                       n_semitones=semis, f0_method=F0Method.RMVPE,
                       index_rate=float(a.get("index_rate", 0.75)), rms_mix_rate=0.25,
                       protect_rate=0.33, embedder_model=EmbedderModel.CONTENTVEC,
                       content_type=RVCContentType.VOCALS)
        rev = float(a.get("reverb", 0.2))
        wet = postprocess(conv, work, room_size=0.15 + 0.5 * rev, wet_level=rev,
                          dry_level=1.0 - 0.5 * rev, damping=0.7)
        m = pitch_shift(music, work, semis) if semis else music
        b = pitch_shift(backing, work, semis) if semis else backing
        tracks = [(wet, int(a.get("vocal_db", 0))), (m, int(a.get("music_db", 0)))]
        if a.get("keep_backing_vocals", True):
            tracks.append((b, int(a.get("backing_db", 0))))
        mixed = mix_song(tracks, work, 44100, AudioExt.MP3, "mix")
        shutil.copy2(mixed, out / "mix.mp3")
        for name, p in (("vocal", wet), ("music", m), ("backing", b)):
            _ffmpeg(p, out / f"{name}.mp3", extra=("-b:a", "192k"))
        return {"key_shift_semitones": 12 * octaves + semis, "source_mid_hz": src_mid}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def op_convert(a: dict) -> dict:
    """A dry vocal or speech clip -> the same performance in the voice (no separation)."""
    from ultimate_rvc.core.generate.common import convert
    from ultimate_rvc.typing_extra import EmbedderModel, F0Method, RVCContentType
    work = WORK / a["job"]
    work.mkdir(parents=True, exist_ok=True)
    out = Path(a["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    try:
        src = work / "in.wav"
        _ffmpeg(a["src"], src, sr=44100)
        octaves, semis = _shift(a, _median_f0(src) if a.get("key", "auto") == "auto" else None)
        speech = a.get("content") == "speech"
        conv = convert(audio_track=src, directory=work, model_name=a["voice"], n_octaves=octaves,
                       n_semitones=semis, f0_method=F0Method.RMVPE,
                       index_rate=float(a.get("index_rate", 0.75)),
                       rms_mix_rate=0.25 if not speech else 0.5, protect_rate=0.33,
                       embedder_model=EmbedderModel.CONTENTVEC,
                       content_type=RVCContentType.SPEECH if speech else RVCContentType.VOCALS)
        _ffmpeg(conv, out / "out.mp3", extra=("-b:a", "192k"))
        return {"key_shift_semitones": 12 * octaves + semis}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def op_separate(a: dict) -> dict:
    """A song -> lead vocal (dry), backing vocals, music. For the new-song pipeline."""
    from ultimate_rvc.typing_extra import SegmentSize, SeparationModel
    work = WORK / a["job"]
    work.mkdir(parents=True, exist_ok=True)
    out = Path(a["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    try:
        src = work / "song.wav"
        _ffmpeg(a["src"], src, sr=44100)
        vocals, music = _sep(src, work, SeparationModel.UVR_MDX_NET_VOC_FT, SegmentSize.SEG_512)
        backing, lead = _sep(vocals, work, SeparationModel.UVR_MDX_NET_KARA_2, SegmentSize.SEG_512)
        for name, p in (("lead", lead), ("backing", backing), ("music", music)):
            _ffmpeg(p, out / f"{name}.wav")
        return {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


OPS = {"clean": op_clean, "prepare": op_prepare, "train_slice": op_train_slice,
       "finalize": op_finalize, "resing": op_resing, "convert": op_convert,
       "separate": op_separate}

if __name__ == "__main__":
    op, args_path, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
    args = json.loads(Path(args_path).read_text())
    try:
        result = {"ok": True, "result": OPS[op](args)}
    except Exception as exc:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        text = f"{type(exc).__name__}: {exc}"
        oom = "out of memory" in text.lower() or "outofmemory" in text.lower()
        result = {"ok": False, "error": text[:800], "oom": oom}
    Path(result_path).write_text(json.dumps(result))
    sys.stdout.flush()
    os._exit(0)
