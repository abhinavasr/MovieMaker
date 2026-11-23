"""Files a person attaches in the assistant chat: context for the planner, inputs for tools.

Each file is numbered per conversation (F1, F2, ...) and read once, in the
background, right after upload:

- picture -> imported as an ordinary studio picture (an `images` row, model
  "upload"), so its image_id works in every tool (sources, add_file, ...); the
  planner SEES it.
- audio   -> duration + transcript (CPU Whisper in the tts service).
- video   -> duration, three frames the planner sees, transcript of its sound.
- pdf / docx / text -> the text.

The planner is told what each file is, with an excerpt; `read_file` pages
through long text. In a tool argument "@F2" stands for the file: a *_base64
argument gets its bytes, an image argument gets its image_id, a text argument
gets its text or transcript. The bytes are substituted only at call time, so
the conversation log keeps "@F2", never megabytes of base64.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import subprocess
import zipfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app import db
from app.config import cfg

log = logging.getLogger("studio.agentfiles")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
TEXT_EXT = {".txt", ".md", ".markdown", ".srt", ".vtt", ".csv", ".tsv", ".json", ".html",
            ".htm", ".xml", ".yaml", ".yml", ".lrc", ".rtf", ".log"}
ACCEPT = sorted(IMAGE_EXT | AUDIO_EXT | VIDEO_EXT | TEXT_EXT | {".pdf", ".docx"})
MAX_FILES_PER_THREAD = 40
MAX_TEXT = 400_000              # stored per file
EXCERPT = 6000                  # shown to the planner per file per message
TRANSCRIBE_MAX_S = 600          # first 10 minutes of audio/video
REF = re.compile(r"^@F(\d+)$")
BASE64_ARGS = {"base64", "reference_base64", "source_base64"}
IMAGE_ARGS = {"image_ids", "image_id"}


def _dir(user_id: str) -> Path:
    d = cfg.DATA_DIR / "agent" / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def kind_of(name: str, mime: str) -> str | None:
    ext = Path(name or "").suffix.lower()
    if ext in IMAGE_EXT or (mime or "").startswith("image/"):
        return "image"
    if ext in AUDIO_EXT or (mime or "").startswith("audio/"):
        return "audio"
    if ext in VIDEO_EXT or (mime or "").startswith("video/"):
        return "video"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext in TEXT_EXT or (mime or "").startswith("text/"):
        return "text"
    return None


def files_of(thread_id: str) -> list:
    return db.query("SELECT * FROM agent_files WHERE thread_id=? ORDER BY num", (thread_id,))


def by_num(thread_id: str, num: int):
    return db.one("SELECT * FROM agent_files WHERE thread_id=? AND num=?", (thread_id, num))


def info(row) -> dict:
    try:
        return json.loads(row["info"] or "{}")
    except ValueError:
        return {}


async def save(user_id: str, thread_id: str, upload: UploadFile) -> dict:
    name = Path(upload.filename or "file").name[:120] or "file"
    kind = kind_of(name, upload.content_type or "")
    if kind is None:
        raise HTTPException(400, f"{name}: this kind of file isn't supported. Pictures, audio, "
                                 "video, PDF, Word (.docx) and text files are.")
    have = db.one("SELECT COUNT(*) AS n, MAX(num) AS m FROM agent_files WHERE thread_id=?",
                  (thread_id,))
    if have["n"] >= MAX_FILES_PER_THREAD:
        raise HTTPException(400, f"a conversation holds at most {MAX_FILES_PER_THREAD} files; "
                                 "start a new one")
    limit = cfg.AGENT_UPLOAD_MB * 1024 * 1024
    raw = await upload.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"{name} is over {cfg.AGENT_UPLOAD_MB} MB")
    if not raw:
        raise HTTPException(400, f"{name} is empty")
    fid = db.new_id("fil")
    path = _dir(user_id) / f"{fid}{Path(name).suffix.lower()[:8]}"
    path.write_bytes(raw)
    num = (have["m"] or 0) + 1
    now = db.now()
    db.run("INSERT INTO agent_files (id, thread_id, user_id, num, name, kind, mime, size, path, "
           "status, info, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (fid, thread_id, user_id, num, name, kind, upload.content_type or "", len(raw),
            str(path), "processing", "{}", now))
    asyncio.create_task(_process(fid))
    return view(db.one("SELECT * FROM agent_files WHERE id=?", (fid,)))


def view(row) -> dict:
    i = info(row)
    out = {"id": row["id"], "ref": f"F{row['num']}", "name": row["name"], "kind": row["kind"],
           "size": row["size"], "status": row["status"]}
    for k in ("image_id", "duration_s", "width", "height", "pages", "chars"):
        if i.get(k) is not None:
            out[k] = i[k]
    if row["error"]:
        out["error"] = row["error"]
    return out


# --------------------------------------------------------------------------- reading

def _run(cmd: list[str], timeout: int = 120, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, input=stdin)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-300:])
    return proc.stdout


def _probe(path: str) -> dict:
    out = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height", "-of", "json", path], 60)
    data = json.loads(out or b"{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"duration_s": round(float((data.get("format") or {}).get("duration") or 0), 1),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "width": video.get("width"), "height": video.get("height")}


async def _transcript(path: str) -> str:
    from app import music
    wav = await asyncio.to_thread(_run, ["ffmpeg", "-v", "error", "-i", path, "-t",
                                         str(TRANSCRIBE_MAX_S), "-vn", "-ac", "1", "-ar", "16000",
                                         "-f", "wav", "pipe:1"], 300)
    return await music.transcribe_source(wav)


def _frames(path: str, duration: float, dest: Path) -> list[str]:
    out = []
    for i, frac in enumerate((0.1, 0.5, 0.9)):
        jpg = dest.with_name(f"{dest.stem}.f{i}.jpg")
        try:
            _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, duration * frac):.2f}", "-i",
                  path, "-frames:v", "1", "-vf", "scale=640:-2", str(jpg)], 60)
            if jpg.exists() and jpg.stat().st_size:
                out.append(str(jpg))
        except Exception:                                      # noqa: BLE001
            pass
    return out


def _import_image(row) -> dict:
    from PIL import Image, ImageOps
    with Image.open(row["path"]) as img:
        img.seek(0)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        img.thumbnail((4096, 4096))
        image_id = db.new_id("img")
        dest = cfg.images_dir / row["user_id"]
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{image_id}.png"
        img.save(out, format="PNG")
        w, h = img.size
    now = db.now()
    db.run("INSERT INTO images (id, user_id, prompt, sources, status, progress, path, width, "
           "height, model, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (image_id, row["user_id"], f"uploaded in the assistant: {row['name']}", 0,
            "succeeded", 100, str(out), w, h, "upload", now, now))
    return {"image_id": image_id, "width": w, "height": h}


def _pdf_text(path: str) -> tuple[str, int]:
    from pypdf import PdfReader
    reader = PdfReader(path)
    parts = []
    for i, page in enumerate(reader.pages):
        parts.append(f"[page {i + 1}]\n{(page.extract_text() or '').strip()}")
        if sum(map(len, parts)) > MAX_TEXT:
            break
    return "\n\n".join(parts), len(reader.pages)


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def _plain(path: str) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


async def _process(fid: str) -> None:
    row = db.one("SELECT * FROM agent_files WHERE id=?", (fid,))
    if row is None:
        return
    i, text, error = {}, "", None
    try:
        kind = row["kind"]
        if kind == "image":
            i.update(await asyncio.to_thread(_import_image, row))
        elif kind in ("audio", "video"):
            i.update(await asyncio.to_thread(_probe, row["path"]))
            if kind == "video":
                i["frames"] = await asyncio.to_thread(
                    _frames, row["path"], i.get("duration_s") or 0, Path(row["path"]))
            if kind == "audio" or i.get("has_audio"):
                try:
                    text = await _transcript(row["path"])
                    i["transcribed"] = True
                    if i.get("duration_s", 0) > TRANSCRIBE_MAX_S:
                        i["transcript_note"] = f"first {TRANSCRIBE_MAX_S // 60} minutes only"
                except Exception as exc:                       # noqa: BLE001
                    i["transcript_note"] = f"no transcript ({getattr(exc, 'detail', exc)})"
        elif kind == "pdf":
            text, i["pages"] = await asyncio.to_thread(_pdf_text, row["path"])
            if not text.replace("[page", "").strip(" \n]0123456789"):
                i["note"] = "no text layer (a scanned PDF); only its pages as pictures would help"
        elif kind == "docx":
            text = await asyncio.to_thread(_docx_text, row["path"])
        else:
            text = await asyncio.to_thread(_plain, row["path"])
        text = text[:MAX_TEXT]
        if text:
            i["chars"] = len(text)
    except Exception as exc:                                   # noqa: BLE001
        log.exception("could not read attachment %s", fid)
        error = f"could not read this file ({type(exc).__name__})"
    db.run("UPDATE agent_files SET status=?, info=?, text=?, error=? WHERE id=?",
           ("failed" if error else "ready", json.dumps(i), text, error, fid))


async def wait_ready(thread_id: str, timeout: float = 600) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        row = db.one("SELECT COUNT(*) AS n FROM agent_files WHERE thread_id=? AND "
                     "status='processing'", (thread_id,))
        if not row["n"]:
            return
        await asyncio.sleep(1.5)


# --------------------------------------------------------------------------- for the models

def describe(row, excerpt: int = EXCERPT) -> str:
    """One attachment as the planner reads it."""
    i = info(row)
    head = f"F{row['num']} \"{row['name']}\" ({row['kind']}, {row['size'] // 1024} KB)"
    if row["status"] == "processing":
        return head + ": still being read"
    if row["status"] == "failed":
        return head + f": {row['error']}"
    bits = []
    if row["kind"] == "image":
        bits.append(f"studio picture image_id={i.get('image_id')} {i.get('width')}x{i.get('height')}"
                    " (you can see it)")
    if i.get("duration_s"):
        bits.append(f"{i['duration_s']} s")
    if row["kind"] == "video":
        bits.append(f"{i.get('width')}x{i.get('height')}, {len(i.get('frames') or [])} frames "
                    "attached for you to see")
    if i.get("pages"):
        bits.append(f"{i['pages']} pages")
    for k in ("transcript_note", "note"):
        if i.get(k):
            bits.append(i[k])
    text = row["text"] or ""
    out = head + (": " + "; ".join(bits) if bits else "")
    if text:
        label = "transcript" if row["kind"] in ("audio", "video") else "text"
        cut = text[:excerpt]
        more = (f"\n  [... {len(text) - excerpt} more characters; read_file(file=\"F{row['num']}\","
                f" start={excerpt}) for the rest]") if len(text) > excerpt else ""
        out += f"\n  {label}:\n" + "\n".join("  " + ln for ln in cut.splitlines()) + more
    return out


def pictures(row) -> list[bytes]:
    """JPEGs of an attachment for the planner's eyes."""
    i = info(row)
    if row["status"] != "ready":
        return []
    if row["kind"] == "image" and i.get("image_id"):
        img = db.one("SELECT path FROM images WHERE id=?", (i["image_id"],))
        if img and img["path"] and Path(img["path"]).exists():
            from app import images
            return [images.preview_jpeg(img["path"])]
    if row["kind"] == "video":
        return [Path(p).read_bytes() for p in i.get("frames") or [] if Path(p).exists()]
    return []


def materialize(thread_id: str, args, arg_name: str = ""):
    """Replace "@F<n>" with what the argument needs. Raises on an unknown or unready file."""
    if isinstance(args, dict):
        return {k: materialize(thread_id, v, k) for k, v in args.items()}
    if isinstance(args, list):
        return [materialize(thread_id, v, arg_name) for v in args]
    if not isinstance(args, str):
        return args
    m = REF.fullmatch(args.strip())
    if not m:
        return args
    row = by_num(thread_id, int(m.group(1)))
    if row is None:
        raise HTTPException(400, f"there is no attached file {args.strip()}")
    if row["status"] != "ready":
        raise HTTPException(409, f"{args.strip()} is {row['status']}: {row['error'] or ''}")
    i = info(row)
    if arg_name in BASE64_ARGS or arg_name.endswith("_base64"):
        return base64.b64encode(Path(row["path"]).read_bytes()).decode()
    if row["kind"] == "image" and i.get("image_id"):
        return i["image_id"]
    if row["text"]:
        return row["text"]
    raise HTTPException(400, f"{args.strip()} ({row['kind']}) can't be used as {arg_name}; "
                             "pass it to a *_base64 argument")


def filename_for(thread_id: str, args: dict) -> dict:
    """add_file needs a filename to tell a picture from audio when given base64."""
    raw = args.get("base64")
    if isinstance(raw, str):
        m = REF.fullmatch(raw.strip())
        if m and not args.get("filename"):
            row = by_num(thread_id, int(m.group(1)))
            if row is not None:
                return {**args, "filename": row["name"]}
    return args


def read_text(thread_id: str, ref: str, start: int = 0, length: int = 12000) -> dict:
    m = REF.fullmatch("@" + ref.strip().lstrip("@"))
    row = by_num(thread_id, int(m.group(1))) if m else None
    if row is None:
        raise HTTPException(404, f"no attached file {ref}")
    text = row["text"] or ""
    start = max(0, int(start or 0))
    length = max(500, min(20000, int(length or 12000)))
    return {"file": f"F{row['num']}", "name": row["name"], "start": start,
            "end": min(len(text), start + length), "total": len(text),
            "text": text[start:start + length]}


def delete_paths(rows) -> None:
    for row in rows:
        p = Path(row["path"])
        p.unlink(missing_ok=True)
        for f in info(row).get("frames") or []:
            Path(f).unlink(missing_ok=True)


def delete_for_thread(thread_id: str) -> None:
    delete_paths(files_of(thread_id))
    db.run("DELETE FROM agent_files WHERE thread_id=?", (thread_id,))
