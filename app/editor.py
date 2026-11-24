"""The production editor: a story, scenes of shots, references, and a mixed timeline.

A PRODUCTION is a storyboard project with `params.editor = true`. Its shots (the
existing `shots` rows, so the render pipeline is the same one) are grouped into
SCENES (`prod_scenes`) and placed end to end: a shot starts where the previous
one ends. REFERENCES (`prod_refs`) are the people, places and voices the film
keeps returning to; a shot lists the ones in it, and they are sent along when
its picture or clip is made, which is what keeps a character the same person.

Each shot is made one of three ways:
- animate  -- minimax-h3-i2v: the shot's picture is the first frame. Silent.
- perform  -- minimax-h3-ref2v: the shot's picture and its references' pictures
              go in as <Picture n>, and the audio clips under the shot that are
              marked "use in video" go in as <Audio n>. It makes its own sound
              (the audio is a reference, not lip-synced exactly).
- lipsync  -- Wan2.2-S2V: the shot's picture and the first use-in-video
              DIALOGUE clip under it; the mouth follows that exact audio. The
              line stays on the dialogue track, so the shot's own copy of it is
              not mixed in again.

The TIMELINE has three audio tracks over the joined shots -- dialogue, ambience,
music -- plus the performance shots' own sound. A clip has a start, a trim, a
length, a gain and fades. The mix settings are per track (gain, mute) plus
music ducking under dialogue. Export joins the shots' clips and mixes it all
with ffmpeg on the CPU.

The assistant works on the same production through tools (see AGENT_TOOLS):
structural and mix edits arrive as PROPOSALS the person reviews and applies,
unless they let it apply changes directly; generating pictures, dialogue, music
and clips is done straight away because nothing is lost by it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app import auth, db, images, music, render, service, speech, wis
from app.config import cfg

log = logging.getLogger("studio.editor")
router = APIRouter()

TRACKS = ("dialogue", "ambience", "music")
REF_KINDS = ("character", "location", "prop", "style", "voice")
FRAMINGS = ("", "EWS", "WS", "MS", "MCU", "CU", "ECU", "OTS", "POV", "2S", "INSERT")
MAKES = ("animate", "perform", "lipsync")
DEFAULT_MIX = {"dialogue": {"gain_db": 0.0, "mute": False},
               "ambience": {"gain_db": -6.0, "mute": False},
               "music": {"gain_db": -8.0, "mute": False},
               "shot": {"gain_db": -4.0, "mute": False},
               "duck_db": -10.0}
MAX_SHOTS = 80
# The film's shape. The video models render three native 0.4 MP sizes on this
# card; "2.39:1" (scope) is rendered 16:9 and cropped to scope when exported, so
# compose with the middle of the frame. Value: (render orientation, crop ratio).
ASPECTS = {"9:16": ("portrait", None), "16:9": ("landscape", None),
           "1:1": ("square", None), "2.39:1": ("landscape", 2.39)}


def aspect_of(params: dict) -> str:
    a = params.get("aspect")
    if a in ASPECTS:
        return a
    return {"landscape": "16:9", "square": "1:1"}.get(params.get("orientation"), "9:16")
MAX_AUDIO_REFS = 3
MAX_IMAGE_REFS = 9
_exporting: set[str] = set()


# =========================================================================== access

def _user(request: Request) -> auth.User:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401)
    return user


def production(user: auth.User, project_id: str):
    project = service.owned_project(user, project_id)
    if project["mode"] != "storyboard":
        raise HTTPException(400, "the editor works on storyboard projects")
    params = db.params_of(project)
    if not params.get("editor"):
        params["editor"] = True
        db.set_params(project_id, params)
        _adopt(project)
    return db.one("SELECT * FROM projects WHERE id=?", (project_id,))


def _adopt(project) -> None:
    """Open an ordinary storyboard in the editor: its shots go into one scene."""
    pid = project["id"]
    if db.one("SELECT 1 FROM prod_scenes WHERE project_id=?", (pid,)):
        return
    scene_id = _new_scene(pid, "Scene 1", "", 0)
    db.run("UPDATE shots SET scene_id=? WHERE project_id=? AND scene_id IS NULL", (scene_id, pid))


def _mix(project) -> dict:
    mix = json.loads(json.dumps(DEFAULT_MIX))
    for k, v in (db.params_of(project).get("mix") or {}).items():
        if isinstance(v, dict) and isinstance(mix.get(k), dict):
            mix[k].update({kk: vv for kk, vv in v.items() if kk in ("gain_db", "mute")})
        elif k == "duck_db":
            mix[k] = float(v)
    return mix


def _touch(project_id: str) -> None:
    db.touch_project(project_id)


def _not_rendering(project) -> None:
    if service.is_rendering(project):
        raise HTTPException(409, "shots are rendering; wait for them, or cancel, before "
                                 "changing the shot list")


# =========================================================================== model

def scenes_of(project_id: str) -> list:
    return db.query("SELECT * FROM prod_scenes WHERE project_id=? ORDER BY idx", (project_id,))


def ordered_shots(project_id: str) -> list:
    """Shots in film order: by scene, then by position. Unscened shots go last."""
    order = {s["id"]: s["idx"] for s in scenes_of(project_id)}
    shots = db.query("SELECT * FROM shots WHERE project_id=?", (project_id,))
    return sorted(shots, key=lambda s: (order.get(s["scene_id"], 10 ** 6), s["idx"]))


def _renumber(project_id: str) -> None:
    """Keep shots.idx in film order: the render joins clips by idx."""
    for i, s in enumerate(ordered_shots(project_id)):
        if s["idx"] != i:
            db.run("UPDATE shots SET idx=? WHERE id=?", (i, s["id"]))


def timings(project_id: str) -> dict[str, tuple[float, float]]:
    t, out = 0.0, {}
    for s in ordered_shots(project_id):
        out[s["id"]] = (t, float(s["seconds"] or 0))
        t += float(s["seconds"] or 0)
    return out


def _new_scene(project_id: str, title: str, summary: str, idx: int) -> str:
    sid = db.new_id("scn")
    db.run("INSERT INTO prod_scenes (id, project_id, idx, title, summary, created_at) "
           "VALUES (?,?,?,?,?,?)", (sid, project_id, idx, title[:120], summary[:2000], db.now()))
    return sid


def _scene(project_id: str, scene_id: str):
    row = db.one("SELECT * FROM prod_scenes WHERE id=? AND project_id=?", (scene_id, project_id))
    if row is None:
        raise HTTPException(404, f"no scene {scene_id} in this production")
    return row


def _shot(project_id: str, shot_id: str):
    row = db.one("SELECT * FROM shots WHERE id=? AND project_id=?", (shot_id, project_id))
    if row is None:
        raise HTTPException(404, f"no shot {shot_id} in this production")
    return row


def _ref(project_id: str, ref_id: str):
    row = db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (ref_id, project_id))
    if row is None:
        raise HTTPException(404, f"no reference {ref_id} in this production")
    return row


def _clip(project_id: str, clip_id: str):
    row = db.one("SELECT * FROM prod_clips WHERE id=? AND project_id=?", (clip_id, project_id))
    if row is None:
        raise HTTPException(404, f"no audio clip {clip_id} in this production")
    return row


def _jl(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- edits
# Every edit goes through these, whether the person made it on the page, an
# applied proposal made it, or the assistant made it directly.

def create(user: auth.User, title: str, story: str = "", aspect: str = "9:16") -> str:
    pid = service.create_project(user, (title or "Untitled production")[:120], "storyboard",
                                 story or "")
    project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    params = db.params_of(project)
    params["editor"] = True
    db.set_params(pid, params)
    set_aspect(db.one("SELECT * FROM projects WHERE id=?", (pid,)), aspect)
    _new_scene(pid, "Scene 1", "", 0)
    return pid


def set_aspect(project, aspect: str) -> bool:
    """Change the film's shape. Returns True when existing clips no longer fit
    (they are marked changed so they render again at the new size)."""
    aspect = {"portrait": "9:16", "landscape": "16:9", "square": "1:1"}.get(aspect, aspect)
    if aspect not in ASPECTS:
        raise HTTPException(400, f"aspect must be one of {', '.join(ASPECTS)}")
    params = db.params_of(project)
    before = service.SIZES.get(params.get("orientation") or "portrait")
    orientation = ASPECTS[aspect][0]
    service.save_settings(project, orientation=orientation)
    params = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (project["id"],)))
    params["aspect"] = aspect
    db.set_params(project["id"], params)
    resized = before != service.SIZES[orientation]
    if resized:
        db.run("UPDATE shots SET status='stale' WHERE project_id=? AND status='done'",
               (project["id"],))
    _touch(project["id"])
    return resized


def set_story(project, title: str | None = None, story: str | None = None,
              aspect: str | None = None) -> None:
    service.save_settings(project, title=title, story=story)
    if aspect:
        set_aspect(db.one("SELECT * FROM projects WHERE id=?", (project["id"],)), aspect)


def add_scene(project, title: str = "", summary: str = "", position: int | None = None) -> str:
    pid = project["id"]
    scenes = scenes_of(pid)
    pos = len(scenes) if position is None else max(0, min(len(scenes), int(position)))
    for s in scenes[pos:]:
        db.run("UPDATE prod_scenes SET idx=? WHERE id=?", (s["idx"] + 1, s["id"]))
    sid = _new_scene(pid, title or f"Scene {len(scenes) + 1}", summary, pos)
    _reindex_scenes(pid)
    _touch(pid)
    return sid


def _reindex_scenes(pid: str) -> None:
    for i, s in enumerate(scenes_of(pid)):
        if s["idx"] != i:
            db.run("UPDATE prod_scenes SET idx=? WHERE id=?", (i, s["id"]))


def edit_scene(project, scene_id: str, title: str | None = None, summary: str | None = None,
               position: int | None = None) -> None:
    pid = project["id"]
    row = _scene(pid, scene_id)
    db.run("UPDATE prod_scenes SET title=?, summary=? WHERE id=?",
           ((title if title is not None else row["title"])[:120],
            (summary if summary is not None else row["summary"])[:2000], scene_id))
    if position is not None:
        _not_rendering(project)
        scenes = [s for s in scenes_of(pid) if s["id"] != scene_id]
        pos = max(0, min(len(scenes), int(position)))
        order = [s["id"] for s in scenes]
        order.insert(pos, scene_id)
        for i, s in enumerate(order):
            db.run("UPDATE prod_scenes SET idx=? WHERE id=?", (i, s))
        _renumber(pid)
    _touch(pid)


def delete_scene(project, scene_id: str) -> None:
    pid = project["id"]
    _not_rendering(project)
    _scene(pid, scene_id)
    for s in db.query("SELECT id FROM shots WHERE scene_id=?", (scene_id,)):
        _delete_shot_row(pid, s["id"])
    db.run("DELETE FROM prod_scenes WHERE id=?", (scene_id,))
    _reindex_scenes(pid)
    if not scenes_of(pid):
        _new_scene(pid, "Scene 1", "", 0)
    _renumber(pid)
    _touch(pid)


def add_shot(project, scene_id: str, title: str = "", prompt: str = "", seconds=None,
             framing: str = "", refs: list | None = None, make: str = "animate",
             position: int | None = None, image_id: str | None = None,
             user: auth.User | None = None, start_from: str = "") -> str:
    pid = project["id"]
    _not_rendering(project)
    _scene(pid, scene_id)
    if db.one("SELECT COUNT(*) AS n FROM shots WHERE project_id=?", (pid,))["n"] >= MAX_SHOTS:
        raise HTTPException(400, f"a production holds at most {MAX_SHOTS} shots")
    in_scene = db.query("SELECT * FROM shots WHERE scene_id=? ORDER BY idx", (scene_id,))
    pos = len(in_scene) if position is None else max(0, min(len(in_scene), int(position)))
    # Leave room in idx order: place before the shot currently at `pos`.
    base = in_scene[pos]["idx"] - 0.5 if pos < len(in_scene) else \
        (in_scene[-1]["idx"] + 0.5 if in_scene else 10 ** 5)
    shot_id = db.new_id("sht")
    params = db.params_of(project)
    make = make if make in MAKES else "animate"
    secs = _clamp(make, seconds if seconds is not None else
                  params.get("seconds", cfg.DEFAULT_SHOT_SECONDS))
    db.run("INSERT INTO shots (id, project_id, idx, title, prompt, seconds, scene_id, framing, "
           "refs, make, start_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
           (shot_id, pid, base, (title or "")[:80], prompt or "", secs, scene_id,
            framing if framing in FRAMINGS else "", json.dumps(_valid_refs(pid, refs or [])),
            make if make in MAKES else "animate", start_from if start_from in START_FROM else ""))
    _renumber(pid)
    if image_id and user is not None:
        set_picture(project, user, shot_id, image_id)
    _touch(pid)
    return shot_id


def cap_seconds(make: str) -> int:
    return {"perform": cfg.SINGLE_MAX_SECONDS, "lipsync": cfg.LIPSYNC_MAX_SECONDS}.get(
        make, cfg.STORYBOARD_MAX_SECONDS)


def _clamp(make: str, seconds) -> int:
    try:
        v = int(round(float(seconds)))
    except (TypeError, ValueError):
        v = cfg.DEFAULT_SHOT_SECONDS
    return max(1, min(cap_seconds(make), v))


def _valid_refs(pid: str, refs: list) -> list:
    have = {r["id"] for r in db.query("SELECT id FROM prod_refs WHERE project_id=?", (pid,))}
    return [r for r in refs if r in have][:MAX_IMAGE_REFS]


def edit_shot(project, shot_id: str, **fields) -> None:
    pid = project["id"]
    shot = _shot(pid, shot_id)
    sets: dict = {}
    if fields.get("title") is not None:
        sets["title"] = str(fields["title"])[:80]
    if fields.get("prompt") is not None:
        sets["prompt"] = str(fields["prompt"])[:4000]
    make = fields.get("make") if fields.get("make") in MAKES else shot["make"]
    if fields.get("make") in MAKES:
        sets["make"] = make
    if fields.get("seconds") is not None or "make" in sets:
        sets["seconds"] = _clamp(make, fields.get("seconds") or shot["seconds"])
    if fields.get("framing") is not None and fields["framing"] in FRAMINGS:
        sets["framing"] = fields["framing"]
    if fields.get("refs") is not None:
        sets["refs"] = json.dumps(_valid_refs(pid, list(fields["refs"])))
    if fields.get("start_from") is not None and fields["start_from"] in START_FROM:
        sets["start_from"] = fields["start_from"]
    changes_clip = any(k in sets for k in ("prompt", "seconds", "make", "refs", "start_from"))
    if changes_clip and shot["status"] == "done":
        sets["status"] = "stale"      # the clip no longer matches; render it again
    if fields.get("scene_id") is not None or fields.get("position") is not None:
        _not_rendering(project)
        scene_id = fields.get("scene_id") or shot["scene_id"]
        _scene(pid, scene_id)
        others = db.query("SELECT * FROM shots WHERE scene_id=? AND id!=? ORDER BY idx",
                          (scene_id, shot_id))
        pos = len(others) if fields.get("position") is None else \
            max(0, min(len(others), int(fields["position"])))
        sets["scene_id"] = scene_id
        sets["idx"] = others[pos]["idx"] - 0.5 if pos < len(others) else \
            (others[-1]["idx"] + 0.5 if others else 10 ** 5)
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        db.run(f"UPDATE shots SET {cols} WHERE id=?", (*sets.values(), shot_id))
        _renumber(pid)
        _touch(pid)


def _delete_shot_row(pid: str, shot_id: str) -> None:
    shot = db.one("SELECT * FROM shots WHERE id=?", (shot_id,))
    if shot and shot["clip_path"]:
        Path(shot["clip_path"]).unlink(missing_ok=True)
    db.run("DELETE FROM shots WHERE id=?", (shot_id,))


def delete_shot(project, shot_id: str) -> None:
    _not_rendering(project)
    _shot(project["id"], shot_id)
    _delete_shot_row(project["id"], shot_id)
    _renumber(project["id"])
    _touch(project["id"])


START_FROM = ("", "prev_last")


def grab_frame(clip_path: str, at) -> bytes:
    """One frame of a clip as PNG. `at` = seconds, or "last" for the final frame."""
    if not clip_path or not Path(clip_path).exists():
        raise HTTPException(400, "this shot has no rendered clip to take a frame from")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        if at in (None, "", "last"):
            # Decode the last half second, overwriting one image each frame: what is
            # left on disk is the clip's final frame.
            cmd = ["ffmpeg", "-v", "error", "-y", "-sseof", "-0.5", "-i", clip_path,
                   "-update", "1", str(out)]
        else:
            try:
                t = max(0.0, float(at))
            except (TypeError, ValueError):
                raise HTTPException(400, "at must be a number of seconds or \"last\"")
            cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", clip_path,
                   "-frames:v", "1", str(out)]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        data = out.read_bytes() if out.exists() else b""
    if proc.returncode != 0 or not data:
        raise HTTPException(400, "could not read a frame from the clip")
    return data


def chain_picture(project_id: str, shot_id: str, prev_clip) -> tuple[str, str]:
    """For render.py: a continuous shot's opening picture = the previous clip's last frame."""
    raw = grab_frame(str(prev_clip), "last")
    shot = db.one("SELECT asset_id FROM shots WHERE id=?", (shot_id,))
    asset_id = _store_picture(project_id, f"last-frame-of-previous.png", raw)
    db.run("UPDATE shots SET asset_id=? WHERE id=?", (asset_id, shot_id))
    if shot:
        _drop_unused_asset(project_id, shot["asset_id"])
    row = db.one("SELECT path FROM assets WHERE id=?", (asset_id,))
    return asset_id, row["path"]


def use_frame(project, user: auth.User, from_shot: str, at="last", to_shot: str | None = None,
              key_new: bool = False) -> dict:
    """Take a frame of one shot's clip as a shot's picture: `to_shot` (an id), the next
    shot (None), or a new shot right after the source (key_new)."""
    pid = project["id"]
    src = _shot(pid, from_shot)
    raw = grab_frame(src["clip_path"], at)
    label = "last frame" if at in (None, "", "last") else f"{float(at):.2f}s"
    image_id = _import_picture(user, f"{src['title'] or 'shot'} — {label}.png", raw)
    if key_new:
        order = [s["id"] for s in db.query("SELECT id FROM shots WHERE scene_id=? ORDER BY idx",
                                           (src["scene_id"],))]
        target = add_shot(project, src["scene_id"], title=f"{src['title'] or 'Shot'} (cont.)",
                          seconds=src["seconds"], framing=src["framing"], refs=_jl(src["refs"], []),
                          make="animate", position=order.index(src["id"]) + 1)
    elif to_shot:
        target = _shot(pid, to_shot)["id"]
    else:
        shots = ordered_shots(pid)
        i = [s["id"] for s in shots].index(src["id"])
        if i + 1 >= len(shots):
            order = [s["id"] for s in db.query("SELECT id FROM shots WHERE scene_id=? ORDER BY idx",
                                               (src["scene_id"],))]
            target = add_shot(project, src["scene_id"], title=f"{src['title'] or 'Shot'} (cont.)",
                              seconds=src["seconds"], framing=src["framing"],
                              refs=_jl(src["refs"], []), position=order.index(src["id"]) + 1)
        else:
            target = shots[i + 1]["id"]
    set_picture(db.one("SELECT * FROM projects WHERE id=?", (pid,)), user, target, image_id)
    return {"image_id": image_id, "shot_id": target}


def set_picture(project, user: auth.User, shot_id: str, image_id: str) -> None:
    pid = project["id"]
    shot = _shot(pid, shot_id)
    row = images.owned_image(user, image_id)
    if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(400, f"picture {image_id} is not finished yet")
    asset_id = _store_picture(pid, f"{image_id}.png", Path(row["path"]).read_bytes())
    db.run("UPDATE shots SET asset_id=?, status=CASE WHEN status='done' THEN 'stale' ELSE "
           "status END WHERE id=?", (asset_id, shot_id))
    _drop_unused_asset(pid, shot["asset_id"])
    _touch(pid)


def _store_picture(pid: str, name: str, raw: bytes) -> str:
    dest = cfg.uploads_dir / pid
    dest.mkdir(parents=True, exist_ok=True)
    asset_id = db.new_id("ast")
    path = dest / f"{asset_id}.png"
    path.write_bytes(raw)
    idx = (db.one("SELECT MAX(idx) AS m FROM assets WHERE project_id=?", (pid,))["m"] or 0) + 1
    db.run("INSERT INTO assets (id, project_id, kind, idx, path, orig_name, mime, created_at) "
           "VALUES (?,?,?,?,?,?,?,?)", (asset_id, pid, "image", idx, str(path), name[:120],
                                       "image/png", db.now()))
    return asset_id


def _drop_unused_asset(pid: str, asset_id: str | None) -> None:
    if not asset_id or db.one("SELECT 1 FROM shots WHERE asset_id=?", (asset_id,)):
        return
    row = db.one("SELECT path FROM assets WHERE id=? AND project_id=?", (asset_id, pid))
    if row:
        Path(row["path"]).unlink(missing_ok=True)
        db.run("DELETE FROM assets WHERE id=?", (asset_id,))


# --------------------------------------------------------------------------- references

def add_ref(project, kind: str, name: str, description: str = "", image_id: str | None = None,
            speaker: str | None = None, voice: bytes | None = None, voice_name: str = "",
            user: auth.User | None = None) -> str:
    pid = project["id"]
    if kind not in REF_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(REF_KINDS)}")
    if not name.strip():
        raise HTTPException(400, "give the reference a name")
    if image_id:
        if user is None:
            raise HTTPException(400, "no user")
        images.owned_image(user, image_id)
    if speaker and speaker not in speech.SPEAKERS:
        raise HTTPException(400, f"speaker must be one of {', '.join(speech.SPEAKERS)}")
    rid = db.new_id("ref")
    voice_path = _save_voice(pid, rid, voice, voice_name) if voice else None
    idx = (db.one("SELECT MAX(idx) AS m FROM prod_refs WHERE project_id=?", (pid,))["m"] or 0) + 1
    db.run("INSERT INTO prod_refs (id, project_id, idx, kind, name, description, image_id, "
           "voice_path, speaker, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (rid, pid, idx, kind, name.strip()[:80], description[:1500], image_id, voice_path,
            speaker or None, db.now()))
    _touch(pid)
    return rid


def _save_voice(pid: str, rid: str, raw: bytes, name: str) -> str:
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "a voice sample must be under 30 MB")
    dest = cfg.uploads_dir / pid / "voices"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{rid}{Path(name or 'voice.wav').suffix.lower()[:6] or '.wav'}"
    path.write_bytes(raw)
    return str(path)


def edit_ref(project, ref_id: str, user: auth.User | None = None, **f) -> None:
    pid = project["id"]
    row = _ref(pid, ref_id)
    sets = {}
    for k in ("name", "description"):
        if f.get(k) is not None:
            sets[k] = str(f[k])[:1500 if k == "description" else 80]
    if f.get("kind") in REF_KINDS:
        sets["kind"] = f["kind"]
    if f.get("image_id"):
        images.owned_image(user, f["image_id"])
        sets["image_id"] = f["image_id"]
    if f.get("speaker") is not None:
        if f["speaker"] and f["speaker"] not in speech.SPEAKERS:
            raise HTTPException(400, f"speaker must be one of {', '.join(speech.SPEAKERS)}")
        sets["speaker"] = f["speaker"] or None
    if f.get("voice") is not None:
        if row["voice_path"]:
            Path(row["voice_path"]).unlink(missing_ok=True)
        sets["voice_path"] = _save_voice(pid, ref_id, f["voice"], f.get("voice_name") or "")
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        db.run(f"UPDATE prod_refs SET {cols} WHERE id=?", (*sets.values(), ref_id))
        _touch(pid)


def delete_ref(project, ref_id: str) -> None:
    pid = project["id"]
    row = _ref(pid, ref_id)
    if row["voice_path"]:
        Path(row["voice_path"]).unlink(missing_ok=True)
    db.run("DELETE FROM prod_refs WHERE id=?", (ref_id,))
    for s in db.query("SELECT id, refs FROM shots WHERE project_id=?", (pid,)):
        refs = _jl(s["refs"], [])
        if ref_id in refs:
            db.run("UPDATE shots SET refs=? WHERE id=?",
                   (json.dumps([r for r in refs if r != ref_id]), s["id"]))
    db.run("UPDATE prod_clips SET ref_id=NULL WHERE ref_id=?", (ref_id,))
    _touch(pid)


# --------------------------------------------------------------------------- audio clips

def _probe_seconds(path: str) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, timeout=30)
        return round(float(out.stdout.decode().strip()), 2)
    except Exception:                                          # noqa: BLE001
        return None


def _insert_clip(pid: str, track: str, *, name: str, source_kind: str, source_id=None,
                 path=None, status="ready", start_s=0.0, gain_db=0.0, use_in_video=False,
                 ref_id=None, text="", duration_s=None, source_s=None) -> str:
    if track not in TRACKS:
        raise HTTPException(400, f"track must be one of {', '.join(TRACKS)}")
    cid = db.new_id("clp")
    now = db.now()
    db.run("INSERT INTO prod_clips (id, project_id, track, name, source_kind, source_id, path, "
           "status, start_s, gain_db, use_in_video, ref_id, text, duration_s, source_s, "
           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (cid, pid, track, name[:120], source_kind, source_id, path, status,
            max(0.0, float(start_s or 0)), float(gain_db or 0), 1 if use_in_video else 0,
            ref_id, text[:2000], duration_s, source_s, now, now))
    _touch(pid)
    return cid


async def add_dialogue(project, user: auth.User, ref_id: str | None, text: str, start_s: float,
                       instruct: str = "", language: str | None = None,
                       use_in_video: bool = True, speaker: str | None = None) -> str:
    """A line spoken by a character: their voice sample is cloned, or their preset
    speaker is used. Lands on the dialogue track when the speech is ready."""
    pid = project["id"]
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "what should they say?")
    ref = _ref(pid, ref_id) if ref_id else None
    reference = Path(ref["voice_path"]).read_bytes() if ref and ref["voice_path"] and \
        Path(ref["voice_path"]).exists() else None
    voice = speaker or (ref["speaker"] if ref else None)
    ids = await speech.create(user, text, speaker=None if reference else voice,
                              language=language, instruct=instruct or None,
                              reference=reference)
    name = f"{ref['name']}: {text}" if ref else text
    return _insert_clip(pid, "dialogue", name=name, source_kind="speech", source_id=ids[0],
                        status="pending", start_s=start_s, use_in_video=use_in_video,
                        ref_id=ref_id, text=text)


async def add_music(project, user: auth.User, description: str, start_s: float = 0.0,
                    duration: float | None = None, track: str = "music",
                    instrumental: bool = True, use_in_video: bool = False,
                    lyrics: str = "") -> str:
    """Music, or ambience/sound effects (the music model, asked for a soundscape)."""
    pid = project["id"]
    if track not in ("music", "ambience"):
        raise HTTPException(400, "track must be music or ambience")
    description = (description or "").strip()
    if not description:
        raise HTTPException(400, "describe the music or the sound")
    total = sum(float(s["seconds"] or 0) for s in ordered_shots(pid)) or 30
    dur = max(10, min(240, int(duration or min(120, total - start_s) or 30)))
    if track == "ambience":
        style = f"ambient soundscape, field recording, sound effects, no melody, no vocals: " \
                f"{description}"
        mid = await music.create(user, task="custom", style=style, instrumental=True,
                                 duration=dur, title=description[:60])
    elif lyrics:
        mid = await music.create(user, task="custom", style=description, lyrics=lyrics,
                                 duration=dur, title=description[:60])
    else:
        mid = await music.create(user, task="describe" if not instrumental else "custom",
                                 description=description if not instrumental else "",
                                 style=description if instrumental else "",
                                 instrumental=instrumental, duration=dur,
                                 title=description[:60])
    return _insert_clip(pid, track, name=description, source_kind="music", source_id=mid,
                        status="pending", start_s=start_s, use_in_video=use_in_video,
                        text=lyrics)


def add_existing(project, user: auth.User, track: str, *, speech_id: str | None = None,
                 music_id: str | None = None, start_s: float = 0.0,
                 use_in_video: bool = False) -> str:
    pid = project["id"]
    if speech_id:
        row = speech.owned(user, speech_id)
        return _insert_clip(pid, track, name=row["text"][:120], source_kind="speech",
                            source_id=speech_id, status="pending", start_s=start_s,
                            use_in_video=use_in_video, text=row["text"])
    if music_id:
        row = music.owned(user, music_id)
        return _insert_clip(pid, track, name=row["title"] or "song", source_kind="music",
                            source_id=music_id, status="pending", start_s=start_s,
                            use_in_video=use_in_video)
    raise HTTPException(400, "give speech_id or music_id")


def add_upload(project, track: str, name: str, raw: bytes, start_s: float = 0.0) -> str:
    pid = project["id"]
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(413, "audio files must be under 100 MB")
    dest = cfg.uploads_dir / pid / "audio"
    dest.mkdir(parents=True, exist_ok=True)
    cid_path = dest / f"{db.new_id('aud')}{Path(name).suffix.lower()[:6] or '.wav'}"
    cid_path.write_bytes(raw)
    secs = _probe_seconds(str(cid_path))
    if not secs:
        cid_path.unlink(missing_ok=True)
        raise HTTPException(400, f"{name} doesn't look like audio")
    return _insert_clip(pid, track, name=name, source_kind="upload", path=str(cid_path),
                        start_s=start_s, duration_s=secs, source_s=secs)


def edit_clip(project, clip_id: str, **f) -> None:
    pid = project["id"]
    row = _clip(pid, clip_id)
    sets = {}
    for k, lo, hi in (("start_s", 0, 36000), ("trim_s", 0, 36000), ("duration_s", 0.2, 36000),
                      ("gain_db", -60, 12), ("fade_in", 0, 60), ("fade_out", 0, 60)):
        if f.get(k) is not None:
            sets[k] = round(max(lo, min(hi, float(f[k]))), 3)
    if sets.get("trim_s") is not None and row["source_s"]:
        sets["trim_s"] = min(sets["trim_s"], max(0, row["source_s"] - 0.2))
    if sets.get("duration_s") is not None and row["source_s"]:
        trim = sets.get("trim_s", row["trim_s"])
        sets["duration_s"] = min(sets["duration_s"], row["source_s"] - trim)
    if f.get("use_in_video") is not None:
        sets["use_in_video"] = 1 if f["use_in_video"] else 0
    if f.get("track") in TRACKS:
        sets["track"] = f["track"]
    if f.get("name") is not None:
        sets["name"] = str(f["name"])[:120]
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        db.run(f"UPDATE prod_clips SET {cols}, updated_at=? WHERE id=?",
               (*sets.values(), db.now(), clip_id))
        _touch(pid)


def delete_clip(project, clip_id: str) -> None:
    pid = project["id"]
    row = _clip(pid, clip_id)
    if row["source_kind"] == "upload" and row["path"]:
        Path(row["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM prod_clips WHERE id=?", (clip_id,))
    _touch(pid)


def set_mix(project, **f) -> None:
    params = db.params_of(project)
    mix = _mix(project)
    for track in ("dialogue", "ambience", "music", "shot"):
        t = f.get(track)
        if isinstance(t, dict):
            if t.get("gain_db") is not None:
                mix[track]["gain_db"] = max(-60.0, min(12.0, float(t["gain_db"])))
            if t.get("mute") is not None:
                mix[track]["mute"] = bool(t["mute"])
    if f.get("duck_db") is not None:
        mix["duck_db"] = max(-30.0, min(0.0, float(f["duck_db"])))
    params["mix"] = mix
    db.set_params(project["id"], params)
    _touch(project["id"])


def refresh_clips(pid: str) -> None:
    """Pending dialogue/music clips: pick up the file once the speech or song is ready."""
    for c in db.query("SELECT * FROM prod_clips WHERE project_id=? AND status='pending'", (pid,)):
        table = "speech" if c["source_kind"] == "speech" else "music"
        row = db.one(f"SELECT status, path, error FROM {table} WHERE id=?", (c["source_id"],))
        if row is None:
            db.run("UPDATE prod_clips SET status='failed', error='the source is gone' WHERE id=?",
                   (c["id"],))
        elif row["status"] == "succeeded" and row["path"] and Path(row["path"]).exists():
            secs = _probe_seconds(row["path"]) or 0
            db.run("UPDATE prod_clips SET status='ready', path=?, source_s=?, duration_s=COALESCE("
                   "duration_s, ?), updated_at=? WHERE id=?",
                   (row["path"], secs, secs, db.now(), c["id"]))
        elif row["status"] in ("failed", "cancelled"):
            db.run("UPDATE prod_clips SET status='failed', error=?, updated_at=? WHERE id=?",
                   (row["error"] or row["status"], db.now(), c["id"]))


# --------------------------------------------------------------------------- generation

def _ref_sources(user: auth.User, pid: str, ref_ids: list) -> list[tuple[str, bytes]]:
    out = []
    for rid in ref_ids:
        r = db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (rid, pid))
        if r and r["image_id"]:
            try:
                out.append(images.source_from_image(user, r["image_id"]))
            except HTTPException:
                pass
    return out


def _shot_context(pid: str, shot) -> str:
    scene = db.one("SELECT * FROM prod_scenes WHERE id=?", (shot["scene_id"],)) \
        if shot["scene_id"] else None
    refs = [db.one("SELECT * FROM prod_refs WHERE id=?", (r,)) for r in _jl(shot["refs"], [])]
    who = "; ".join(f"{r['name']} ({r['kind']}): {r['description']}" for r in refs if r)
    parts = []
    if scene and scene["summary"]:
        parts.append(f"Scene: {scene['summary']}")
    if who:
        parts.append(f"In shot: {who}")
    return " ".join(parts)


async def picture_options(project, user: auth.User, shot_id: str, prompt: str = "",
                          count: int = 2, model: str | None = None) -> list[str]:
    """Pictures for a shot, made with its references as sources so characters stay
    the same people. The person picks one (or the assistant does)."""
    pid = project["id"]
    shot = _shot(pid, shot_id)
    count = max(1, min(4, int(count or 2)))
    framing = {"EWS": "extreme wide shot", "WS": "wide shot", "MS": "medium shot",
               "MCU": "medium close-up", "CU": "close-up", "ECU": "extreme close-up",
               "OTS": "over-the-shoulder shot", "POV": "point-of-view shot",
               "2S": "two-shot", "INSERT": "insert shot"}.get(shot["framing"], "")
    text = prompt.strip() or " ".join(x for x in (framing, shot["title"], shot["prompt"],
                                                  _shot_context(pid, shot)) if x)
    if not text:
        raise HTTPException(400, "describe the picture, or give the shot a title and prompt")
    sources = _ref_sources(user, pid, _jl(shot["refs"], []))[:4]
    if sources:
        names = [db.one("SELECT name FROM prod_refs WHERE id=?", (r,))["name"]
                 for r in _jl(shot["refs"], []) if db.one("SELECT image_id FROM prod_refs "
                                                         "WHERE id=?", (r,))["image_id"]][:4]
        text += " " + " ".join(f"{n} looks exactly like picture {i + 1}."
                               for i, n in enumerate(names))
    params = db.params_of(project)
    ids = []
    for i in range(count):
        ids.append(await images.create(user, text[:1900], sources,
                                       orientation=params.get("orientation", "portrait"),
                                       model=model))
    return ids


def render_shots(project, user: auth.User, shot_ids: list[str] | None = None):
    """Render the given shots now (all unrendered ones when None), in advance of
    the film; the film is joined when every shot has a clip."""
    pid = project["id"]
    if service.is_rendering(project):
        raise HTTPException(409, "shots are already rendering; they queue one production "
                                 "at a time")
    if service.rate_limited(user.id):
        raise HTTPException(429, f"you have started {cfg.USER_RENDERS_PER_HOUR} renders in the "
                                 "last hour, which is the limit for this account")
    shots = ordered_shots(pid)
    if not shots:
        raise HTTPException(400, "add shots first")
    want = shot_ids or [s["id"] for s in shots
                        if s["status"] != "done" or not (s["clip_path"] and
                                                         Path(s["clip_path"]).exists())]
    if not want:
        raise HTTPException(400, "every shot already has a clip; pick the ones to redo")
    by_id = {s["id"]: s for s in shots}
    pos = {s["id"]: i for i, s in enumerate(shots)}
    problems = []
    for sid in want:
        s = by_id.get(sid)
        if s is None:
            raise HTTPException(404, f"no shot {sid}")
        label = s["title"] or f"shot {s['idx'] + 1}"
        if s["start_from"] == "prev_last":
            prev = shots[pos[sid] - 1] if pos[sid] > 0 else None
            if prev is None:
                problems.append(f"{label} starts from the previous shot's last frame, but it is "
                                "the first shot")
            elif prev["id"] not in want and not (prev["clip_path"] and
                                                 Path(prev["clip_path"]).exists()):
                problems.append(f"{label} starts from the last frame of "
                                f"{prev['title'] or 'the previous shot'}, which isn't rendered — "
                                "render both")
        elif not s["asset_id"]:
            problems.append(f"{label} has no picture")
        if not (s["prompt"] or "").strip():
            problems.append(f"{label} has no prompt (what moves, how the camera moves)")
        cap = cap_seconds(s["make"])
        if int(s["seconds"] or 0) > cap:
            problems.append(f"{label} is longer than {cap} s")
        if s["make"] == "lipsync" and not _dialogue_under(pid, s["id"]):
            problems.append(f"{label} is a lip-sync shot with no dialogue under it marked "
                            "\"use in video\" — add a line on the dialogue track first")
    if problems:
        raise HTTPException(400, "; ".join(problems))
    marks = ",".join("?" * len(want))
    db.run(f"UPDATE shots SET status='pending', clip_path=NULL, error=NULL, wis_job_id=NULL "
           f"WHERE id IN ({marks})", tuple(want))
    db.run("INSERT INTO renders (id,user_id,project_id,created_at) VALUES (?,?,?,?)",
           (db.new_id("rnd"), user.id, pid, db.now()))
    return render.enqueue(pid, set(want), partial=True)


def _dialogue_under(pid: str, shot_id: str) -> list:
    start, length = timings(pid).get(shot_id, (0.0, 0.0))
    return [c for c in db.query("SELECT * FROM prod_clips WHERE project_id=? AND track='dialogue' "
                                "AND status='ready' AND use_in_video=1 ORDER BY start_s", (pid,))
            if c["path"] and c["start_s"] < start + length and
            c["start_s"] + (c["duration_s"] or 0) > start]


def perform_payload(project_id: str, shot, image_path: str | None, params: dict) -> dict:
    """The WIS request for a performance or lip-sync shot (called by app/render.py)."""
    pid = project_id
    if shot.get("make") == "lipsync":
        lines = _dialogue_under(pid, shot["id"])
        if not image_path or not lines:
            raise RuntimeError(f"{shot.get('title') or 'a lip-sync shot'} needs a picture and a "
                               "use-in-video dialogue line under it")
        start, length = timings(pid).get(shot["id"], (0.0, float(shot.get("seconds") or 5)))
        ctx = _shot_context(pid, shot)
        return {"model": cfg.LIPSYNC_MODEL,
                "prompt": " ".join(x for x in (shot.get("prompt") or "", ctx) if x)[:3900],
                "seconds": min(cfg.LIPSYNC_MAX_SECONDS, int(shot.get("seconds") or 5)),
                "width": int(params.get("width", 480)), "height": int(params.get("height", 864)),
                "client_request_id": shot["id"],
                "image": {"b64": wis.b64(Path(image_path).read_bytes()), "mime": "image/png"},
                # The whole shot window, silence included, so the mouth is still
                # where the line has not started yet.
                "reference_audio": [{"b64": wis.b64(_window(lines, start, length)),
                                     "mime": "audio/wav"}]}
    refs = [db.one("SELECT * FROM prod_refs WHERE id=?", (r,)) for r in _jl(shot.get("refs"), [])]
    refs = [r for r in refs if r is not None]
    pics: list[tuple[str, str]] = []           # (label, path)
    if image_path:
        pics.append(("the opening frame", image_path))
    for r in refs:
        if r["image_id"]:
            row = db.one("SELECT path FROM images WHERE id=?", (r["image_id"],))
            if row and row["path"] and Path(row["path"]).exists():
                pics.append((r["name"], row["path"]))
    pics = pics[:MAX_IMAGE_REFS]
    start, length = timings(pid).get(shot["id"], (0.0, float(shot.get("seconds") or 5)))
    under = [c for c in db.query("SELECT * FROM prod_clips WHERE project_id=? AND status='ready' "
                                 "AND use_in_video=1", (pid,))
             if c["start_s"] < start + length and
             c["start_s"] + (c["duration_s"] or 0) > start and c["path"]]
    under.sort(key=lambda c: (c["track"] != "dialogue", c["start_s"]))
    under = under[:MAX_AUDIO_REFS]

    lines = []
    tag = {}
    for i, (label, _) in enumerate(pics):
        tag[label] = f"<Picture {i + 1}>"
        lines.append(f"<Picture {i + 1}> is {label}." if label != "the opening frame"
                     else "<Picture 1> is how the shot looks.")
    for i, c in enumerate(under):
        who = db.one("SELECT name FROM prod_refs WHERE id=?", (c["ref_id"],)) if c["ref_id"] else None
        if c["track"] == "dialogue" and who:
            speaker = tag.get(who["name"], who["name"])
            lines.append(f"{speaker} says \"{c['text']}\" in the voice of <Audio {i + 1}>.")
        elif c["track"] == "dialogue":
            lines.append(f"Someone says \"{c['text']}\", as in <Audio {i + 1}>.")
        else:
            lines.append(f"The {c['track']} sounds like <Audio {i + 1}>.")
    ctx = _shot_context(pid, shot)
    prompt = " ".join([shot.get("prompt") or "", *lines, ctx]).strip()
    payload = {
        "model": cfg.SINGLE_MODEL, "prompt": prompt[:3900],
        "seconds": min(cfg.SINGLE_MAX_SECONDS, int(shot.get("seconds") or 5)),
        "width": int(params.get("width", 480)), "height": int(params.get("height", 864)),
        "ref_image_size": params.get("ref_image_size", "match"),
        "client_request_id": shot["id"],
        "reference_images": [{"b64": wis.b64(Path(p).read_bytes()), "mime": "image/png"}
                             for _, p in pics],
    }
    if under:
        payload["reference_audio"] = [{"b64": wis.b64(_cut(c, start, length)), "mime": "audio/wav"}
                                      for c in under]
    if params.get("upscale") in ("latent", "refine"):
        payload["upscale"] = params["upscale"]
    return payload


def _window(lines: list, start: float, length: float) -> bytes:
    """The dialogue under a shot as one WAV exactly as long as the shot."""
    inputs, parts = [], []
    for i, c in enumerate(lines):
        a = max(start, c["start_s"])
        b = min(start + length, c["start_s"] + (c["duration_s"] or 0))
        if b <= a:
            continue
        offset = c["trim_s"] + (a - c["start_s"])
        inputs += ["-i", c["path"]]
        parts.append(f"[{len(parts)}:a]atrim=start={offset:.3f}:duration={b - a:.3f},"
                     f"asetpts=PTS-STARTPTS,aresample=44100,aformat=channel_layouts=mono,"
                     f"adelay={int((a - start) * 1000)}:all=1[p{len(parts)}]")
    n = len(parts)
    graph = ";".join(parts) + ";" + "".join(f"[p{i}]" for i in range(n)) + \
        (f"amix=inputs={n}:normalize=0," if n > 1 else "anull,") + \
        f"apad=whole_dur={length:.3f},atrim=duration={length:.3f}[out]"
    proc = subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex", graph,
                           "-map", "[out]", "-ac", "1", "-ar", "44100", "-f", "wav", "pipe:1"],
                          capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("could not cut the dialogue for the lip-sync shot: " +
                           proc.stderr.decode(errors="replace")[-300:])
    return proc.stdout


def _cut(clip, shot_start: float, shot_len: float) -> bytes:
    """The part of a clip that plays under the shot, as WAV."""
    a = max(shot_start, clip["start_s"])
    b = min(shot_start + shot_len, clip["start_s"] + (clip["duration_s"] or 0))
    offset = clip["trim_s"] + (a - clip["start_s"])
    proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{offset:.3f}", "-t",
                           f"{max(0.5, b - a):.3f}", "-i", clip["path"], "-ac", "2", "-ar",
                           "44100", "-f", "wav", "pipe:1"], capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        return Path(clip["path"]).read_bytes()
    return proc.stdout


# --------------------------------------------------------------------------- export (mix)

def _db_to_amp(gain_db: float) -> float:
    return 10 ** (gain_db / 20)


def export_path(pid: str) -> Path:
    return cfg.outputs_dir / pid / "film_mix.mp4"


def _mix_film(project) -> Path:
    """Join the shot clips and mix the timeline over them. CPU only."""
    pid = project["id"]
    params = db.params_of(project)
    mix = _mix(project)
    shots = ordered_shots(pid)
    missing = [s["title"] or f"shot {i + 1}" for i, s in enumerate(shots)
               if not (s["clip_path"] and Path(s["clip_path"]).exists()) or
               s["status"] not in ("done",)]
    if missing:
        raise HTTPException(400, f"{len(missing)} shot(s) have no up-to-date clip yet: "
                                 f"{', '.join(missing[:6])}. Render them first.")
    out = export_path(pid)
    work = out.parent / "mixwork"
    work.mkdir(parents=True, exist_ok=True)
    w, h = int(params.get("width", 480)), int(params.get("height", 864))
    k = 2 if params.get("upscale") in ("latent", "refine") else 1
    total = sum(float(s["seconds"] or 0) for s in shots)

    # 1) picture: every clip trimmed/padded to its shot length, scaled, joined.
    inputs, filters = [], []
    for i, s in enumerate(shots):
        inputs += ["-i", s["clip_path"]]
        filters.append(f"[{i}:v]scale={w * k}:{h * k}:flags=lanczos,setsar=1,fps=24,"
                       f"tpad=stop_mode=clone:stop_duration=30,trim=duration={s['seconds']},"
                       f"setpts=PTS-STARTPTS[v{i}]")
    crop = ASPECTS[aspect_of(params)][1]
    tail = f",crop=iw:trunc(iw/{crop}/2)*2" if crop else ""
    filters.append("".join(f"[v{i}]" for i in range(len(shots))) +
                   f"concat=n={len(shots)}:v=1:a=0{tail}[vout]")

    # 2) sound: the performance shots' own audio, then every timeline clip.
    n = len(shots)
    labels: dict[str, list[str]] = {"dialogue": [], "ambience": [], "music": [], "shot": []}
    t = 0.0
    for i, s in enumerate(shots):
        # Lip-sync shots carry the dialogue line, which is already on its track.
        if s["make"] == "perform" and not mix["shot"]["mute"] and _has_audio(s["clip_path"]):
            lab = f"sa{i}"
            filters.append(f"[{i}:a]atrim=duration={s['seconds']},asetpts=PTS-STARTPTS,"
                           f"aresample=48000,adelay={int(t * 1000)}:all=1[{lab}]")
            labels["shot"].append(lab)
        t += float(s["seconds"] or 0)
    clips = db.query("SELECT * FROM prod_clips WHERE project_id=? AND status='ready' "
                     "ORDER BY start_s", (pid,))
    for c in clips:
        if mix[c["track"]]["mute"] or not c["path"] or not Path(c["path"]).exists():
            continue
        if c["start_s"] >= total:
            continue
        dur = min(c["duration_s"] or c["source_s"] or 0, total - c["start_s"])
        if dur <= 0:
            continue
        inputs += ["-i", c["path"]]
        idx = n
        n += 1
        chain = [f"atrim=start={c['trim_s']}:duration={dur}", "asetpts=PTS-STARTPTS",
                 "aresample=48000", "aformat=channel_layouts=stereo",
                 f"volume={_db_to_amp(c['gain_db']):.4f}"]
        if c["fade_in"] > 0:
            chain.append(f"afade=t=in:st=0:d={min(c['fade_in'], dur)}")
        if c["fade_out"] > 0:
            chain.append(f"afade=t=out:st={max(0, dur - c['fade_out'])}:d={min(c['fade_out'], dur)}")
        chain.append(f"adelay={int(c['start_s'] * 1000)}:all=1")
        lab = f"c{idx}"
        filters.append(f"[{idx}:a]" + ",".join(chain) + f"[{lab}]")
        labels[c["track"]].append(lab)

    buses = []
    for track in ("dialogue", "ambience", "music", "shot"):
        labs = labels[track]
        if not labs:
            continue
        gain = _db_to_amp(mix[track]["gain_db"])
        bus = f"bus_{track}"
        if len(labs) == 1:
            filters.append(f"[{labs[0]}]volume={gain:.4f},apad=whole_dur={total}[{bus}]")
        else:
            filters.append("".join(f"[{x}]" for x in labs) +
                           f"amix=inputs={len(labs)}:normalize=0:duration=longest,"
                           f"volume={gain:.4f},apad=whole_dur={total}[{bus}]")
        buses.append(bus)
    # Ducking: music and ambience dip under dialogue.
    if "bus_dialogue" in buses and mix["duck_db"] < -0.5:
        ratio = max(2, min(20, abs(mix["duck_db"]) / 1.5))
        filters.append("[bus_dialogue]asplit=2[dlg][dkey]")
        ducked = []
        dkeys = [b for b in buses if b in ("bus_music", "bus_ambience")]
        if dkeys:
            filters.append(f"[dkey]asplit={len(dkeys)}" + "".join(f"[k{i}]" for i in range(len(dkeys)))
                           if len(dkeys) > 1 else "[dkey]anull[k0]")
            for i, b in enumerate(dkeys):
                filters.append(f"[{b}][k{i}]sidechaincompress=threshold=0.03:ratio={ratio:.1f}:"
                               f"attack=20:release=350[d_{b}]")
                ducked.append(f"d_{b}")
            buses = ["dlg"] + ducked + [b for b in buses if b not in dkeys and b != "bus_dialogue"]
        else:
            filters.append("[dkey]anullsink")
            buses = ["dlg"] + [b for b in buses if b != "bus_dialogue"]
    audio = None
    if buses:
        filters.append("".join(f"[{b}]" for b in buses) +
                       f"amix=inputs={len(buses)}:normalize=0:duration=longest,"
                       f"atrim=duration={total},alimiter=limit=0.95[aout]")
        audio = "[aout]"
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters),
           "-map", "[vout]"]
    if audio:
        cmd += ["-map", audio, "-c:a", "aac", "-b:a", "192k"]
    tmp = out.with_suffix(".part.mp4")
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-t", f"{total:.3f}", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600)
    shutil.rmtree(work, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-800:])
    tmp.replace(out)
    return out


def _has_audio(path: str) -> bool:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                              "stream=index", "-of", "csv=p=0", path], capture_output=True,
                             timeout=30)
        return bool(out.stdout.strip())
    except Exception:                                          # noqa: BLE001
        return False


async def export(project) -> None:
    pid = project["id"]
    if pid in _exporting:
        raise HTTPException(409, "already exporting")
    _mix_check = ordered_shots(pid)
    if not _mix_check:
        raise HTTPException(400, "add shots first")
    _exporting.add(pid)
    params = db.params_of(project)
    params["_export"] = {"status": "running", "at": db.now()}
    db.set_params(pid, params)

    async def go():
        try:
            out = await asyncio.to_thread(_mix_film, db.one("SELECT * FROM projects WHERE id=?",
                                                            (pid,)))
            state = {"status": "done", "at": db.now(), "path": str(out)}
        except HTTPException as exc:
            state = {"status": "failed", "at": db.now(), "error": str(exc.detail)}
        except Exception as exc:                               # noqa: BLE001
            log.exception("export %s failed", pid)
            state = {"status": "failed", "at": db.now(), "error": str(exc)[:600]}
        finally:
            _exporting.discard(pid)
        p = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (pid,)))
        p["_export"] = state
        db.set_params(pid, p)
        _touch(pid)
    # Check the shots synchronously so the person gets the reason straight away.
    missing = [s for s in _mix_check if not (s["clip_path"] and Path(s["clip_path"]).exists())
               or s["status"] != "done"]
    if missing:
        _exporting.discard(pid)
        params["_export"] = {"status": "failed", "at": db.now(),
                             "error": f"{len(missing)} shot(s) need rendering first"}
        db.set_params(pid, params)
        raise HTTPException(400, f"{len(missing)} shot(s) have no up-to-date clip yet; render "
                                 "them first (Render missing shots)")
    asyncio.create_task(go())


# =========================================================================== proposals

def propose(project, title: str, summary: str, changes: list, shot_id: str | None = None,
            options: list | None = None) -> str:
    pid = project["id"]
    _validate_changes(changes)
    prop_id = db.new_id("prp")
    now = db.now()
    db.run("INSERT INTO proposals (id, project_id, shot_id, title, summary, changes, options, "
           "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (prop_id, pid, shot_id, title[:120], summary[:2000], json.dumps(changes),
            json.dumps(options or []), "open", now, now))
    _touch(pid)
    return prop_id


OPS = {"set_story", "add_scene", "edit_scene", "delete_scene", "add_shot", "edit_shot",
       "delete_shot", "edit_clip", "delete_clip", "set_mix", "add_ref", "edit_ref", "use_frame"}


def _validate_changes(changes) -> None:
    if not isinstance(changes, list):
        raise HTTPException(400, "changes must be a list of {op, ...}")
    for c in changes:
        if not isinstance(c, dict) or c.get("op") not in OPS:
            raise HTTPException(400, f"each change needs an op, one of {', '.join(sorted(OPS))}")


_FRAMING_WORDS = {"extreme wide": "EWS", "wide": "WS", "medium close": "MCU", "medium": "MS",
                  "extreme close": "ECU", "close": "CU", "over the shoulder": "OTS",
                  "over-the-shoulder": "OTS", "point of view": "POV", "two-shot": "2S",
                  "two shot": "2S", "insert": "INSERT"}


def _normalise(c: dict) -> dict:
    """Accept the near-misses a model writes: `type`/`mode` for make, words for framing."""
    c = dict(c)
    if c.get("op") in ("add_shot", "edit_shot"):
        for alias in ("type", "mode", "kind"):
            if c.get(alias) in MAKES and not c.get("make"):
                c["make"] = c.pop(alias)
        f = c.get("framing")
        if isinstance(f, str) and f.upper() not in FRAMINGS:
            low = f.lower()
            c["framing"] = next((v for k, v in _FRAMING_WORDS.items() if k in low), "")
        elif isinstance(f, str):
            c["framing"] = f.upper()
    return c


def apply_changes(project, user: auth.User, changes: list) -> dict:
    """Run a list of edits. New scenes/shots can be named with `key` and referred
    to by that key later in the same list (e.g. add_scene key=s1, add_shot scene=s1)."""
    keys: dict[str, str] = {}
    pid = project["id"]

    def rid(v):
        return keys.get(v, v) if isinstance(v, str) else v

    done = []
    for c in changes:
        project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        c = _normalise(c)
        op = c.get("op")
        if op == "set_story":
            set_story(project, title=c.get("title"), story=c.get("story"), aspect=c.get("aspect"))
        elif op == "add_scene":
            keys[c.get("key") or f"scene{len(keys)}"] = add_scene(
                project, c.get("title", ""), c.get("summary", ""), c.get("position"))
        elif op == "edit_scene":
            edit_scene(project, rid(c.get("scene_id")), c.get("title"), c.get("summary"),
                       c.get("position"))
        elif op == "delete_scene":
            delete_scene(project, rid(c.get("scene_id")))
        elif op == "add_shot":
            keys[c.get("key") or f"shot{len(keys)}"] = add_shot(
                project, rid(c.get("scene_id")), c.get("title", ""), c.get("prompt", ""),
                c.get("seconds"), c.get("framing", ""), [rid(r) for r in c.get("refs") or []],
                c.get("make", "animate"), c.get("position"), c.get("image_id"), user,
                c.get("start_from", ""))
        elif op == "edit_shot":
            fields = {k: c.get(k) for k in ("title", "prompt", "seconds", "framing", "refs",
                                            "make", "position", "start_from") if k in c}
            if "scene_id" in c:
                fields["scene_id"] = rid(c["scene_id"])
            if fields.get("refs") is not None:
                fields["refs"] = [rid(r) for r in fields["refs"]]
            edit_shot(project, rid(c.get("shot_id")), **fields)
            if c.get("image_id"):
                set_picture(project, user, rid(c["shot_id"]), c["image_id"])
        elif op == "delete_shot":
            delete_shot(project, rid(c.get("shot_id")))
        elif op == "use_frame":
            r = use_frame(project, user, rid(c.get("from_shot_id")), c.get("at", "last"),
                          rid(c.get("to_shot_id")) if c.get("to_shot_id") else None,
                          key_new=bool(c.get("new_shot")))
            if c.get("key"):
                keys[c["key"]] = r["shot_id"]
        elif op == "edit_clip":
            edit_clip(project, c.get("clip_id"), **{k: c.get(k) for k in (
                "start_s", "trim_s", "duration_s", "gain_db", "fade_in", "fade_out",
                "use_in_video", "track", "name") if k in c})
        elif op == "delete_clip":
            delete_clip(project, c.get("clip_id"))
        elif op == "set_mix":
            set_mix(project, **{k: c.get(k) for k in ("dialogue", "ambience", "music", "shot",
                                                      "duck_db") if k in c})
        elif op == "add_ref":
            keys[c.get("key") or f"ref{len(keys)}"] = add_ref(
                project, c.get("kind", "character"), c.get("name", ""), c.get("description", ""),
                c.get("image_id"), c.get("speaker"), user=user)
        elif op == "edit_ref":
            edit_ref(project, rid(c.get("ref_id")), user=user,
                     **{k: c.get(k) for k in ("name", "description", "kind", "image_id",
                                              "speaker") if k in c})
        done.append(op)
    return {"applied": done, "new_ids": keys}


def apply_proposal(project, user: auth.User, prop_id: str, option: str | None = None) -> dict:
    pid = project["id"]
    row = db.one("SELECT * FROM proposals WHERE id=? AND project_id=?", (prop_id, pid))
    if row is None:
        raise HTTPException(404, "no such proposal")
    if row["status"] != "open":
        raise HTTPException(409, f"this proposal was already {row['status']}")
    result = apply_changes(project, user, _jl(row["changes"], []))
    options = _jl(row["options"], [])
    if options and row["shot_id"]:
        pick = option or options[0]
        if pick not in options:
            raise HTTPException(400, "pick one of the proposal's options")
        set_picture(db.one("SELECT * FROM projects WHERE id=?", (pid,)), user, row["shot_id"], pick)
        result["picture"] = pick
    db.run("UPDATE proposals SET status='applied', updated_at=? WHERE id=?", (db.now(), prop_id))
    touched = [v for v in result["new_ids"].values() if v.startswith("sht_")]
    for c in _jl(row["changes"], []):
        if c.get("op") == "edit_shot":
            touched.append(result["new_ids"].get(c.get("shot_id"), c.get("shot_id")))
    if row["shot_id"]:
        touched.append(row["shot_id"])
    result["touched_shots"] = [t for t in dict.fromkeys(touched) if t]
    _touch(pid)
    return result


# =========================================================================== views

def _asset_url(pid: str, asset_id: str | None) -> str | None:
    return f"/projects/{pid}/asset/{asset_id}" if asset_id else None


def state(project, base: str) -> dict:
    pid = project["id"]
    refresh_clips(pid)
    project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    params = db.params_of(project)
    tm = timings(pid)
    refs = db.query("SELECT * FROM prod_refs WHERE project_id=? ORDER BY idx", (pid,))
    shots = ordered_shots(pid)
    scenes = []
    for sc in scenes_of(pid):
        scenes.append({"id": sc["id"], "title": sc["title"], "summary": sc["summary"],
                       "shots": [s["id"] for s in shots if s["scene_id"] == sc["id"]]})
    shot_views = []
    for n, s in enumerate(shots):
        start, secs = tm[s["id"]]
        clip_ok = bool(s["clip_path"] and Path(s["clip_path"]).exists())
        shot_views.append({
            "id": s["id"], "n": n + 1, "scene_id": s["scene_id"], "title": s["title"],
            "prompt": s["prompt"], "seconds": secs, "start": start, "framing": s["framing"],
            "make": s["make"] or "animate", "refs": _jl(s["refs"], []), "status": s["status"],
            "start_from": s["start_from"] or "",
            "error": s["error"], "picture": _asset_url(pid, s["asset_id"]),
            "clip": f"/api/editor/{pid}/shots/{s['id']}/clip?v={int(Path(s['clip_path']).stat().st_mtime)}"
                    if clip_ok else None})
    clips = []
    for c in db.query("SELECT * FROM prod_clips WHERE project_id=? ORDER BY start_s", (pid,)):
        clips.append({k: c[k] for k in ("id", "track", "name", "source_kind", "source_id",
                                        "status", "error", "start_s", "trim_s", "duration_s",
                                        "source_s", "gain_db", "fade_in", "fade_out", "ref_id",
                                        "text")} |
                     {"use_in_video": bool(c["use_in_video"]),
                      "url": f"/api/editor/{pid}/clips/{c['id']}/audio" if c["path"] else None})
    ref_views = []
    for r in refs:
        ref_views.append({"id": r["id"], "kind": r["kind"], "name": r["name"],
                          "description": r["description"], "speaker": r["speaker"],
                          "image_id": r["image_id"],
                          "image": base + service.signed_path("image", r["image_id"], 86400)
                          if r["image_id"] else None,
                          "voice": f"/api/editor/{pid}/refs/{r['id']}/voice"
                          if r["voice_path"] else None})
    props = []
    for p in db.query("SELECT * FROM proposals WHERE project_id=? AND status='open' "
                      "ORDER BY created_at", (pid,)):
        opts = []
        for o in _jl(p["options"], []):
            im = db.one("SELECT status, progress FROM images WHERE id=?", (o,))
            opts.append({"image_id": o, "status": im["status"] if im else "gone",
                         "progress": im["progress"] if im else 0,
                         "url": base + service.signed_path("image", o, 86400)})
        props.append({"id": p["id"], "shot_id": p["shot_id"], "title": p["title"],
                      "summary": p["summary"], "changes": _jl(p["changes"], []), "options": opts,
                      "created_at": p["created_at"]})
    exp = params.get("_export") or {}
    export_view = {"status": exp.get("status"), "error": exp.get("error"), "at": exp.get("at")}
    if exp.get("status") == "done" and export_path(pid).exists():
        export_view["url"] = f"/api/editor/{pid}/export.mp4?v={int(export_path(pid).stat().st_mtime)}"
    thread = db.one("SELECT id FROM agent_threads WHERE project_id=? ORDER BY created_at LIMIT 1",
                    (pid,))
    return {
        "id": pid, "title": project["title"], "story": project["story"],
        "orientation": params.get("orientation", "portrait"),
        "aspect": aspect_of(params), "aspects": list(ASPECTS),
        "size": [int(params.get("width", 480)), int(params.get("height", 864))],
        "status": project["status"], "progress": project["progress"],
        "message": project["message"], "error": project["error"],
        "scenes": scenes, "shots": shot_views, "refs": ref_views, "clips": clips,
        "mix": _mix(project), "duration": sum(s["seconds"] for s in shot_views),
        "proposals": props, "export": export_view,
        "agent_applies": bool(params.get("agent_applies")),
        "thread_id": thread["id"] if thread else None,
        "speakers": sorted(speech.SPEAKERS), "framings": list(FRAMINGS),
        "limits": {m: cap_seconds(m) for m in MAKES},
        **service.retention(project["updated_at"]), "now": db.now(),
    }


def describe_for_agent(project_id: str, focus_shot: str | None = None) -> str:
    """The production as the planner reads it, compactly, with every id it needs."""
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if project is None:
        return ""
    refresh_clips(project_id)
    params = db.params_of(project)
    tm = timings(project_id)
    lines = [f"PRODUCTION project_id={project_id} \"{project['title']}\" "
             f"(aspect {aspect_of(params)}"
             f"{', rendered 16:9 and cropped to scope: keep action in the middle band' if aspect_of(params) == '2.39:1' else ''}"
             f", render: {project['status']} "
             f"{project['progress']}% {project['message'] or ''})",
             f"Story: {(project['story'] or '(none yet)')[:2500]}"]
    refs = db.query("SELECT * FROM prod_refs WHERE project_id=? ORDER BY idx", (project_id,))
    if refs:
        lines.append("References:")
        for r in refs:
            bits = [f"picture image_id={r['image_id']}" if r["image_id"] else "no picture",
                    "voice sample" if r["voice_path"] else
                    (f"speaker {r['speaker']}" if r["speaker"] else "no voice")]
            lines.append(f"- ref_id={r['id']} {r['kind']} \"{r['name']}\": "
                         f"{r['description'][:200]} [{', '.join(bits)}]")
    shots = ordered_shots(project_id)
    names = {r["id"]: r["name"] for r in refs}
    for sc in scenes_of(project_id):
        lines.append(f"SCENE scene_id={sc['id']} \"{sc['title']}\": {sc['summary'][:300]}")
        for s in [x for x in shots if x["scene_id"] == sc["id"]]:
            start, secs = tm[s["id"]]
            mark = " <== the person is looking at this shot" if s["id"] == focus_shot else ""
            lines.append(
                f"  shot_id={s['id']} #{s['idx'] + 1} \"{s['title']}\" {s['framing'] or ''} "
                f"{s['make']} {secs:g}s @{start:g}s status={s['status']}"
                f"{' starts-from-previous-last-frame' if s['start_from'] == 'prev_last' else (' picture' if s['asset_id'] else ' NO PICTURE')} "
                f"refs=[{', '.join(names.get(r, r) for r in _jl(s['refs'], []))}] "
                f"prompt: {(s['prompt'] or '')[:220]}{mark}")
    clips = db.query("SELECT * FROM prod_clips WHERE project_id=? ORDER BY track, start_s",
                     (project_id,))
    if clips:
        lines.append("Timeline audio:")
        for c in clips:
            lines.append(f"- clip_id={c['id']} {c['track']} @{c['start_s']:g}s "
                         f"{(c['duration_s'] or 0):g}s gain {c['gain_db']:g}dB "
                         f"{'USE-IN-VIDEO ' if c['use_in_video'] else ''}{c['status']} "
                         f"\"{c['name'][:80]}\"")
    mix = _mix(project)
    lines.append("Mix: " + ", ".join(f"{t} {mix[t]['gain_db']:g}dB{' muted' if mix[t]['mute'] else ''}"
                                     for t in ("dialogue", "ambience", "music", "shot")) +
                 f", music/ambience duck {mix['duck_db']:g}dB under dialogue")
    props = db.query("SELECT id, title FROM proposals WHERE project_id=? AND status='open'",
                     (project_id,))
    if props:
        lines.append("Open proposals (waiting for the person): " +
                     "; ".join(f"{p['id']} {p['title']}" for p in props))
    lines.append(f"Total {sum(tm[s['id']][1] for s in shots):g}s. Shot length caps: animate "
                 f"{cap_seconds('animate')}s, perform {cap_seconds('perform')}s, lipsync "
                 f"{cap_seconds('lipsync')}s. "
                 + ("The person lets you apply changes directly."
                    if params.get("agent_applies") else
                    "Edits you make with propose_changes wait for the person to apply them."))
    return "\n".join(lines)


# =========================================================================== agent tools

def _agent_project(user, args):
    return production(user, str(args.get("project_id") or ""))


def _t_state(user, args, base):
    return {"production": describe_for_agent(_agent_project(user, args)["id"],
                                              args.get("focus_shot_id"))}


def _t_propose(user, args, base):
    project = _agent_project(user, args)
    changes = args.get("changes") or []
    if db.params_of(project).get("agent_applies"):
        result = apply_changes(project, user, changes)
        return {"applied": True, **result}
    pid = propose(project, str(args.get("title") or "Proposed changes"),
                  str(args.get("summary") or ""), changes, args.get("shot_id"))
    return {"proposal_id": pid, "status": "waiting for the person to review and apply",
            "changes": len(changes)}


async def _t_picture_options(user, args, base):
    project = _agent_project(user, args)
    shot_id = str(args.get("shot_id") or "")
    ids = await picture_options(project, user, shot_id, str(args.get("prompt") or ""),
                                int(args.get("count") or 2), args.get("model"))
    for i in ids:
        await images.wait(i, 60)
    done = [i for i in ids if (db.one("SELECT status FROM images WHERE id=?", (i,)) or
                               {"status": ""})["status"] == "succeeded"]
    if db.params_of(project).get("agent_applies") and done:
        set_picture(project, user, shot_id, done[0])
        return {"image_ids": ids, "applied": done[0]}
    pid = propose(project, str(args.get("title") or "New picture options"),
                  str(args.get("summary") or f"{len(ids)} pictures to choose from"), [],
                  shot_id, ids)
    out = {"image_ids": ids, "proposal_id": pid,
           "status": "the person picks one; you can see them on review"}
    if done:
        out["image_url"] = base + service.signed_path("image", done[0], 86400)
        out["image_id"] = done[0]
    return out


async def _t_render(user, args, base):
    project = _agent_project(user, args)
    ids = args.get("shot_ids") or None
    await render_shots(project, user, ids)
    return {"project_id": project["id"], "status": "queued",
            "rendering": ids or "every shot without an up-to-date clip"}


async def _t_dialogue(user, args, base):
    project = _agent_project(user, args)
    start = args.get("start_s")
    if start is None and args.get("shot_id"):
        start = timings(project["id"]).get(args["shot_id"], (0, 0))[0] + \
            float(args.get("offset_s") or 0.3)
    cid = await add_dialogue(project, user, args.get("ref_id"), str(args.get("text") or ""),
                             float(start or 0), str(args.get("instruct") or ""),
                             args.get("language"), bool(args.get("use_in_video", True)),
                             args.get("speaker"))
    return {"clip_id": cid, "status": "speaking (ready in about 15-60 s)"}


async def _t_music(user, args, base):
    project = _agent_project(user, args)
    start = float(args.get("start_s") or 0)
    if args.get("scene_id"):
        shots = [s for s in ordered_shots(project["id"]) if s["scene_id"] == args["scene_id"]]
        if shots:
            tm = timings(project["id"])
            start = tm[shots[0]["id"]][0]
            if not args.get("duration"):
                args["duration"] = sum(tm[s["id"]][1] for s in shots)
    cid = await add_music(project, user, str(args.get("description") or ""), start,
                          args.get("duration"), str(args.get("track") or "music"),
                          bool(args.get("instrumental", True)),
                          bool(args.get("use_in_video", False)), str(args.get("lyrics") or ""))
    return {"clip_id": cid, "status": "composing (about a minute when the GPU is free)"}


async def _t_export(user, args, base):
    project = _agent_project(user, args)
    await export(project)
    return {"status": "mixing the film on the CPU; it appears on the Export button"}


_PID = {"project_id": {"type": "string"}}
AGENT_TOOLS = {
    "production_state": {
        "handler": _t_state, "light": True,
        "schema": {"type": "object", "required": ["project_id"],
                   "properties": {**_PID, "focus_shot_id": {"type": "string"}}},
        "description": "The whole production: story, references, scenes, shots (ids, timing, "
                       "status, prompts), timeline audio clips, mix, open proposals."},
    "propose_changes": {
        "handler": _t_propose, "light": True,
        "schema": {"type": "object", "required": ["project_id", "title", "changes"], "properties": {
            **_PID, "title": {"type": "string"}, "summary": {"type": "string"},
            "shot_id": {"type": "string", "description": "the shot this is about, if one"},
            "changes": {"type": "array", "items": {"type": "object"}, "description":
                        "ordered edits, each {op, ...}. ops: set_story{title,story,aspect "
                        "(9:16|16:9|1:1|2.39:1)}; "
                        "add_scene{key,title,summary,position}; edit_scene{scene_id,title,"
                        "summary,position}; delete_scene{scene_id}; add_shot{key,scene_id,title,"
                        "prompt,seconds,framing,refs,make,position,image_id,start_from}; edit_shot{"
                        "shot_id,title,prompt,seconds,framing,refs,make,scene_id,position,image_id,"
                        "start_from}; use_frame{from_shot_id,at:'last'|seconds,to_shot_id or "
                        "new_shot:true,key} (a frame of a rendered clip becomes a shot's picture); "
                        "start_from: '' (own picture) | 'prev_last' (opens on the previous shot's "
                        "last frame, taken when it renders: a continuous take); "
                        "delete_shot{shot_id}; edit_clip{clip_id,start_s,trim_s,duration_s,"
                        "gain_db,fade_in,fade_out,use_in_video,track}; delete_clip{clip_id}; "
                        "set_mix{dialogue:{gain_db,mute},ambience:{..},music:{..},shot:{..},"
                        "duck_db}; add_ref{key,kind,name,description,image_id,speaker}; "
                        "edit_ref{ref_id,...}. A new scene/shot/ref may be given a `key` and "
                        "used as its id later in the same list. framing: " +
                        "|".join(f for f in FRAMINGS if f) + ". make: animate (silent, picture "
                        "is the first frame) | perform (ref2v: picture + reference pictures + "
                        "use-in-video audio under the shot; makes its own sound) | lipsync "
                        "(Wan S2V: the picture speaks/sings the use-in-video dialogue line "
                        "under the shot, mouth in sync)."}}},
        "description": "Edit the production (story, scenes, shots, references, audio clips, "
                       "mix). The person reviews and applies the proposal unless they let you "
                       "apply directly. Use it for any change to the story structure or mix."},
    "shot_picture_options": {
        "handler": _t_picture_options,
        "schema": {"type": "object", "required": ["project_id", "shot_id"], "properties": {
            **_PID, "shot_id": {"type": "string"},
            "prompt": {"type": "string", "description": "the picture; default: built from the "
                                                        "shot's framing, title, prompt, scene"},
            "count": {"type": "integer", "description": "1-4, default 2"},
            "model": {"type": "string"}, "title": {"type": "string"},
            "summary": {"type": "string"}}},
        "description": "Make picture options for a shot with its references' pictures as "
                       "sources (keeps characters consistent). Offered to the person to pick."},
    "render_shots": {
        "handler": _t_render,
        "schema": {"type": "object", "required": ["project_id"], "properties": {
            **_PID, "shot_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "omit = every shot without an up-to-date clip"}}},
        "description": "Generate the video clips of these shots now, ahead of the film. Each "
                       "needs a picture and a prompt. ~2 min per 5 s."},
    "add_dialogue": {
        "handler": _t_dialogue,
        "schema": {"type": "object", "required": ["project_id", "text"], "properties": {
            **_PID, "text": {"type": "string"},
            "ref_id": {"type": "string", "description": "the character speaking (their voice "
                                                        "sample is cloned, or preset used)"},
            "shot_id": {"type": "string", "description": "place it at this shot's start"},
            "start_s": {"type": "number", "description": "or an exact time on the timeline"},
            "offset_s": {"type": "number"}, "instruct": {"type": "string",
                                                         "description": "delivery, e.g. 'weeping, "
                                                                        "broken voice'"},
            "speaker": {"type": "string"}, "language": {"type": "string"},
            "use_in_video": {"type": "boolean", "description": "default true: a perform shot "
                                                               "under it uses it"}}},
        "description": "A spoken line on the dialogue track, in a character's voice."},
    "add_music": {
        "handler": _t_music,
        "schema": {"type": "object", "required": ["project_id", "description"], "properties": {
            **_PID, "description": {"type": "string"},
            "track": {"type": "string", "enum": ["music", "ambience"]},
            "scene_id": {"type": "string", "description": "span this scene"},
            "start_s": {"type": "number"}, "duration": {"type": "number"},
            "instrumental": {"type": "boolean"}, "lyrics": {"type": "string"},
            "use_in_video": {"type": "boolean", "description": "default false"}}},
        "description": "Score or ambience/sound effects on the timeline (ACE-Step)."},
    "export_film": {
        "handler": _t_export,
        "schema": {"type": "object", "required": ["project_id"], "properties": {**_PID}},
        "description": "Join every shot's clip and mix the timeline into the final film."},
}
EDITOR_HEAVY = {"shot_picture_options", "render_shots", "add_dialogue", "add_music"}


# =========================================================================== API

async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        return {}
    return b if isinstance(b, dict) else {}


@router.post("/api/editor")
async def api_create(request: Request):
    user = _user(request)
    b = await _body(request)
    pid = create(user, str(b.get("title") or "Untitled production"), str(b.get("story") or ""),
                 str(b.get("aspect") or b.get("orientation") or "9:16"))
    return {"id": pid}


async def refresh_pending(pid: str) -> None:
    """Studio pictures learn they are finished only when someone asks WIS; the
    page asks through here, or picture options would wait forever (2026-11-24)."""
    for p in db.query("SELECT options FROM proposals WHERE project_id=? AND status='open'", (pid,)):
        for o in _jl(p["options"], []):
            row = db.one("SELECT status FROM images WHERE id=?", (o,))
            if row and row["status"] not in images.TERMINAL:
                try:
                    await images.refresh(o)
                except Exception:                              # noqa: BLE001
                    log.warning("could not refresh picture %s", o)


@router.get("/api/editor/{pid}")
async def api_state(pid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    await refresh_pending(pid)
    return state(project, service.base_url(request))


@router.post("/api/editor/{pid}/settings")
async def api_settings(pid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    out = {"ok": True}
    if any(k in b for k in ("title", "story")):
        set_story(project, b.get("title"), b.get("story"))
    if b.get("aspect"):
        out["rerender"] = set_aspect(db.one("SELECT * FROM projects WHERE id=?", (pid,)), b["aspect"])
    if "agent_applies" in b:
        params = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (pid,)))
        params["agent_applies"] = bool(b["agent_applies"])
        db.set_params(pid, params)
    if "mix" in b and isinstance(b["mix"], dict):
        set_mix(db.one("SELECT * FROM projects WHERE id=?", (pid,)), **b["mix"])
    return out


@router.post("/api/editor/{pid}/changes")
async def api_changes(pid: str, request: Request):
    """The page's own edits, through the same code as an applied proposal."""
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    changes = b.get("changes") or []
    _validate_changes(changes)
    return apply_changes(project, user, changes)


@router.post("/api/editor/{pid}/shots/{sid}/picture")
async def api_picture(pid: str, sid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            raise HTTPException(400, "no file")
        raw = await up.read()
        image_id = _import_picture(user, getattr(up, "filename", "picture.png"), raw)
    else:
        image_id = str((await _body(request)).get("image_id") or "")
    set_picture(project, user, sid, image_id)
    return {"ok": True, "image_id": image_id}


def _import_picture(user: auth.User, name: str, raw: bytes) -> str:
    """An uploaded picture becomes a studio picture, so it works everywhere."""
    from PIL import Image, ImageOps
    import io
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((4096, 4096))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            w, h = img.size
    except Exception:                                          # noqa: BLE001
        raise HTTPException(400, f"{name} isn't a picture this studio can read")
    image_id = db.new_id("img")
    dest = cfg.images_dir / user.id
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{image_id}.png"
    path.write_bytes(buf.getvalue())
    now = db.now()
    db.run("INSERT INTO images (id, user_id, prompt, sources, status, progress, path, width, "
           "height, model, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (image_id, user.id, f"uploaded: {name}"[:200], 0, "succeeded", 100, str(path), w, h,
            "upload", now, now))
    return image_id


@router.post("/api/editor/{pid}/shots/{sid}/frame")
async def api_frame(pid: str, sid: str, request: Request):
    """{at: seconds|"last", to: "next"|"new"|"this"|<shot id>} -- a frame becomes a picture."""
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    to = str(b.get("to") or "next")
    return await asyncio.to_thread(
        use_frame, project, user, sid, b.get("at", "last"),
        sid if to == "this" else (None if to in ("next", "new") else to), key_new=(to == "new"))


@router.post("/api/editor/{pid}/shots/{sid}/options")
async def api_options(pid: str, sid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    ids = await picture_options(project, user, sid, str(b.get("prompt") or ""),
                                int(b.get("count") or 2), b.get("model"))
    prop = propose(project, "Picture options", str(b.get("prompt") or "")[:200], [], sid, ids)
    return {"proposal_id": prop, "image_ids": ids}


@router.post("/api/editor/{pid}/render")
async def api_render(pid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    await render_shots(project, user, b.get("shot_ids") or None)
    return {"ok": True}


@router.post("/api/editor/{pid}/cancel")
async def api_cancel(pid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    if not service.is_rendering(project):
        return {"ok": True}
    render.request_cancel(pid)
    return {"ok": True}


@router.get("/api/editor/{pid}/shots/{sid}/clip")
async def api_clip(pid: str, sid: str, request: Request):
    user = _user(request)
    production(user, pid)
    shot = _shot(pid, sid)
    if not shot["clip_path"] or not Path(shot["clip_path"]).exists():
        raise HTTPException(404, "no clip yet")
    return FileResponse(shot["clip_path"], media_type="video/mp4")


@router.post("/api/editor/{pid}/refs")
async def api_add_ref(pid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    form = await request.form()
    image_id = str(form.get("image_id") or "") or None
    pic = form.get("picture")
    if pic is not None and hasattr(pic, "read"):
        raw = await pic.read()
        if raw:
            image_id = _import_picture(user, getattr(pic, "filename", "picture.png"), raw)
    voice = form.get("voice")
    vraw = await voice.read() if voice is not None and hasattr(voice, "read") else None
    rid = add_ref(project, str(form.get("kind") or "character"), str(form.get("name") or ""),
                  str(form.get("description") or ""), image_id,
                  str(form.get("speaker") or "") or None, vraw or None,
                  getattr(voice, "filename", "") if vraw else "", user)
    return {"id": rid}


@router.post("/api/editor/{pid}/refs/{rid}")
async def api_edit_ref(pid: str, rid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    form = await request.form()
    f = {k: form.get(k) for k in ("name", "description", "kind", "speaker", "image_id")
         if form.get(k) is not None}
    pic = form.get("picture")
    if pic is not None and hasattr(pic, "read"):
        raw = await pic.read()
        if raw:
            f["image_id"] = _import_picture(user, getattr(pic, "filename", "picture.png"), raw)
    voice = form.get("voice")
    if voice is not None and hasattr(voice, "read"):
        raw = await voice.read()
        if raw:
            f["voice"], f["voice_name"] = raw, getattr(voice, "filename", "")
    edit_ref(project, rid, user=user, **f)
    return {"ok": True}


@router.post("/api/editor/{pid}/refs/{rid}/delete")
async def api_del_ref(pid: str, rid: str, request: Request):
    user = _user(request)
    delete_ref(production(user, pid), rid)
    return {"ok": True}


@router.get("/api/editor/{pid}/refs/{rid}/voice")
async def api_ref_voice(pid: str, rid: str, request: Request):
    user = _user(request)
    production(user, pid)
    r = _ref(pid, rid)
    if not r["voice_path"] or not Path(r["voice_path"]).exists():
        raise HTTPException(404, "no voice sample")
    return FileResponse(r["voice_path"])


@router.post("/api/editor/{pid}/clips")
async def api_add_clip(pid: str, request: Request):
    """{kind: dialogue|music|ambience|speech_id|music_id, ...} or a multipart upload."""
    user = _user(request)
    project = production(user, pid)
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            raise HTTPException(400, "no file")
        cid = add_upload(project, str(form.get("track") or "music"),
                         getattr(up, "filename", "audio"), await up.read(),
                         float(form.get("start_s") or 0))
        return {"id": cid}
    b = await _body(request)
    kind = b.get("kind")
    start = float(b.get("start_s") or 0)
    if kind == "dialogue":
        cid = await add_dialogue(project, user, b.get("ref_id"), str(b.get("text") or ""), start,
                                 str(b.get("instruct") or ""), b.get("language"),
                                 bool(b.get("use_in_video", True)), b.get("speaker"))
    elif kind in ("music", "ambience"):
        cid = await add_music(project, user, str(b.get("description") or ""), start,
                              b.get("duration"), kind, bool(b.get("instrumental", True)),
                              bool(b.get("use_in_video", False)), str(b.get("lyrics") or ""))
    else:
        cid = add_existing(project, user, str(b.get("track") or "music"),
                           speech_id=b.get("speech_id"), music_id=b.get("music_id"),
                           start_s=start, use_in_video=bool(b.get("use_in_video", False)))
    return {"id": cid}


@router.get("/api/editor/{pid}/clips/{cid}/audio")
async def api_clip_audio(pid: str, cid: str, request: Request):
    user = _user(request)
    production(user, pid)
    c = _clip(pid, cid)
    if not c["path"] or not Path(c["path"]).exists():
        raise HTTPException(404, "not ready")
    return FileResponse(c["path"])


@router.post("/api/editor/{pid}/proposals/{prop}/apply")
async def api_apply(pid: str, prop: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    result = apply_proposal(project, user, prop, b.get("option"))
    if b.get("render") and result["touched_shots"]:
        ready = [s for s in result["touched_shots"]
                 if (r := db.one("SELECT asset_id, prompt FROM shots WHERE id=?", (s,)))
                 and r["asset_id"] and (r["prompt"] or "").strip()]
        if ready:
            await render_shots(db.one("SELECT * FROM projects WHERE id=?", (pid,)), user, ready)
            result["rendering"] = ready
    return result


@router.post("/api/editor/{pid}/proposals/{prop}/dismiss")
async def api_dismiss(pid: str, prop: str, request: Request):
    user = _user(request)
    production(user, pid)
    db.run("UPDATE proposals SET status='dismissed', updated_at=? WHERE id=? AND project_id=?",
           (db.now(), prop, pid))
    return {"ok": True}


@router.post("/api/editor/{pid}/export")
async def api_export(pid: str, request: Request):
    user = _user(request)
    await export(production(user, pid))
    return {"ok": True}


@router.get("/api/editor/{pid}/export.mp4")
async def api_export_file(pid: str, request: Request, download: int = 0):
    user = _user(request)
    project = production(user, pid)
    p = export_path(pid)
    if not p.exists():
        raise HTTPException(404, "not exported yet")
    name = "".join(ch for ch in project["title"] if ch.isalnum() or ch in " -_")[:60] or "film"
    return FileResponse(p, media_type="video/mp4", filename=f"{name}.mp4",
                        content_disposition_type="attachment" if download else "inline")


@router.post("/api/editor/{pid}/agent")
async def api_agent_thread(pid: str, request: Request):
    """The production's conversation with the assistant (made on first use)."""
    from app import agent
    user = _user(request)
    project = production(user, pid)
    row = db.one("SELECT id FROM agent_threads WHERE project_id=? ORDER BY created_at LIMIT 1",
                 (pid,))
    if row:
        return {"id": row["id"]}
    tid = agent._new_thread(user, request, f"🎬 {project['title']}"[:70])
    db.run("UPDATE agent_threads SET project_id=? WHERE id=?", (pid, tid))
    return {"id": tid}


def productions_of(user: auth.User) -> list:
    rows = db.query("SELECT * FROM projects WHERE user_id=? AND mode='storyboard' "
                    "ORDER BY updated_at DESC LIMIT 100", (user.id,))
    return [r for r in rows if db.params_of(r).get("editor")]


def first_picture(pid: str) -> str | None:
    s = db.one("SELECT asset_id FROM shots WHERE project_id=? AND asset_id IS NOT NULL "
               "ORDER BY idx LIMIT 1", (pid,))
    return _asset_url(pid, s["asset_id"]) if s else None


def delete_production_rows(pid: str) -> None:
    for c in db.query("SELECT * FROM prod_clips WHERE project_id=? AND source_kind='upload'",
                      (pid,)):
        if c["path"]:
            Path(c["path"]).unlink(missing_ok=True)
    for t in ("prod_scenes", "prod_refs", "prod_clips", "proposals"):
        db.run(f"DELETE FROM {t} WHERE project_id=?", (pid,))


__all__ = ["router", "AGENT_TOOLS", "EDITOR_HEAVY", "describe_for_agent", "perform_payload",
           "state", "create", "productions_of", "first_picture", "delete_production_rows",
           "math"]
